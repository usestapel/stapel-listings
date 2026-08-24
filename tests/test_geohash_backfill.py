"""``listings_backfill_geohash`` — the one-time pass over listings written
before ``Listing.save()`` called ``geo.geohash_encode`` (see
``services/geohash_backfill.py`` and ``tests/test_geohash_stamp.py`` for the
save()-time fix these rows predate).
"""
import hashlib
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from stapel_core.comm import register_function
from stapel_core.comm.registry import function_registry
from stapel_listings.models import Listing
from stapel_listings.services.geohash_backfill import backfill_geohashes

pytestmark = pytest.mark.django_db


def _expected(lat, lon, precision=8):
    key = f"{float(lat):.6f},{float(lon):.6f}".encode()
    return hashlib.sha1(key).hexdigest()[:precision]


@pytest.fixture
def register_geo():
    """Same deterministic stub as conftest's ``stub_geo``, registered here
    directly (not as a listing-creation-time fixture) so a test controls
    exactly when the provider becomes available relative to inserting the
    "legacy" rows."""

    def provider(payload):
        precision = payload.get("precision") or 8
        key = f"{payload['lat']:.6f},{payload['lon']:.6f}".encode()
        return {"geohash": hashlib.sha1(key).hexdigest()[:precision]}

    register_function("geo.geohash_encode", provider)
    yield
    function_registry._providers.pop("geo.geohash_encode", None)
    function_registry._schemas.pop("geo.geohash_encode", None)


def _legacy_listing(user, **fields) -> Listing:
    """A row shaped like pre-fix data: coordinates present, geohash empty,
    inserted via ``bulk_create`` so ``Listing.save()``'s stamping (this
    release's fix) never runs — exactly what a listing written before this
    release looks like today."""
    defaults = {"owner": user, "category_id": "7"}
    defaults.update(fields)
    listing = Listing(**defaults)
    Listing.objects.bulk_create([listing])
    return Listing.all_objects.get(pk=listing.pk)


class TestBackfillService:
    def test_stamps_published_and_draft_populations(self, user, register_geo):
        published = _legacy_listing(
            user, lat=Decimal("52.520008"), lon=Decimal("13.404954")
        )
        draft = _legacy_listing(
            user, lat_draft=Decimal("40.7128"), lon_draft=Decimal("-74.0060")
        )
        no_coords = _legacy_listing(user)

        result = backfill_geohashes()

        published.refresh_from_db()
        draft.refresh_from_db()
        no_coords.refresh_from_db()

        assert published.geohash == _expected("52.520008", "13.404954")
        assert draft.geohash_draft == _expected("40.7128", "-74.0060")
        assert no_coords.geohash == "" and no_coords.geohash_draft == ""

        assert result["published"] == {"candidates": 1, "stamped": 1, "unresolved": 0}
        assert result["draft"] == {"candidates": 1, "stamped": 1, "unresolved": 0}

    def test_idempotent_second_run_touches_nothing(self, user, register_geo):
        _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("2.0"))

        first = backfill_geohashes()
        second = backfill_geohashes()

        assert first["published"]["stamped"] == 1
        assert second["published"]["candidates"] == 0
        assert second["published"]["stamped"] == 0

    def test_dry_run_reports_without_writing(self, user, register_geo):
        listing = _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("2.0"))

        result = backfill_geohashes(dry_run=True)
        listing.refresh_from_db()

        assert result["published"] == {"candidates": 1, "stamped": 0, "unresolved": 0}
        assert listing.geohash == ""

    def test_geo_unanswered_is_unresolved_not_a_crash(self, user):
        # No register_geo fixture: no provider registered at all, the same
        # shape as a deployment without stapel-geo.
        listing = _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("2.0"))

        result = backfill_geohashes()  # must not raise

        listing.refresh_from_db()
        assert listing.geohash == ""
        assert result["published"] == {"candidates": 1, "stamped": 0, "unresolved": 1}

    def test_includes_soft_deleted_listings(self, user, register_geo):
        listing = _legacy_listing(
            user, lat=Decimal("1.0"), lon=Decimal("2.0"), deleted_at=timezone.now()
        )
        assert Listing.objects.filter(pk=listing.pk).count() == 0  # hidden by default manager

        result = backfill_geohashes()
        listing.refresh_from_db()

        assert listing.geohash == _expected("1.0", "2.0")
        assert result["published"]["stamped"] == 1

    def test_limit_bounds_each_population_independently(self, user, register_geo):
        _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("1.0"))
        _legacy_listing(user, lat=Decimal("2.0"), lon=Decimal("2.0"))
        _legacy_listing(user, lat_draft=Decimal("3.0"), lon_draft=Decimal("3.0"))

        result = backfill_geohashes(limit=1)

        assert result["published"]["candidates"] == 1
        assert result["published"]["stamped"] == 1
        assert result["draft"]["candidates"] == 1
        assert result["draft"]["stamped"] == 1


class TestBackfillCommand:
    def test_command_runs_and_reports(self, user, register_geo, capsys):
        _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("1.0"))

        call_command("listings_backfill_geohash")

        out = capsys.readouterr().out
        assert "[published]:" in out
        assert "stamped 1 geohash" in out

    def test_command_dry_run_flag(self, user, register_geo):
        listing = _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("1.0"))

        call_command("listings_backfill_geohash", "--dry-run")

        listing.refresh_from_db()
        assert listing.geohash == ""

    def test_command_warns_when_everything_unresolved(self, user, capsys):
        _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("1.0"))

        call_command("listings_backfill_geohash")

        out = capsys.readouterr().out
        assert "unresolved" in out

    def test_command_batch_size_and_limit_flags_accepted(self, user, register_geo):
        _legacy_listing(user, lat=Decimal("1.0"), lon=Decimal("1.0"))
        _legacy_listing(user, lat=Decimal("2.0"), lon=Decimal("2.0"))

        call_command("listings_backfill_geohash", "--batch-size", "1", "--limit", "1")

        assert Listing.all_objects.exclude(geohash="").count() == 1
