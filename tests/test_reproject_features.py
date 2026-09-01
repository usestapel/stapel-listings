"""``listings_reproject_features`` — refreshing the write-time projection snapshot.

The snapshot ``publish_listing`` takes is only as fresh as the last publish.
Everything published before stapel-attributes 0.7.0 carries ``select`` DAOs
with no ``labels``, so its cards print storage slugs; the same staleness has
always applied to ``ref_select`` and to any category whose option copy an
owner edits. These tests pin the repair pass and, just as importantly, pin
what it must NOT move.
"""
import pytest
from django.core.cache import cache
from django.core.management import call_command

from stapel_listings.models import Listing, ListingStatus, ModerationStatus
from stapel_listings.services import publish as publish_service
from stapel_listings.services.reproject import reproject_listings

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clean_schema_cache():
    """``category_schema`` caches configs by revision in a process-wide locmem
    cache; these tests reshape the schema, so start each one from cold."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def live_listing(draft_listing):
    """``draft_listing`` published and approved — a listing the public sees."""
    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")
    assert draft_listing.status == ListingStatus.PUBLISHED
    return draft_listing


def _drop_labels(daos):
    return [{k: v for k, v in dao.items() if k != "labels"} for dao in (daos or [])]


def _as_pre_070(listing) -> Listing:
    """Rewrite the stored projections into their pre-0.7.0 shape — ``select``
    DAOs carrying ``value`` and no ``labels``.

    Written through a queryset ``.update()`` so ``Listing.save()`` never runs:
    that is exactly what a row published by an older engine looks like in the
    database today, and going through ``save()`` would both re-derive things
    and emit an event that has nothing to do with the test.
    """
    Listing.all_objects.filter(pk=listing.pk).update(
        features=_drop_labels(listing.features),
        features_title=_drop_labels(listing.features_title),
        features_badges=_drop_labels(listing.features_badges),
    )
    listing.refresh_from_db()
    return listing


def _badge(listing):
    return listing.features_badges[0]


def _reregister(register_function, provider):
    """Swap the ``categories.features`` provider mid-test.

    ``register_function`` enforces one provider per name (deliberately), and
    ``stub_categories`` has already claimed it — so drop the incumbent first.
    """
    from stapel_core.comm.registry import function_registry

    function_registry._providers.pop("categories.features", None)
    register_function("categories.features", provider)


# A draft that satisfies conftest's two-feature schema (mandatory int
# ``mileage`` shown in the title, optional single-select ``condition`` shown
# as a badge).
def _draft(condition="new", mileage=42000):
    return {
        "mileage": {"type": "int", "value": mileage},
        "condition": {"type": "select", "value": [condition]},
    }


class TestLabelRepair:
    def test_a_select_dao_without_labels_gains_them(self, live_listing):
        legacy = _as_pre_070(live_listing)
        assert "labels" not in _badge(legacy)

        result = reproject_listings()

        legacy.refresh_from_db()
        badge = _badge(legacy)
        assert badge["slug"] == "condition"
        # ``value`` is untouched — it is the filter/search axis, and the
        # repair adds the display copy beside it, it does not replace it.
        assert badge["value"] == ["used"]
        assert badge["labels"] == ["cond.used"]
        assert result["examined"] == 1
        assert result["changed"] == 1
        assert result["unchanged"] == 0
        assert result["skipped"] == 0

    def test_the_repair_reaches_every_one_of_the_four_projections(self, live_listing):
        legacy = _as_pre_070(live_listing)

        reproject_listings()

        legacy.refresh_from_db()
        stored = next(d for d in legacy.features if d["slug"] == "condition")
        assert stored["labels"] == ["cond.used"]
        assert _badge(legacy)["labels"] == ["cond.used"]
        # features_search keeps the CODES, never the labels: the index filters
        # on values, and a label is a display snapshot in one language.
        assert legacy.features_search["condition"] == ["used"]

    def test_a_freshly_published_listing_is_already_current(self, live_listing):
        """The single-definition property: publish and re-projection derive the
        projections through the same ``build_projections``, so a listing
        published by this release is examined and found unchanged."""
        result = reproject_listings()

        assert result["examined"] == 1
        assert result["changed"] == 0
        assert result["unchanged"] == 1

    def test_rerun_changes_nothing(self, live_listing):
        _as_pre_070(live_listing)

        first = reproject_listings()
        second = reproject_listings()

        assert first["changed"] == 1
        assert second["examined"] == 1
        assert second["changed"] == 0
        assert second["unchanged"] == 1

    def test_a_listing_with_no_projections_is_not_examined(self, draft_listing):
        """A never-published draft has nothing to re-project — it is outside
        the population, not a skip."""
        assert draft_listing.features in (None, [])

        result = reproject_listings()

        assert result["examined"] == 0


class TestDryRun:
    def test_dry_run_writes_nothing_but_counts_the_change(self, live_listing):
        legacy = _as_pre_070(live_listing)

        result = reproject_listings(dry_run=True)

        legacy.refresh_from_db()
        assert "labels" not in _badge(legacy)
        assert result["dry_run"] is True
        assert result["examined"] == 1
        assert result["changed"] == 1

    def test_dry_run_emits_no_event(self, live_listing, capture_events):
        _as_pre_070(live_listing)
        updated = capture_events("listing.updated")

        result = reproject_listings(dry_run=True)

        assert updated == []
        # It still REPORTS the events a real run would produce, so the dry run
        # is a preview of the index traffic too.
        assert result["events_emitted"] == 1


class TestNothingElseMoves:
    def test_lifecycle_moderation_expiry_and_timestamps_are_untouched(
        self, live_listing
    ):
        legacy = _as_pre_070(live_listing)
        legacy.moderation_note = "checked by hand"
        legacy.save(update_fields=["moderation_note"])

        frozen = ("status", "moderation_status", "moderation_note", "expires_at",
                  "published_at", "created_at", "updated_at", "title",
                  "description", "price", "images", "features_draft")
        before = Listing.all_objects.filter(pk=legacy.pk).values(*frozen).first()

        result = reproject_listings()

        after = Listing.all_objects.filter(pk=legacy.pk).values(*frozen).first()
        assert before == after
        assert after["status"] == ListingStatus.PUBLISHED
        assert after["moderation_status"] == ModerationStatus.APPROVED
        assert result["changed"] == 1  # ...and it really did rewrite the projections

    def test_no_listing_submitted_it_is_not_a_republication(
        self, live_listing, capture_events
    ):
        _as_pre_070(live_listing)
        submitted = capture_events("listing.submitted")
        published = capture_events("listing.published")
        removed = capture_events("listing.removed")

        reproject_listings()

        assert submitted == []
        assert published == []
        assert removed == []


class TestIndexEvent:
    """The decision, asserted rather than assumed: ``listing.updated`` IS
    wanted here — a search index holding the stale text is exactly what this
    command exists to repair — so the write goes row by row through
    ``save(update_fields=...)`` and not through ``bulk_update``."""

    def test_changed_indexed_listing_emits_listing_updated(
        self, live_listing, capture_events
    ):
        _as_pre_070(live_listing)
        updated = capture_events("listing.updated")

        result = reproject_listings()

        assert len(updated) == 1
        assert updated[0].payload["listing_id"] == live_listing.pk
        assert result["events_emitted"] == len(updated)

    def test_unchanged_listing_emits_nothing(self, live_listing, capture_events):
        updated = capture_events("listing.updated")

        result = reproject_listings()

        assert result["unchanged"] == 1
        assert updated == []
        assert result["events_emitted"] == 0

    def test_a_non_indexed_listing_is_repaired_without_announcing_it(
        self, live_listing, capture_events
    ):
        """A PAUSED listing is not in any index, so there is nothing to tell —
        but its row is repaired all the same, ready for the day it comes back."""
        _as_pre_070(live_listing)
        live_listing.transition_to(ListingStatus.PAUSED)
        updated = capture_events("listing.updated")

        result = reproject_listings()

        live_listing.refresh_from_db()
        assert _badge(live_listing)["labels"] == ["cond.used"]
        assert result["changed"] == 1
        assert result["events_emitted"] == 0
        assert updated == []


class TestSkips:
    def test_a_gone_category_is_counted_and_skipped_not_crashed(
        self, live_listing, stub_categories
    ):
        from stapel_core.comm import register_function

        _as_pre_070(live_listing)
        Listing.all_objects.filter(pk=live_listing.pk).update(category_id="999")

        def gone(payload):
            raise LookupError(f"no such category: {payload['category_id']}")

        _reregister(register_function, gone)

        result = reproject_listings()  # must not raise

        live_listing.refresh_from_db()
        assert "labels" not in _badge(live_listing)  # left exactly as found
        assert result["examined"] == 1
        assert result["changed"] == 0
        assert result["skipped"] == 1
        assert result["skipped_by_reason"]["category_unresolved"] == 1
        assert result["skipped_ids"]["category_unresolved"] == [live_listing.pk]

    def test_one_gone_category_does_not_abort_the_rest_of_the_run(
        self, live_listing, draft_listing, user, stub_categories
    ):
        """The healthy row after the broken one is still repaired."""
        from stapel_core.comm import register_function

        broken = _as_pre_070(live_listing)
        Listing.all_objects.filter(pk=broken.pk).update(category_id="999")

        healthy = Listing.objects.create(
            owner=user,
            category_id="7",
            title_draft="Second car",
            description_draft="Another well kept car in great condition.",
            images_draft=["product/def456"],
            features_draft=_draft(),
        )
        publish_service.publish_listing(healthy)
        healthy.apply_moderation("approved")
        _as_pre_070(healthy)
        cache.clear()

        def sometimes(payload):
            if str(payload["category_id"]) == "999":
                raise LookupError("no such category")
            return {
                "category_id": payload["category_id"],
                "revision": 1,
                "features": stub_categories,
            }

        _reregister(register_function, sometimes)

        result = reproject_listings()

        healthy.refresh_from_db()
        assert _badge(healthy)["labels"] == ["cond.new"]
        assert result["examined"] == 2
        assert result["changed"] == 1
        assert result["skipped"] == 1

    def test_one_invalid_field_no_longer_costs_the_listing_its_repair(
        self, live_listing, stub_categories
    ):
        """The measured failure: 12 listings stuck on a stale shape.

        Before 0.13.0 this asserted the opposite — one field out of bounds and
        the whole listing was skipped, so ``condition`` kept printing its
        storage slug because ``mileage`` had drifted. The two fields have
        nothing to do with each other, and that is the point.
        """
        _as_pre_070(live_listing)
        # The category tightened its bounds under a listing that predates the
        # change: the stored mileage of 42000 no longer validates.
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()  # the schema cache holds a pickled snapshot, not the list

        result = reproject_listings()

        live_listing.refresh_from_db()
        # The healthy field WAS repaired: _as_pre_070 stripped `labels`, and
        # the badge carries them again.
        assert _badge(live_listing)["labels"], "condition kept its stale shape"
        assert result["changed"] == 1
        assert result["skipped_by_reason"]["draft_invalid"] == 0
        # And the broken one was reported, by slug, with the reason.
        assert result["invalid_field_count"] == 1
        assert set(result["invalid_fields"][live_listing.pk]) == {"mileage"}
        assert result["repaired_with_invalid_fields"] == 1

    def test_an_unrepairable_field_keeps_its_stored_value_it_is_not_dropped(
        self, live_listing, stub_categories
    ):
        """Dropping it would turn a stale value into a missing one."""
        before = {
            dao["slug"]: dao
            for dao in Listing.objects.get(pk=live_listing.pk).features
        }
        _as_pre_070(live_listing)
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()

        reproject_listings()

        live_listing.refresh_from_db()
        after = {dao["slug"]: dao for dao in live_listing.features}
        assert "mileage" in after, "the field the pass could not fix was deleted"
        assert after["mileage"]["value"] == before["mileage"]["value"]

    def test_the_other_three_projections_agree_with_the_merged_list(
        self, live_listing, stub_categories
    ):
        """title/badges/search are derived from the MERGED list, not the fresh half."""
        _as_pre_070(live_listing)
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()

        reproject_listings()

        live_listing.refresh_from_db()
        slugs = {dao["slug"] for dao in live_listing.features}
        # mileage is the title field in the fixture; it survived, so the title
        # projection must still carry it.
        assert "mileage" in slugs
        assert {dao["slug"] for dao in live_listing.features_title} <= slugs
        assert set(live_listing.features_search) <= slugs

    def test_a_rerun_after_a_partial_repair_changes_nothing(
        self, live_listing, stub_categories
    ):
        """Idempotence survives the repair path."""
        _as_pre_070(live_listing)
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()

        reproject_listings()
        second = reproject_listings()

        assert second["changed"] == 0
        # Still reported on every run — the field is still broken.
        assert second["invalid_field_count"] == 1

    def test_a_draft_that_is_not_an_object_is_still_a_whole_listing_skip(
        self, live_listing
    ):
        """No field can be judged, so there is nothing to repair per field."""
        Listing.all_objects.filter(pk=live_listing.pk).update(
            features_draft=["not", "an", "object"]
        )

        result = reproject_listings()

        assert result["skipped_by_reason"]["draft_invalid"] == 1
        assert result["changed"] == 0

    def test_projections_without_a_draft_are_left_alone(self, live_listing):
        """Projecting an empty draft would ERASE the listing's attributes."""
        legacy = _as_pre_070(live_listing)
        Listing.all_objects.filter(pk=legacy.pk).update(features_draft={})

        result = reproject_listings()

        legacy.refresh_from_db()
        assert legacy.features  # still there
        assert result["skipped_by_reason"]["no_draft"] == 1
        assert result["changed"] == 0

    def test_soft_deleted_listings_are_outside_the_population(self, live_listing):
        _as_pre_070(live_listing)
        live_listing.delete()

        result = reproject_listings()

        assert result["examined"] == 0


class TestCategoryFilter:
    @pytest.fixture
    def two_categories(self, live_listing, user, stub_categories):
        second = Listing.objects.create(
            owner=user,
            category_id="8",
            title_draft="Second car",
            description_draft="Another well kept car in great condition.",
            images_draft=["product/def456"],
            features_draft=_draft(),
        )
        publish_service.publish_listing(second)
        second.apply_moderation("approved")
        return _as_pre_070(live_listing), _as_pre_070(second)

    def test_category_filter_bounds_the_population(self, two_categories):
        in_seven, in_eight = two_categories

        result = reproject_listings(category_ids=["8"])

        in_seven.refresh_from_db()
        in_eight.refresh_from_db()
        assert "labels" not in _badge(in_seven)
        assert _badge(in_eight)["labels"] == ["cond.new"]
        assert result["examined"] == 1
        assert result["changed"] == 1

    def test_no_filter_takes_both(self, two_categories):
        result = reproject_listings()
        assert result["examined"] == 2
        assert result["changed"] == 2


class TestCommand:
    def test_command_reports_numbers_not_the_word_done(self, live_listing, capsys):
        _as_pre_070(live_listing)

        call_command("listings_reproject_features")

        out = capsys.readouterr().out
        assert "1 examined" in out
        assert "re-projected 1" in out
        assert "0 already current" in out
        assert "0 skipped" in out
        assert "listing.updated: 1" in out

    def test_command_dry_run_writes_nothing(self, live_listing, capsys):
        legacy = _as_pre_070(live_listing)

        call_command("listings_reproject_features", "--dry-run")

        legacy.refresh_from_db()
        assert "labels" not in _badge(legacy)
        assert "DRY RUN" in capsys.readouterr().out

    def test_command_category_flag_repeats_and_splits_on_commas(
        self, live_listing, capsys
    ):
        _as_pre_070(live_listing)

        call_command(
            "listings_reproject_features", "--category", "7,8", "--category", "9"
        )

        out = capsys.readouterr().out
        assert "categories 7, 8, 9" in out
        assert "1 examined" in out

    def test_command_names_the_skipped_ids(self, live_listing, capsys):
        _as_pre_070(live_listing)
        Listing.all_objects.filter(pk=live_listing.pk).update(features_draft={})

        call_command("listings_reproject_features")

        out = capsys.readouterr().out
        assert "skipped 1 as no_draft" in out
        assert str(live_listing.pk) in out

    def test_command_says_so_when_the_scope_is_empty(self, db, capsys):
        call_command("listings_reproject_features", "--category", "404")

        assert "nothing to re-project" in capsys.readouterr().out

    def test_command_names_every_field_it_could_not_re_derive(
        self, live_listing, stub_categories, capsys
    ):
        """A repair that hides what it worked around is how a catalogue rots."""
        _as_pre_070(live_listing)
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()

        call_command("listings_reproject_features")

        out = capsys.readouterr().out
        assert "could not be re-derived" in out
        assert "KEPT THEIR STORED VALUES" in out
        assert f"listing {live_listing.pk} [mileage]" in out

    def test_command_exits_zero_when_it_repaired_something(
        self, live_listing, stub_categories, capsys
    ):
        """An invalid field that was worked around is a report, not a failure."""
        _as_pre_070(live_listing)
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()

        call_command("listings_reproject_features")  # must not raise

    def test_command_exits_non_zero_when_it_repaired_nothing(
        self, live_listing, stub_categories, capsys
    ):
        from django.core.management.base import CommandError

        from stapel_core.comm import register_function

        _as_pre_070(live_listing)
        # The category is gone entirely: nothing on this listing is repairable.
        Listing.all_objects.filter(pk=live_listing.pk).update(category_id="999")

        def gone(payload):
            raise LookupError(f"no such category: {payload['category_id']}")

        _reregister(register_function, gone)

        with pytest.raises(CommandError, match="repaired nothing"):
            call_command("listings_reproject_features")

    def test_a_row_with_no_draft_is_not_a_repair_failure(
        self, live_listing, capsys
    ):
        """`no_draft` is a row this pass does not apply to, not damage."""
        legacy = _as_pre_070(live_listing)
        Listing.all_objects.filter(pk=legacy.pk).update(features_draft={})

        call_command("listings_reproject_features")  # must not raise

    def test_strict_turns_a_worked_around_field_into_a_failure(
        self, live_listing, stub_categories, capsys
    ):
        from django.core.management.base import CommandError

        _as_pre_070(live_listing)
        for feature in stub_categories:
            if feature["slug"] == "mileage":
                feature["config"] = {"type": "int", "min": 0, "max": 10}
        cache.clear()

        with pytest.raises(CommandError, match="--strict"):
            call_command("listings_reproject_features", "--strict")

    def test_command_refuses_a_batch_size_below_one(self):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="batch-size"):
            call_command("listings_reproject_features", "--batch-size", "0")

    def test_command_batch_size_flag_is_accepted(self, live_listing):
        _as_pre_070(live_listing)

        call_command("listings_reproject_features", "--batch-size", "1")

        live_listing.refresh_from_db()
        assert _badge(live_listing)["labels"] == ["cond.used"]
