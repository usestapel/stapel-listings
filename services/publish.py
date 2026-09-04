"""Publish workflow: promote a draft to a moderated, indexable listing.

Ported from the legacy catalog's ``ads/services/publish_ad.py``, decoupled:

- feature configs come from the ``categories.features`` comm Function
  (``category_schema.get_feature_configs``), not a local Feature model;
- value validation / DTO->DAO conversion delegate to stapel-attributes;
- moderation is requested by emitting ``listing.submitted`` (a future
  stapel-moderation module consumes it and replies with
  ``moderation.completed``); no LLM pipeline lives here. Deployments without
  a moderation module set ``AUTO_APPROVE_ON_PUBLISH`` to publish immediately.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from stapel_core.comm import mutate_and_emit

from stapel_attributes import validate_description, validate_dto
from stapel_attributes.results import (
    FeatureValidationResult,
    ValidationBatchResult,
    ValidationStatus,
)
from stapel_attributes import validate_dto_structured

from ..conf import listings_settings
from ..errors import ERR_400_FEATURE_NOT_ALLOWED, ERR_400_PUBLISH_VALIDATION_FAILED
from ..models import INDEXED_STATUSES, ListingStatus, ModerationStatus, has_category
from . import category_schema
from .features import build_projections
from .location import resolve_place_label

logger = logging.getLogger(__name__)


def _unknown_slug_results(configs, features_draft) -> list:
    """Structured results for draft keys not present in the category schema.

    M-7: ``validate_dto_structured`` silently ignores unknown slugs, but
    ``publish_listing``'s ``validate_dto`` raises on them — so a draft holding a
    feature since removed from the category validated clean yet failed publish
    with an opaque error. We converge the policy on the *reject* side (with
    per-feature detail) here, in listings, without touching stapel-attributes.
    """
    allowed = set()
    for cfg in configs or []:
        slug = cfg.get("slug") if isinstance(cfg, dict) else getattr(cfg, "slug", None)
        fid = cfg.get("id") if isinstance(cfg, dict) else getattr(cfg, "id", None)
        if slug:
            allowed.add(str(slug))
        if fid is not None:
            allowed.add(str(fid))

    results = []
    for key in (features_draft or {}):
        if str(key) not in allowed:
            results.append(
                FeatureValidationResult(
                    slug=str(key),
                    status=ValidationStatus.VALIDATION_FAILED,
                    localizable_error=ERR_400_FEATURE_NOT_ALLOWED,
                    params={"feature": str(key), "slug": str(key)},
                    message=f"Feature '{key}' is not allowed for this category",
                )
            )
    return results


def _zero_price_result(listing):
    """A structured failure for an EXPLICIT 0 price, or ``None``.

    NULL ("price not stated") is legal everywhere; 0 is the claim "free" and
    is legal only in the categories the host names in FREE_PRICE_CATEGORY_IDS.
    The distinction is the whole Д51 fix: the composer's skipped price used to
    publish as a public «0 ₽» because the model defaulted the difference away.
    """
    from ..errors import ERR_400_ZERO_PRICE_NOT_ALLOWED

    if listing.price_draft is None or listing.price_draft != 0:
        return None
    allowed = {str(cid) for cid in listings_settings.FREE_PRICE_CATEGORY_IDS or ()}
    if str(listing.category_id or "") in allowed:
        return None
    return FeatureValidationResult(
        slug="price",
        status=ValidationStatus.VALIDATION_FAILED,
        localizable_error=ERR_400_ZERO_PRICE_NOT_ALLOWED,
        params={"category_id": str(listing.category_id or "")},
        message="A price of 0 is not allowed in this category; "
                "leave the price empty for 'price not stated'",
    )


def _missing_location_result(listing):
    """A structured failure for a draft with no coordinates, or ``None`` (Д71).

    Structured, not a bare ``ValidationError``: the composer renders errors
    per control, and the image check's flattened
    ``ERR_400_PUBLISH_VALIDATION_FAILED`` leaves a seller in front of a form
    that says something is wrong and not what. The slug is ``location`` — not
    a category feature, but the same channel, because the composer already
    knows how to put a message under a named control and inventing a second
    error shape for one field would be a second thing to render.
    """
    from ..errors import ERR_400_LOCATION_REQUIRED
    from .location import has_place

    if not listings_settings.REQUIRE_LOCATION_ON_PUBLISH:
        return None
    if has_place(listing):
        return None
    return FeatureValidationResult(
        slug="location",
        status=ValidationStatus.VALIDATION_FAILED,
        localizable_error=ERR_400_LOCATION_REQUIRED,
        params={},
        message="Choose where the item is before publishing",
    )


def _missing_category_result(listing):
    """A structured failure for a draft with no category, or ``None``.

    ``category_id`` is nullable since 0.21.4 so the composer can open the row
    on the first photo, before the category step. Publishing is where it stops
    being optional, and the refusal is shaped like the location one: a
    structured result under a named control, on the same channel the composer
    already renders, rather than a second error shape for one field.

    The slug is ``category_id`` — the field's own name, not a category feature
    — and the localizable error is the EXISTING
    ``ERR_400_PUBLISH_VALIDATION_FAILED``: a caller that goes straight to
    ``publish_listing`` past ``validate-draft`` gets that same code back from
    the view, so both doors say the same word.
    """
    if has_category(listing):
        return None
    return FeatureValidationResult(
        slug="category_id",
        status=ValidationStatus.VALIDATION_FAILED,
        localizable_error=ERR_400_PUBLISH_VALIDATION_FAILED,
        params={},
        message="Choose a category before publishing",
    )


def validate_draft(listing) -> ValidationBatchResult:
    """Structured validation of a listing's draft against its category schema.

    Combines feature-value validation (via the comm-fetched configs) with the
    free-text description length check. Used by the validate/publish views to
    return machine-readable results. Unknown feature slugs are flagged (M-7) so
    this agrees with ``publish_listing``.
    """
    # No category -> no schema to validate values against, and no way to tell
    # a genuinely unknown slug from one this category would have declared. The
    # category itself is then the failure; the free-text/price/location checks
    # below still run, so the composer gets the whole list at once.
    category_error = _missing_category_result(listing)
    configs = (
        [] if category_error is not None
        else category_schema.get_feature_configs(listing.category_id)
    )
    result = validate_dto_structured(configs, listing.features_draft or {})

    if category_error is None:
        unknown = _unknown_slug_results(configs, listing.features_draft or {})
        if unknown:
            result.results.extend(unknown)
            result.valid = False

    price_error = _zero_price_result(listing)
    if price_error is not None:
        result.results.insert(0, price_error)
        result.valid = False

    location_error = _missing_location_result(listing)
    if location_error is not None:
        result.results.insert(0, location_error)
        result.valid = False

    desc_error = validate_description(
        listing.description_draft,
        min_length=listings_settings.DESCRIPTION_MIN_LENGTH,
        max_length=listings_settings.DESCRIPTION_MAX_LENGTH,
    )
    if desc_error is not None:
        result.results.insert(0, desc_error)
        result.valid = False

    if category_error is not None:
        result.results.insert(0, category_error)
        result.valid = False
    return result


def publish_listing(listing) -> None:
    """Validate the draft, build projections, promote fields, request moderation.

    Raises ``django.core.exceptions.ValidationError`` when the draft is invalid
    or (per policy) an image is missing.

    **First publication** (any non-indexed status) follows the
    ``MODERATION_GATE`` policy. Under ``"pre"`` (default) it is the strict
    path it has always been: lifecycle -> PENDING, moderation -> PENDING,
    nothing public until a verdict arrives. Under ``"post"`` the listing
    goes PUBLISHED in the same flow — live and indexed immediately — with
    ``moderation_status`` still PENDING and ``listing.submitted`` still
    emitted, so review happens on the live content and a rejecting verdict
    takes it down (PUBLISHED -> BLOCKED).

    **Re-publishing a LIVE listing** — an owner editing something already
    published — is post-moderation and rides the moderation axis alone: the
    lifecycle stays PUBLISHED, ``moderation_status`` goes back to PENDING, and
    the edit is visible immediately. Before this, ``publish_listing`` assigned
    ``status = PENDING`` directly (past the FSM, so no event at all), and the
    listing silently vanished from ``Listing.objects.published()`` and from
    every search index for as long as re-moderation took — a takedown in all
    but name, applied before anyone had looked at the content. A rejecting
    verdict now lands where takedowns already land: ``apply_moderation
    ("rejected")`` -> PUBLISHED -> BLOCKED, which emits ``listing.removed``.

    The ``listing.updated`` an index needs for the new content is emitted by
    ``Listing.save()`` itself (it compares the promoted fields against the
    stored row), so a re-publish that moves no indexed field announces
    nothing — one detector, no second call site that could disagree with it.
    """
    was_indexed = listing.status in INDEXED_STATUSES
    # Same side of the door as the location and image checks: a storefront
    # that skipped validate-draft must not be able to publish a category-less
    # draft anyway. The view maps this to ERR_400_PUBLISH_VALIDATION_FAILED,
    # the code the structured result above also carries.
    if _missing_category_result(listing) is not None:
        raise ValidationError("A category is required to publish a listing.")
    configs = category_schema.get_feature_configs(listing.category_id)
    features_draft = listing.features_draft or {}

    if features_draft:
        validate_dto(configs, features_draft)  # raises on invalid

    # Д71, enforced on the same side of the door as the image check: a
    # storefront that skipped validate-draft must not be able to publish a
    # placeless listing anyway. Both gates read one predicate
    # (``_missing_location_result``), so the structured answer the composer
    # renders and the refusal the write path issues cannot drift apart.
    if _missing_location_result(listing) is not None:
        raise ValidationError("A location is required to publish a listing.")

    # Promote draft -> published fields. The four attribute projections come
    # from ``build_projections`` — the single definition of what they are,
    # shared with ``services.reproject`` so a refreshed snapshot and a freshly
    # published one can never mean different things.
    for field, value in build_projections(configs, features_draft).items():
        setattr(listing, field, value)
    listing.title = listing.title_draft or listing.title
    listing.description = listing.description_draft
    listing.location_id = listing.location_id_draft
    # Д76: the CARD's location line is derived from the pin, not echoed back
    # from the client. Fail-soft to the supplied string — see
    # ``services/location.py`` for why a dark geocoder must not block a
    # publish, and why the picker's own line stays on the draft twin.
    listing.location_label = (
        resolve_place_label(listing.lat_draft, listing.lon_draft)
        or listing.location_label_draft
    )
    listing.geohash = listing.geohash_draft
    listing.lat = listing.lat_draft
    listing.lon = listing.lon_draft
    # Unconditional, like every other draft twin: a cleared price publishes as
    # NULL («Цена не указана»), not as the previously published number. The
    # old ``if price_draft is not None`` guard existed only because price had
    # ``default=0`` and a None promote would have been destructive.
    listing.price = listing.price_draft

    images_draft = listing.images_draft or []
    if listings_settings.REQUIRE_IMAGE_ON_PUBLISH and not images_draft:
        raise ValidationError("At least one image is required to publish a listing.")
    listing.images = images_draft

    ttl_days = listings_settings.DEFAULT_LISTING_TTL_DAYS
    if ttl_days:
        listing.expires_at = timezone.now() + timedelta(days=int(ttl_days))
    listing.expiry_notification_sent = False

    if not was_indexed:
        # First publication: nothing is public yet, so the lifecycle waits for
        # the verdict. A live listing keeps its status — see the docstring.
        listing.status = ListingStatus.PENDING
    listing.moderation_status = ModerationStatus.PENDING
    listing.moderation_note = ""

    from .. import events

    # The promotion write and the moderation-request emit commit together: a
    # listing must never reach moderation PENDING without the
    # listing.submitted event a moderation module needs (nor emit for a
    # promotion that rolled back). ``save()`` raises its own listing.updated
    # inside this block when the promoted content actually moved on a live
    # listing; it joins the same transaction.
    with mutate_and_emit():
        listing.save()
        events.emit_listing_submitted(listing)
        if listings_settings.AUTO_APPROVE_ON_PUBLISH:
            listing.apply_moderation("approved", note="auto-approved (no moderation module)")
        elif not was_indexed and listings_settings.MODERATION_GATE == "post":
            # Post-moderation gate: the FIRST publication goes live in the
            # same transaction that requested review. Deliberately NOT
            # ``apply_moderation("approved")`` — nothing has been approved;
            # ``moderation_status`` stays PENDING and the listing.submitted
            # above still opens the case, so a verdict is still owed and a
            # rejecting one lands on the PUBLISHED -> BLOCKED takedown edge
            # exactly as it does for a re-moderated live edit. transition_to
            # emits the listing.published a search index needs. A live
            # listing (``was_indexed``) never reaches this branch: the
            # re-publish path above already kept it PUBLISHED.
            listing.transition_to(ListingStatus.PUBLISHED)

    logger.info(
        "listing %s submitted for moderation (status %s)", listing.pk, listing.status
    )


def restore_listing(listing) -> None:
    """Bring an ARCHIVED listing back on sale — «опубликовать снова» (Д193).

    Raises ``django.core.exceptions.ValidationError`` on an invalid draft, for
    the same reasons and with the same message ``publish_listing`` does: this
    IS a publication, and the seller pressing one button in the cabinet gets
    the composer's refusal rather than a listing that goes back to the window
    with no photo on it.

    Deliberately not ``listing.transition_to(PUBLISHED)``. Three things follow
    from restoring through the publish path instead:

    * **Re-moderation.** ``listing.submitted`` is emitted and
      ``moderation_status`` goes back to PENDING, so a fleet whose moderation
      is wired to publish reviews the restored content exactly as it reviews a
      fresh one. Where the lifecycle lands is that policy's answer, not this
      function's: PENDING under the default ``MODERATION_GATE="pre"``,
      PUBLISHED under ``"post"`` or ``AUTO_APPROVE_ON_PUBLISH``.
    * **No takedown laundering.** ARCHIVED is reachable from BLOCKED, so a
      bare FSM hop to PUBLISHED would hand every taken-down listing a
      two-press route back into the index, around the gate that keeps
      BLOCKED -> PUBLISHED out of ``OWNER_TRANSITIONS`` entirely.
    * **The content is current.** Draft twins are promoted, projections are
      rebuilt and the TTL is restarted, so a listing that spent six months
      archived does not come back with an expiry date in the past.

    The caller reports ``listing.status`` after this returns; it is not
    necessarily the status that was asked for.
    """
    publish_listing(listing)


def is_valid(result: ValidationBatchResult) -> bool:
    """Convenience: whether every result entry is OK."""
    return result.valid and all(
        r.status == ValidationStatus.OK for r in result.results
    )
