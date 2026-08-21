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
