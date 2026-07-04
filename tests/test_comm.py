"""comm surface: emits schema-validated, the status Function, and round-trips."""
import json
from pathlib import Path

import jsonschema
import pytest

import stapel_listings
from stapel_listings.models import Listing, ListingStatus, ModerationStatus
from stapel_listings.services import category_schema, publish as publish_service

pytestmark = pytest.mark.django_db

SCHEMAS = Path(stapel_listings.__file__).parent / "schemas"


def _schema(kind, name):
    return json.loads((SCHEMAS / kind / f"{name}.json").read_text())


# --- emits are schema-valid ----------------------------------------------


def test_publish_emits_submitted_matching_schema(draft_listing, capture_events):
    submitted = capture_events("listing.submitted")
    publish_service.publish_listing(draft_listing)
    assert len(submitted) == 1
    jsonschema.validate(submitted[0].payload, _schema("emits", "listing.submitted"))


def test_published_payload_carries_search_projection(user, capture_events):
    published = capture_events("listing.published")
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING,
        features_search={"mileage": [42000]},
    )
    listing.transition_to(ListingStatus.PUBLISHED)
    jsonschema.validate(published[0].payload, _schema("emits", "listing.published"))
    assert published[0].payload["features_search"] == {"mileage": [42000]}


# --- listings.status Function --------------------------------------------


def test_status_function(user):
    from stapel_core.comm import call

    listing = Listing.objects.create(owner=user, category_id="7")
    result = call("listings.status", {"listing_id": listing.pk})
    assert result["status"] == ListingStatus.DRAFT
    assert result["owner_id"] == str(user.id)
    assert result["is_active"] is False


def test_status_function_unknown_listing_raises():
    from stapel_core.comm import call
    from stapel_core.comm import FunctionCallError

    with pytest.raises(FunctionCallError):
        call("listings.status", {"listing_id": 999999})


def test_status_function_request_schema_enforced():
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        call("listings.status", {"wrong": "field"})


# --- moderation round-trip: submit -> completed -> flip ------------------


def test_moderation_round_trip_approved(draft_listing, capture_events):
    from stapel_core.comm import emit

    submitted = capture_events("listing.submitted")
    published = capture_events("listing.published")

    publish_service.publish_listing(draft_listing)
    assert len(submitted) == 1
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.PENDING

    # A moderation module replies (schema-validated by conftest-registered consumes schema).
    emit("moderation.completed", {"listing_id": draft_listing.pk, "decision": "approved"})

    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.PUBLISHED
    assert draft_listing.moderation_status == ModerationStatus.APPROVED
    assert len(published) == 1


def test_moderation_round_trip_rejected(draft_listing):
    from stapel_core.comm import emit

    publish_service.publish_listing(draft_listing)
    emit("moderation.completed", {"listing_id": draft_listing.pk, "decision": "rejected", "note": "nope"})
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.REJECTED
    assert draft_listing.moderation_note == "nope"


def test_moderation_completed_bad_payload_rejected(draft_listing):
    from stapel_core.comm import emit
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        emit("moderation.completed", {"listing_id": draft_listing.pk, "decision": "maybe"})


# --- category.changed invalidates the cached feature configs -------------


def test_category_changed_invalidates_config_cache(stub_categories):
    from stapel_core.comm import emit

    # Warm the cache.
    configs = category_schema.get_feature_configs("7")
    assert len(configs) == 2

    # Mutate the stub schema and announce the change.
    stub_categories.append(
        {"id": 3, "slug": "color", "name": "Color", "mandatory": False,
         "config": {"type": "string"}}
    )
    emit("category.changed", {"category_id": 7, "revision": 2})

    refreshed = category_schema.get_feature_configs("7")
    assert len(refreshed) == 3
