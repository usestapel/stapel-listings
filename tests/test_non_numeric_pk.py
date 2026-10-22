"""A non-numeric pk is a 404, not a 500, on every listing route.

The router's default lookup admitted "abc"; the custom actions passed it to
``Listing.objects.get(pk=...)``, which raises ValueError, and only DRF's
``retrieve`` turned that into a 404.
"""
import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.mark.parametrize("method,path", [
    ("post", "/listings/listings/abc/archive/"),
    ("post", "/listings/listings/undefined/publish/"),
    ("get", "/listings/listings/null/validate-draft/"),
    ("post", "/listings/listings/null/favorite/"),
    ("delete", "/listings/listings/abc/favorite/"),
    ("patch", "/listings/listings/undefined/"),
    ("get", "/listings/listings/abc/"),
])
def test_non_numeric_pk_is_not_found(owner_client, method, path):
    resp = getattr(owner_client, method)(path, {}, format="json")
    assert resp.status_code == 404


def test_numeric_pk_still_routes(owner_client, draft_listing):
    resp = owner_client.get(f"/listings/listings/{draft_listing.pk}/validate-draft/")
    assert resp.status_code == 200
