"""Publish workflow: promote a draft to a moderated, indexable listing.

Ported from legacy-catalog ``ads/services/publish_ad.py``, decoupled:

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

from stapel_attributes import normalize_to_dao, validate_description, validate_dto
from stapel_attributes.results import ValidationBatchResult, ValidationStatus
from stapel_attributes import validate_dto_structured

from ..conf import listings_settings
from ..models import ListingStatus, ModerationStatus
from . import category_schema
from .features import (
    build_features_badges,
    build_features_list,
    build_features_search,
    build_features_title,
    get_consecutive_header_pairs,
)

logger = logging.getLogger(__name__)


def validate_draft(listing) -> ValidationBatchResult:
    """Structured validation of a listing's draft against its category schema.

    Combines feature-value validation (via the comm-fetched configs) with the
    free-text description length check. Used by the validate/publish views to
    return machine-readable results.
    """
    configs = category_schema.get_feature_configs(listing.category_id)
    result = validate_dto_structured(configs, listing.features_draft or {})

    desc_error = validate_description(
        listing.description_draft,
        min_length=listings_settings.DESCRIPTION_MIN_LENGTH,
        max_length=listings_settings.DESCRIPTION_MAX_LENGTH,
    )
    if desc_error is not None:
        result.results.insert(0, desc_error)
        result.valid = False
    return result


def publish_listing(listing) -> None:
    """Validate the draft, build projections, promote fields, request moderation.

    Raises ``django.core.exceptions.ValidationError`` when the draft is invalid
    or (per policy) an image is missing.
    """
    configs = category_schema.get_feature_configs(listing.category_id)
    features_draft = listing.features_draft or {}

    if features_draft:
        validate_dto(configs, features_draft)  # raises on invalid
        features_dao_dict = normalize_to_dao(configs, features_draft)
    else:
        features_dao_dict = {}

    consecutive_headers = get_consecutive_header_pairs(configs)
    features_list = build_features_list(features_dao_dict, consecutive_headers)

    # Promote draft -> published fields.
    listing.features = features_list
    listing.features_title = build_features_title(features_list)
    listing.features_badges = build_features_badges(features_list)
    listing.features_search = build_features_search(features_dao_dict)
    listing.title = listing.title_draft or listing.title
    listing.description = listing.description_draft
    listing.location_id = listing.location_id_draft
    listing.location_label = listing.location_label_draft
    listing.geohash = listing.geohash_draft
    if listing.price_draft is not None:
        listing.price = listing.price_draft

    images_draft = listing.images_draft or []
    if listings_settings.REQUIRE_IMAGE_ON_PUBLISH and not images_draft:
        raise ValidationError("At least one image is required to publish a listing.")
    listing.images = images_draft

    ttl_days = listings_settings.DEFAULT_LISTING_TTL_DAYS
    if ttl_days:
        listing.expires_at = timezone.now() + timedelta(days=int(ttl_days))
    listing.expiry_notification_sent = False

    listing.status = ListingStatus.PENDING
    listing.moderation_status = ModerationStatus.PENDING
    listing.moderation_note = ""
    listing.save()

    # Request moderation (transactional-outbox emit). A future
    # stapel-moderation module consumes this and replies with moderation.completed.
    from .. import events

    events.emit_listing_submitted(listing)
    logger.info("listing %s submitted for moderation", listing.pk)

    if listings_settings.AUTO_APPROVE_ON_PUBLISH:
        listing.apply_moderation("approved", note="auto-approved (no moderation module)")


def is_valid(result: ValidationBatchResult) -> bool:
    """Convenience: whether every result entry is OK."""
    return result.valid and all(
        r.status == ValidationStatus.OK for r in result.results
    )
