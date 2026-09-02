"""``listings_backfill_cdn_refs`` — the one-time pass claiming CDN media for
listings written before ``Listing.save()`` announced claims (0.14.0, see
``tests/test_cdn_ref_sync.py`` for the save()-time sync these rows predate).

Every event carries ``old_hashes=[]``: the backfill only ever ADDS claims
(``apply_ref_sync``'s ``to_remove = old - new`` is empty by construction), so
a rerun — or a run over rows the save-path already claimed — is a no-op on
the CDN side, never a release.
"""
import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_listings.models import Listing
from stapel_listings.services.cdn_refs_backfill import backfill_cdn_refs

pytestmark = pytest.mark.django_db


def _legacy_listing(user, **fields) -> Listing:
    """A row shaped like pre-0.14.0 data: image refs present, nothing ever
    claimed — inserted via ``bulk_create`` so ``Listing.save()``'s ref sync
    (this release's 0.14.0 fix) never runs."""
    defaults = {"owner": user, "category_id": "7"}
    defaults.update(fields)
    listing = Listing(**defaults)
    Listing.objects.bulk_create([listing])
    return Listing.all_objects.get(pk=listing.pk)


@pytest.fixture
def capture_sync(monkeypatch):
    import stapel_core.django.cdn.ref_sync as ref_sync

    calls = []

    def fake_sync(service, entity_type, entity_id, old_refs, new_refs):
        calls.append(
            (service, entity_type, entity_id, list(old_refs), set(new_refs))
        )
        return ref_sync.RefSyncResult(ok=True)

    monkeypatch.setattr(ref_sync, "sync_cdn_refs", fake_sync)
    return calls


class TestBackfillService:
    def test_claims_union_of_published_and_draft(self, user, capture_sync):
        listing = _legacy_listing(
            user,
            images=["product/pub1"],
            images_draft=["product/dr1", "product/pub1"],
        )
        result = backfill_cdn_refs()
        assert capture_sync == [
            ("listings", "listing", listing.pk, [], {"product/pub1", "product/dr1"})
        ]
        assert result == {"candidates": 1, "published": 1, "failed": 0}

    def test_skips_rows_without_images(self, user, capture_sync):
        _legacy_listing(user)
        result = backfill_cdn_refs()
        assert capture_sync == []
        assert result == {"candidates": 0, "published": 0, "failed": 0}

    def test_skips_soft_deleted_rows(self, user, capture_sync):
        _legacy_listing(
            user, images_draft=["product/gone"], deleted_at=timezone.now()
        )
        result = backfill_cdn_refs()
        assert capture_sync == []
        assert result == {"candidates": 0, "published": 0, "failed": 0}

    def test_dry_run_publishes_nothing(self, user, capture_sync):
        _legacy_listing(user, images_draft=["product/a1"])
        result = backfill_cdn_refs(dry_run=True)
        assert capture_sync == []
        assert result == {"candidates": 1, "published": 0, "failed": 0}

    def test_limit_bounds_the_pass(self, user, capture_sync):
        _legacy_listing(user, images_draft=["product/a1"])
        _legacy_listing(user, images_draft=["product/b2"])
        result = backfill_cdn_refs(limit=1)
        assert len(capture_sync) == 1
        assert result == {"candidates": 1, "published": 1, "failed": 0}

    def test_bus_failure_counted_not_raised(self, user, monkeypatch):
        """The real helper degrades a dead bus to ok=False; the backfill
        counts it as failed and keeps going instead of raising."""
        import stapel_core.bus as bus

        def boom(topic, event):
            raise RuntimeError("bus down")

        monkeypatch.setattr(bus, "publish", boom)
        _legacy_listing(user, images_draft=["product/a1"])
        _legacy_listing(user, images_draft=["product/b2"])
        result = backfill_cdn_refs()
        assert result == {"candidates": 2, "published": 0, "failed": 2}


class TestBackfillCommand:
    def test_command_reports_counts(self, user, capture_sync, capsys):
        _legacy_listing(user, images_draft=["product/a1"])
        call_command("listings_backfill_cdn_refs")
        out = capsys.readouterr().out
        assert "1 candidate(s)" in out
        assert "claimed 1" in out
        assert len(capture_sync) == 1

    def test_command_dry_run(self, user, capture_sync, capsys):
        _legacy_listing(user, images_draft=["product/a1"])
        call_command("listings_backfill_cdn_refs", "--dry-run")
        out = capsys.readouterr().out
        assert "would claim" in out
        assert capture_sync == []
