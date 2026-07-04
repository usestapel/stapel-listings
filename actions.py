"""Action subscriptions (comm consumers) of stapel-listings.

Handlers must be idempotent: delivery is at-least-once (outbox retries, broker
redelivery). Consumed contracts are documented in ``schemas/consumes/*.json``.

- ``category.changed`` (from stapel-categories) — invalidate the cached
  feature configs for that category so re-validation picks up the new schema.
- ``moderation.completed`` (from a future stapel-moderation module) — flip the
  listing's moderation/lifecycle status. The moderation *decision* is owned by
  that module; this module only applies the verdict.
- ``user.deleted`` (from stapel-auth/gdpr) — erase the user's listings and
  favorites (GDPR Art. 17).
"""
import logging

from stapel_core.comm import on_action

logger = logging.getLogger(__name__)


@on_action("category.changed")
def handle_category_changed(event):
    """Invalidate cached feature configs for a mutated category."""
    from .services import category_schema

    category_id = event.payload.get("category_id")
    if category_id is None:
        logger.warning("category.changed without category_id: %s", event.event_id)
        return
    category_schema.invalidate(category_id)
    # Categories may key their id as int; our cache keys stringify — clear both.
    category_schema.invalidate(str(category_id))


@on_action("moderation.completed")
def handle_moderation_completed(event):
    """Apply a moderation verdict to the target listing."""
    from .models import Listing

    payload = event.payload or {}
    listing_id = payload.get("listing_id")
    decision = payload.get("decision")
    if not listing_id or not decision:
        logger.error("moderation.completed missing listing_id/decision: %s", event.event_id)
        return
    try:
        listing = Listing.all_objects.get(pk=listing_id)
    except Listing.DoesNotExist:
        logger.warning("moderation.completed for unknown listing %s", listing_id)
        return

    listing.apply_moderation(decision, note=payload.get("note", ""))
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
