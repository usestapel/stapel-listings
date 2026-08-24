"""Server-side ``geohash_draft`` stamping (Listing.save() ->
compute_geohash_draft() -> the geo.geohash_encode comm Function).

Before this fix stapel-listings never called geo.geohash_encode at all — see
stapel-geo's MODULE.md, which documented listings as the consumer of exactly
this Function — so every listing carried an empty geohash regardless of
lat/lon. These tests cover: stamp on create, stamp on a coordinate update, no
stamp/no crash when geo is unanswered (the default here — stapel_geo is not
in INSTALLED_APPS for this test suite), and that a client can no longer set
geohash_draft directly.
"""
from decimal import Decimal

import pytest

from stapel_listings.models import Listing
from stapel_listings.serializers import ListingDraftSerializer

pytestmark = pytest.mark.django_db


def _geo_answer(lat, lon, precision=8):
    """The same deterministic stub geohash the ``stub_geo`` fixture's
    provider returns, computed independently so a test can assert an exact
    value without importing the fixture's closure."""
    import hashlib

    key = f"{Decimal(lat):.6f},{Decimal(lon):.6f}".encode()
    return hashlib.sha1(key).hexdigest()[:precision]


class TestStampOnCreate:
    def test_create_with_coordinates_stamps_geohash(self, user, stub_categories, stub_geo):
        listing = Listing.objects.create(
            owner=user,
            category_id="7",
            lat_draft=Decimal("52.520008"),
            lon_draft=Decimal("13.404954"),
        )
        assert listing.geohash_draft == _geo_answer("52.520008", "13.404954")
        assert listing.geohash_draft != ""

    def test_create_without_coordinates_stays_blank(self, user, stub_categories, stub_geo):
        listing = Listing.objects.create(owner=user, category_id="7")
        assert listing.geohash_draft == ""

    def test_create_with_only_one_coordinate_stays_blank(self, user, stub_categories, stub_geo):
        listing = Listing.objects.create(
            owner=user, category_id="7", lat_draft=Decimal("52.5"),
        )
        assert listing.geohash_draft == ""


class TestStampOnUpdate:
    def test_setting_coordinates_on_update_stamps_geohash(self, draft_listing, stub_geo):
        assert draft_listing.geohash_draft == ""
        draft_listing.lat_draft = Decimal("40.7128")
        draft_listing.lon_draft = Decimal("-74.0060")
        draft_listing.save()
        assert draft_listing.geohash_draft == _geo_answer("40.7128", "-74.0060")

    def test_changing_coordinates_recomputes_geohash(self, draft_listing, stub_geo):
        draft_listing.lat_draft = Decimal("40.7128")
        draft_listing.lon_draft = Decimal("-74.0060")
        draft_listing.save()
        first = draft_listing.geohash_draft

        draft_listing.lat_draft = Decimal("51.5074")
        draft_listing.lon_draft = Decimal("-0.1278")
        draft_listing.save()

        assert draft_listing.geohash_draft != first
        assert draft_listing.geohash_draft == _geo_answer("51.5074", "-0.1278")

    def test_unrelated_field_update_does_not_touch_geohash(self, draft_listing, stub_geo):
        draft_listing.lat_draft = Decimal("40.7128")
        draft_listing.lon_draft = Decimal("-74.0060")
        draft_listing.save()
        stamped = draft_listing.geohash_draft

        draft_listing.title_draft = "Updated title"
        draft_listing.save(update_fields=["title_draft"])
        draft_listing.refresh_from_db()

        assert draft_listing.geohash_draft == stamped

    def test_partial_save_touching_only_lon_still_recomputes(self, draft_listing, stub_geo):
        draft_listing.lat_draft = Decimal("10.0")
        draft_listing.lon_draft = Decimal("20.0")
        draft_listing.save()

        draft_listing.lon_draft = Decimal("21.0")
        draft_listing.save(update_fields=["lon_draft"])
        draft_listing.refresh_from_db()

        assert draft_listing.geohash_draft == _geo_answer("10.0", "21.0")


class TestGeoUnanswered:
    """No ``stub_geo`` fixture in this class: geo.geohash_encode has no
    provider at all, exactly like a deployment without stapel-geo."""

    def test_create_with_coordinates_and_no_geo_provider_does_not_crash(
        self, user, stub_categories,
    ):
        listing = Listing.objects.create(
            owner=user,
            category_id="7",
            lat_draft=Decimal("52.520008"),
            lon_draft=Decimal("13.404954"),
        )
        assert listing.geohash_draft == ""

    def test_update_with_coordinates_and_no_geo_provider_does_not_crash(
        self, draft_listing,
    ):
        draft_listing.lat_draft = Decimal("40.7128")
        draft_listing.lon_draft = Decimal("-74.0060")
        draft_listing.save()  # must not raise
        assert draft_listing.geohash_draft == ""

    def test_compute_geohash_draft_direct_call_is_graceful(self, draft_listing):
        draft_listing.lat_draft = Decimal("1.0")
        draft_listing.lon_draft = Decimal("2.0")
        assert draft_listing.compute_geohash_draft() == ""

    def test_stale_geohash_is_cleared_when_geo_becomes_unreachable(
        self, draft_listing, stub_geo,
    ):
        """A previously-stamped geohash must not survive a coordinate change
        the geo service can no longer confirm — an unknown geohash is safer
        than one describing the wrong coordinates (compute_geohash_draft's
        own "unknown beats wrong" stance, same as compute_price_base)."""
        draft_listing.lat_draft = Decimal("1.0")
        draft_listing.lon_draft = Decimal("2.0")
        draft_listing.save()
        assert draft_listing.geohash_draft != ""

        from stapel_core.comm.registry import function_registry

        function_registry._providers.pop("geo.geohash_encode", None)
        function_registry._schemas.pop("geo.geohash_encode", None)

        draft_listing.lat_draft = Decimal("3.0")
        draft_listing.save()
        assert draft_listing.geohash_draft == ""


class TestServerComputedNotClientWritable:
    def test_client_supplied_geohash_draft_is_ignored(self, draft_listing, stub_geo):
        serializer = ListingDraftSerializer(
            draft_listing,
            data={
                "lat_draft": "48.8566",
                "lon_draft": "2.3522",
                "geohash_draft": "bogus-client-value",
            },
            partial=True,
        )
        assert serializer.is_valid(), serializer.errors
        serializer.save()
        draft_listing.refresh_from_db()

        assert draft_listing.geohash_draft != "bogus-client-value"
        assert draft_listing.geohash_draft == _geo_answer("48.8566", "2.3522")
