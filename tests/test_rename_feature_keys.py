"""``listings.rename_feature_keys`` — the write half of a slug rename.

The defect these pin, measured on a live fleet on 2026-09-05: a catalogue
import renamed five feature slugs in place (``make_ref_select`` → ``make`` and
four more of the same shape). The category schema moved; the stored answers did
not. Every draft kept its answers under the OLD keys, so the facet emptied, the
search projection lost the values, and ``listings_reproject_features`` — which
keys on the CURRENT slugs — would have DROPPED them rather than repaired them.

So the tests below hold four things: the keys move and the values do not; a
second run is a no-op; a draft answering both slugs is never guessed at; and a
dry run writes nothing at all.
"""
import pytest
from django.core.cache import cache
from django.core.management import call_command

from stapel_core.comm import call
from stapel_listings.models import Listing, ListingStatus
from stapel_listings.services import publish as publish_service
from stapel_listings.services.rename_features import (
    RenameError,
    rename_draft,
    rename_feature_keys,
    validate_renames,
)

pytestmark = pytest.mark.django_db

RENAMES = {"mileage": "odometer"}


@pytest.fixture(autouse=True)
def clean_schema_cache():
    """The schema is memoized by revision in a process-wide locmem cache and
    these tests reshape it. Start cold, end cold."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def renamed_schema(stub_categories):
    """The category AFTER the rename: ``mileage`` is now ``odometer``.

    This is the whole point of the pass — the schema has already moved, and the
    listing's stored answer is filed under a key the schema no longer knows.
    """
    for feature_def in stub_categories:
        if feature_def["slug"] == "mileage":
            feature_def["slug"] = "odometer"
    return stub_categories


@pytest.fixture
def live_listing(draft_listing):
    """``draft_listing`` published and approved — a listing the public sees."""
    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")
    assert draft_listing.status == ListingStatus.PUBLISHED
    return draft_listing


def _projected_slugs(listing) -> set:
    return {row["slug"] for row in (listing.features or []) if row.get("slug")}


# --- the rename itself ----------------------------------------------------


def test_key_moves_and_the_value_does_not(live_listing, renamed_schema):
    stored = live_listing.features_draft["mileage"]

    result = rename_feature_keys(category_id="7", renames=RENAMES)

    live_listing.refresh_from_db()
    assert "mileage" not in live_listing.features_draft
    assert live_listing.features_draft["odometer"] == stored
    assert result["listings_changed"] == 1
    assert result["keys_renamed"] == 1
    assert result["conflicts"] == []


def test_the_projection_follows_the_key(live_listing, renamed_schema):
    """Without this the repair is half done: the row is right and the card,
    the facet and the search document still show the loss."""
    assert "mileage" in _projected_slugs(live_listing)

    rename_feature_keys(category_id="7", renames=RENAMES)

    live_listing.refresh_from_db()
    assert _projected_slugs(live_listing) == {"odometer", "condition"}
    assert live_listing.features_search.get("odometer")


def test_the_index_is_told(live_listing, renamed_schema, capture_events):
    """A search index still serving a document without the renamed values is
    the half of the damage nobody would otherwise see."""
    updated = capture_events("listing.updated")

    rename_feature_keys(category_id="7", renames=RENAMES)

    assert [event.payload["listing_id"] for event in updated] == [live_listing.pk]


def test_key_order_is_preserved(live_listing, renamed_schema):
    """A renamed key keeps its position: the composer's per-field sidecar and
    every human reading the JSON take the order as the order it was answered."""
    before = list(live_listing.features_draft)

    rename_feature_keys(category_id="7", renames=RENAMES)

    live_listing.refresh_from_db()
    assert list(live_listing.features_draft) == [
        "odometer" if slug == "mileage" else slug for slug in before
    ]


# --- idempotence ----------------------------------------------------------


def test_second_run_changes_nothing_and_emits_nothing(
    live_listing, renamed_schema, capture_events
):
    rename_feature_keys(category_id="7", renames=RENAMES)
    live_listing.refresh_from_db()
    draft_after_first = dict(live_listing.features_draft)
    updated = capture_events("listing.updated")

    result = rename_feature_keys(category_id="7", renames=RENAMES)

    live_listing.refresh_from_db()
    assert live_listing.features_draft == draft_after_first
    assert result["listings_changed"] == 0
    assert result["keys_renamed"] == 0
    assert updated == []


# --- the collision --------------------------------------------------------


def test_a_draft_answering_both_slugs_is_never_guessed_at(live_listing, renamed_schema):
    """Overwriting one answer with the other would be a silent edit of a
    seller's data on a run whose whole point is to stop being silent."""
    Listing.objects.filter(pk=live_listing.pk).update(
        features_draft={
            "mileage": {"type": "int", "value": 42000},
            "odometer": {"type": "int", "value": 11000},
            "condition": {"type": "select", "value": ["used"]},
        }
    )

    result = rename_feature_keys(category_id="7", renames=RENAMES)

    live_listing.refresh_from_db()
    assert live_listing.features_draft["mileage"]["value"] == 42000
    assert live_listing.features_draft["odometer"]["value"] == 11000
    assert result["listings_changed"] == 0
    assert result["conflicts"] == [
        {"listing_id": str(live_listing.pk), "old": "mileage", "new": "odometer"}
    ]


# --- the dry run ----------------------------------------------------------


def test_dry_run_writes_nothing(live_listing, renamed_schema, capture_events):
    before = dict(live_listing.features_draft)
    before_features = list(live_listing.features)
    updated = capture_events("listing.updated")

    result = rename_feature_keys(category_id="7", renames=RENAMES, dry_run=True)

    live_listing.refresh_from_db()
    assert live_listing.features_draft == before
    assert live_listing.features == before_features
    assert updated == []
    assert result["dry_run"] is True
    # …and it still REPORTS what a real run would do, or it would be useless.
    assert result["listings_changed"] == 1
    assert result["keys_renamed"] == 1
    assert result["reprojected"] is None


# --- scope ----------------------------------------------------------------


def test_another_category_is_out_of_scope(live_listing, renamed_schema, user):
    elsewhere = Listing.objects.create(
        owner=user, category_id="99",
        features_draft={"mileage": {"type": "int", "value": 1}},
    )

    rename_feature_keys(category_id="7", renames=RENAMES)

    elsewhere.refresh_from_db()
    assert "mileage" in elsewhere.features_draft


def test_no_children_provider_covers_the_named_category_and_says_so(
    live_listing, renamed_schema
):
    """Degrading is fine; implying a subtree that was never walked is not."""
    result = rename_feature_keys(category_id="7", renames=RENAMES)

    assert result["subtree_resolved"] is False
    assert result["categories"] == ["7"]
    assert result["listings_changed"] == 1


def test_the_subtree_is_walked_when_a_provider_answers(user, stub_categories):
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    child = Listing.objects.create(
        owner=user, category_id="8",
        features_draft={"mileage": {"type": "int", "value": 7}},
    )
    tree = {"7": [{"id": 8, "slug": "child", "name": "Child", "children_count": 0}]}

    def provider(payload):
        parent = str(payload.get("parent_id"))
        return {"parent_id": payload.get("parent_id"),
                "children": tree.get(parent, [])}

    register_function("categories.children", provider)
    try:
        result = rename_feature_keys(category_id="7", renames=RENAMES)
    finally:
        function_registry._providers.pop("categories.children", None)
        function_registry._schemas.pop("categories.children", None)

    child.refresh_from_db()
    assert result["subtree_resolved"] is True
    assert result["categories"] == ["7", "8"]
    assert "odometer" in child.features_draft


# --- the rename map itself ------------------------------------------------


@pytest.mark.parametrize("renames", [
    {},
    {"a": "a"},
    {"a": "c", "b": "c"},
    {"a": "b", "b": "c"},
    {"": "b"},
])
def test_a_map_that_cannot_mean_anything_is_refused(renames):
    with pytest.raises(RenameError):
        validate_renames(renames)


def test_refusal_happens_before_a_single_row_is_read(live_listing):
    with pytest.raises(RenameError):
        rename_feature_keys(category_id="7", renames={"a": "a"})


def test_rename_draft_is_pure():
    draft = {"a": 1, "b": 2}
    out, renamed, conflicts = rename_draft(draft, {"a": "z"})
    assert draft == {"a": 1, "b": 2}
    assert out == {"z": 1, "b": 2}
    assert renamed == ["a"]
    assert conflicts == []


# --- the seams a caller uses ----------------------------------------------


def test_comm_function_answers_the_documented_counts(live_listing, renamed_schema):
    result = call(
        "listings.rename_feature_keys",
        {"category_id": 7, "renames": RENAMES, "dry_run": False},
    )

    assert result["listings_scanned"] == 1
    assert result["listings_changed"] == 1
    assert result["keys_renamed"] == 1
    assert result["conflicts"] == []
    live_listing.refresh_from_db()
    assert "odometer" in live_listing.features_draft


def test_management_command_renames_and_reports(live_listing, renamed_schema, capsys):
    call_command(
        "listings_rename_feature_keys",
        "--category", "7", "--rename", "mileage=odometer",
    )

    out = capsys.readouterr().out
    assert "mileage → odometer" in out
    assert "renamed 1 key(s)" in out
    live_listing.refresh_from_db()
    assert "odometer" in live_listing.features_draft


def test_management_command_dry_run_writes_nothing(
    live_listing, renamed_schema, capsys
):
    call_command(
        "listings_rename_feature_keys",
        "--category", "7", "--rename", "mileage=odometer", "--dry-run",
    )

    assert "DRY RUN" in capsys.readouterr().out
    live_listing.refresh_from_db()
    assert "mileage" in live_listing.features_draft
