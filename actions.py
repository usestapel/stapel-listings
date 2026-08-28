"""Action subscriptions (comm consumers) of stapel-listings.

Handlers must be idempotent: delivery is at-least-once (outbox retries, broker
redelivery). Consumed contracts are documented in ``schemas/consumes/*.json``.

- ``category.changed`` (from stapel-categories) — invalidate the cached
  feature configs for that category so re-validation picks up the new schema.
- ``moderation.completed`` (from stapel-moderation) — flip the listing's
  moderation/lifecycle status. The moderation *decision* is owned by that
  module; this module only applies the verdict. The topic is target-generic
  (``target_type``/``target_key``); verdicts for other target types are
  ignored here.
- ``user.deleted`` (from stapel-auth/gdpr) — erase the user's listings and
  favorites (GDPR Art. 17).
- ``user.merged`` (from stapel-auth) — an anonymous guest was absorbed into an
  existing account; carry the guest's favorites and listings over to it.
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


class MergeTargetNotReady(RuntimeError):
    """A ``user.merged`` arrived before the surviving account exists here.

    Transient, not a bug: the guest has rows to carry over but there is no
    local user row to point their FKs at yet. Raising is the comm layer's
    retry signal — ``deliver()`` wraps a failing handler in
    ``ActionDeliveryError`` and the outbox redelivers — so the transfer
    completes once the survivor's user projection lands. An operator seeing
    this in a redelivery loop is looking at an ordering lag, not a defect.
    """


@on_action("category.changed")
def handle_category_changed(event):
    """Invalidate cached feature configs for a mutated category."""
    from .services import category_schema

    category_id = event.payload.get("category_id")
    if category_id is None:
        logger.warning("category.changed without category_id: %s", event.event_id)
        return
    revision = event.payload.get("revision")
    # Advance the cache's revision pointer (M-6). Categories key their id as int
    # while callers may pass a str (listing.category_id); cover both key forms.
    category_schema.note_changed(category_id, revision)
    category_schema.note_changed(str(category_id), revision)


@on_action("moderation.completed")
def handle_moderation_completed(event):
    """Apply a moderation verdict to the target listing.

    The verdict topic is target-generic — one moderation queue rules over
    listings, reviews, profiles and chat messages — so a payload addresses its
    target as ``{target_type, target_key}`` and this handler ignores every
    target type that is not ours (``MODERATION_TARGET_TYPE``). The pre-0.4
    ``{listing_id}`` form is still accepted: a payload with no ``target_type``
    is a listing verdict by construction, and one that carries ``listing_id``
    next to a matching ``target_type`` resolves the same way.
    """
    from .conf import listings_settings
    from .models import Listing

    payload = event.payload or {}
    target_type = payload.get("target_type")
    if target_type is not None and target_type != listings_settings.MODERATION_TARGET_TYPE:
        return  # another module's target — not our verdict to apply

    listing_id = payload.get("target_key", payload.get("listing_id"))
    decision = payload.get("decision")
    if not listing_id or not decision:
        logger.error(
            "moderation.completed missing target_key/listing_id or decision: %s",
            event.event_id,
        )
        return
    try:
        listing = Listing.all_objects.get(pk=listing_id)
    except (Listing.DoesNotExist, ValueError, TypeError):
        logger.warning("moderation.completed for unknown listing %r", listing_id)
        return

    note = payload.get("note") or payload.get("reason_code") or ""
    listing.apply_moderation(decision, note=note)
    logger.info("listing %s moderation -> %s", listing_id, decision)


@on_action("user.deleted")
def handle_user_deleted(event):
    """Erase a deleted user's listings and favorites (GDPR Art. 17)."""
    from .gdpr import ListingsGDPRProvider

    user_id = event.payload.get("user_id")
    if not user_id:
        logger.error("user.deleted without user_id: %s", event.event_id)
        return
    ListingsGDPRProvider().delete(user_id)
    logger.info("listings erased for deleted user %s", user_id)


@on_action("user.merged")
def handle_user_merged(event):
    """Carry a merged-away account's favorites and listings to the survivor.

    stapel-auth deletes the absorbed row, and every row this module owns hangs
    off it by ``on_delete=CASCADE`` — so without this handler a visitor who
    saved listings as a guest loses them the moment they sign in with an
    account that already exists. Reassignment happens here, in one
    transaction, before that deletion can cascade.

    Two different "unknown id" situations, and conflating them loses data:

    * the guest owns nothing here (never visited, or a previous delivery
      already moved it all) — a genuine no-op, returned quietly;
    * the guest owns rows but the survivor has no user row here yet — NOT a
      no-op. :class:`MergeTargetNotReady` is raised so the event is
      redelivered, because returning success would let the outbox mark it
      delivered and lose the favorites forever.
    """
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from .models import Favorite, Listing

    payload = event.payload or {}
    from_user_id = payload.get("from_user_id")
    into_user_id = payload.get("into_user_id")
    if not from_user_id or not into_user_id:
        logger.error("user.merged without from/into user id: %s", event.event_id)
        return
    if str(from_user_id) == str(into_user_id):
        return

    user_model = get_user_model()
    with transaction.atomic():
        # Both reads and the decision they feed happen inside the transaction
        # and before the first write, so the "not yet" path below can never
        # leave half the rows moved.
        try:
            owns_favorites = Favorite.objects.filter(user_id=from_user_id).exists()
            owns_listings = Listing.all_objects.filter(owner_id=from_user_id).exists()
        except (ValueError, TypeError):
            logger.warning("user.merged with unusable user ids: %s", event.event_id)
            return
        if not (owns_favorites or owns_listings):
            # Nothing to carry: the guest never reached this service, or a
            # previous delivery already moved everything. Quiet by design —
            # this is also the at-least-once idempotency path.
            return
        if not user_model.objects.filter(pk=into_user_id).exists():
            # The guest HAS rows but the survivor has no row here yet, so
            # nothing can point a FK at them. Not a no-op: raising is this
            # comm layer's retry signal (deliver() wraps it in
            # ActionDeliveryError and the outbox redelivers), so the transfer
            # lands once the survivor's user projection arrives.
            raise MergeTargetNotReady(
                f"user.merged {from_user_id} -> {into_user_id}: the surviving "
                f"account has no user row in stapel-listings yet; redeliver "
                f"once its projection has landed"
            )

        # The survivor already saved some of these listings. A blind update
        # would break uniq_user_listing_fav; drop the guest's duplicate rows
        # instead — the listing stays saved, under the survivor.
        already = Favorite.objects.filter(user_id=into_user_id).values_list(
            "listing_id", flat=True
        )
        Favorite.objects.filter(
            user_id=from_user_id, listing_id__in=list(already)
        ).delete()
        moved_favorites = Favorite.objects.filter(user_id=from_user_id).update(
            user_id=into_user_id
        )
        # all_objects: a soft-deleted listing is still the survivor's to own.
        moved_listings = Listing.all_objects.filter(owner_id=from_user_id).update(
            owner_id=into_user_id
        )

    logger.info(
        "user.merged %s -> %s: %s favorites, %s listings carried over",
        from_user_id,
        into_user_id,
        moved_favorites,
        moved_listings,
    )
