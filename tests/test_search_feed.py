"""The search seam: pull Functions, the live listing.updated, projection freshness.

Three defects are gated here, each of which made an index stale by
construction before 0.4:

1. there was no way to READ a listing's document — the events carry identity
   only, and an indexer may not touch this module's database;
2. ``listing.updated`` had zero call sites, so an edit of a live listing
   reached nobody;
3. ``features_search`` was rebuilt at exactly one call site (publish), so a
   PAUSED -> PUBLISHED republish re-announced the projection built weeks ago.
"""
import json
from pathlib import Path

import jsonschema
import pytest

import stapel_listings
from stapel_listings.models import Listing, ListingStatus
from stapel_listings.services import publish as publish_service

pytestmark = pytest.mark.django_db

SCHEMAS = Path(stapel_listings.__file__).parent / "schemas"


def _schema(kind, name):
    return json.loads((SCHEMAS / kind / f"{name}.json").read_text())


# --- listings.search_documents -------------------------------------------


def test_search_documents_round_trip(draft_listing):
    """Publish -> approve -> the document an indexer pulls carries the content."""
    from stapel_core.comm import call

    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")

    result = call("listings.search_documents", {"keys": [str(draft_listing.pk)]})
    doc = result[str(draft_listing.pk)]

    assert doc["title"] == "Toyota Camry"
    assert doc["description"] == "A well kept car in great condition."
    assert doc["category_id"] == "7"
    assert doc["owner_id"] == str(draft_listing.owner_id)
    assert doc["status"] == ListingStatus.PUBLISHED
    assert doc["images"] == ["product/abc123"]
    assert doc["features_search"] == {"mileage": [42000], "condition": ["used"]}
    # Money and coordinates travel as strings — a float would round a price.
    assert doc["price"] == "15000.00"
    assert isinstance(doc["price_base"], str)
    # The whole payload must survive a JSON round trip over a bus.
    assert json.loads(json.dumps(doc)) == doc


def test_search_documents_is_a_keyed_batch(user, draft_listing):
    from stapel_core.comm import call

    other = Listing.objects.create(owner=user, category_id="9", title="Second")
    result = call(
        "listings.search_documents",
        {"keys": [draft_listing.pk, other.pk, 999999]},
    )
    assert set(result) == {str(draft_listing.pk), str(other.pk)}


def test_search_documents_omits_soft_deleted(draft_listing):
    """A key with no document is how the indexer learns to drop it."""
    from stapel_core.comm import call

    draft_listing.delete()
    assert call("listings.search_documents", {"keys": [draft_listing.pk]}) == {}


def test_search_documents_request_schema_enforced():
    from stapel_core.comm import call
    from stapel_core.comm.exceptions import SchemaValidationError

    with pytest.raises(SchemaValidationError):
        call("listings.search_documents", {"key": 1})


# --- listings.search_export ----------------------------------------------


def test_search_export_snapshot_contract(user):
    """{cursor, limit} -> {rows, cursor, total}, paged to exhaustion."""
    from stapel_core.comm import call

    for n in range(5):
        Listing.objects.create(owner=user, category_id="7", title=f"L{n}")

    seen, cursor, pages = [], None, 0
    while True:
        page = call("listings.search_export", {"cursor": cursor, "limit": 2})
        assert page["total"] == 5
        seen.extend(page["rows"])
        pages += 1
        cursor = page["cursor"]
        if not cursor:
            break
        assert pages < 10, "cursor never exhausted"

    assert len(seen) == 5
    assert [r["key"] for r in seen] == sorted(r["key"] for r in seen)
    row = seen[0]
    # Every row carries its source key and an ordering token, and is the same
    # document shape the keyed batch returns.
    assert row["seq"] > 0
    assert row["title"] == "L0"
    assert "features_search" in row


def test_search_export_and_search_documents_agree(draft_listing):
    """A rebuilt index and a live-updated one cannot disagree about a listing."""
    from stapel_core.comm import call

    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")

    exported = call("listings.search_export", {})["rows"][0]
    pulled = call("listings.search_documents", {"keys": [draft_listing.pk]})[
        str(draft_listing.pk)
    ]
    assert {k: v for k, v in exported.items() if k not in ("key", "seq")} == pulled


def test_search_export_defaults_to_the_first_page(user):
    from stapel_core.comm import call

    Listing.objects.create(owner=user, category_id="7")
    page = call("listings.search_export", {})
    assert page["cursor"] is None and page["total"] == 1


# --- listing.updated now has a live call site ----------------------------


def test_editing_a_published_listing_emits_updated(draft_listing, capture_events):
    """The defect: re-publishing a LIVE listing reached no index at all."""
    updated = capture_events("listing.updated")

    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")
    assert draft_listing.status == ListingStatus.PUBLISHED
    assert updated == []

    draft_listing.title_draft = "Toyota Camry 2019"
    draft_listing.description_draft = "Now with a new description entirely."
    draft_listing.save()
    publish_service.publish_listing(draft_listing)

    assert len(updated) == 1
    jsonschema.validate(updated[0].payload, _schema("emits", "listing.updated"))
    assert updated[0].payload["listing_id"] == draft_listing.pk
    draft_listing.refresh_from_db()
    assert draft_listing.title == "Toyota Camry 2019"


def test_first_publish_of_a_draft_emits_no_updated(draft_listing, capture_events):
    """listing.submitted covers the first publication — nothing was indexed."""
    updated = capture_events("listing.updated")
    publish_service.publish_listing(draft_listing)
    assert updated == []


def test_direct_content_write_on_a_live_listing_emits_updated(user, capture_events):
    """Any write path, not only the publish service: admin, script, migration."""
    updated = capture_events("listing.updated")
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING, title="Old"
    )
    listing.transition_to(ListingStatus.PUBLISHED)
    assert updated == []

    listing.title = "New"
    listing.save(update_fields=["title", "updated_at"])
    assert len(updated) == 1


def test_status_only_write_on_a_live_listing_emits_no_updated(user, capture_events):
    """A transition owns its own event — one write must not raise two."""
    updated = capture_events("listing.updated")
    published = capture_events("listing.published")
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    listing.transition_to(ListingStatus.PUBLISHED)
    assert len(published) == 1 and updated == []


def test_failing_updated_emit_rolls_back_the_edit(user, monkeypatch):
    """Same atomicity rule as every other listing.* event."""
    from stapel_listings import events

    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING, title="Old"
    )
    listing.transition_to(ListingStatus.PUBLISHED)

    def boom(_listing):
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "emit_listing_updated", boom)
    listing.title = "New"
    with pytest.raises(RuntimeError):
        listing.save(update_fields=["title", "updated_at"])

    listing.refresh_from_db()
    assert listing.title == "Old"


# --- features_search is derived, not built once --------------------------


def test_republish_from_paused_carries_a_fresh_projection(draft_listing, capture_events):
    """paused -> published used to re-announce the projection built at publish."""
    published = capture_events("listing.published")

    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")
    draft_listing.transition_to(ListingStatus.PAUSED)

    # Something changed the attribute projection while the listing was paused.
    draft_listing.features = [
        {"slug": "mileage", "type": "int", "value": 51000},
        {"slug": "condition", "type": "select", "value": ["new"]},
    ]
    draft_listing.save(update_fields=["features", "updated_at"])

    draft_listing.transition_to(ListingStatus.PUBLISHED)

    assert published[-1].payload["features_search"] == {
        "mileage": [51000],
        "condition": ["new"],
    }


def test_features_search_is_re_derived_on_any_write_of_features(user):
    listing = Listing.objects.create(owner=user, category_id="7")
    listing.features = [{"slug": "mileage", "type": "int", "value": 7}]
    listing.save(update_fields=["features", "updated_at"])

    listing.refresh_from_db()
    assert listing.features_search == {"mileage": [7]}


def test_features_search_matches_the_publish_time_build(draft_listing):
    """The two derivations — publish-time DAO dict and stored list — agree."""
    from stapel_listings.services.features import build_features_search_from_list

    publish_service.publish_listing(draft_listing)
    assert build_features_search_from_list(draft_listing.features) == (
        draft_listing.features_search
    )
