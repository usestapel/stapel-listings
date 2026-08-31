"""Coordinate precision at the API boundary.

The night run's blocker: Photon answers ``55.7505412`` — seven decimal places —
and every attempt to save a draft carrying a geocoded address came back 400
``Ensure that there are no more than 6 decimal places``. No address from the
geocoder could be published at all, and the browser's own fix is worse: a live
GPS reading carries fourteen places.

Eleven centimetres is not a client error, so the boundary rounds instead of
refusing. What still has to hold: the bounds are real, the rounding is the
column's, and money is untouched by any of it.
"""
import pytest

from stapel_listings.models import Listing

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


def test_create_accepts_a_geocoder_s_seven_places(auth_client):
    """The verdict's repro, line for line."""
    resp = auth_client.post(
        "/listings/listings/",
        {"category_id": "7", "lat_draft": "55.7505412", "lon_draft": "37.6174782"},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    listing = Listing.objects.get(pk=resp.data["id"])
    assert str(listing.lat_draft) == "55.750541"
    assert str(listing.lon_draft) == "37.617478"


def test_create_still_accepts_six_places(auth_client):
    resp = auth_client.post(
        "/listings/listings/",
        {"category_id": "7", "lat_draft": "55.750541", "lon_draft": "37.617478"},
        format="json",
    )
    assert resp.status_code == 201, resp.content


def test_save_draft_accepts_a_browser_fix(auth_client, user, stub_categories):
    """What a real phone sends: the full float, straight out of the GPS."""
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {
            "lat_draft": "55.75396200000001",
            "lon_draft": "37.620393999999995",
        },
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert str(listing.lat_draft) == "55.753962"
    assert str(listing.lon_draft) == "37.620394"


def test_rounds_to_the_nearest_column_value(auth_client, user, stub_categories):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"lat_draft": "55.7505419", "lon_draft": "-37.6174785"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert str(listing.lat_draft) == "55.750542"
    assert str(listing.lon_draft) == "-37.617478"


def test_a_coordinate_out_of_range_is_still_refused(auth_client, user, stub_categories):
    """Rounding is not permissiveness: 1000 degrees is a wrong answer."""
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"lat_draft": "1000.1234567"},
        format="json",
    )
    assert resp.status_code == 400, resp.content


def test_price_precision_is_still_a_client_error(auth_client, user, stub_categories):
    """Money is not a coordinate: dropping a digit there changes the amount."""
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"price_draft": "200.00123"},
        format="json",
    )
    assert resp.status_code == 400, resp.content


def test_a_geocoded_draft_still_gets_its_geohash(auth_client, user, stub_categories, stub_geo):
    """The stamp downstream of the pair, on the rounded value."""
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"lat_draft": "55.7505412", "lon_draft": "37.6174782"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.geohash_draft
