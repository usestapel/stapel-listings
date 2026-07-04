"""API smoke tests for the ListingViewSet."""
import pytest

from stapel_listings.models import Favorite, Listing, ListingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


def test_create_draft(auth_client, user):
    resp = auth_client.post("/listings/listings/", {"category_id": "7"}, format="json")
    assert resp.status_code == 201, resp.content
    listing = Listing.objects.get(pk=resp.data["id"])
    assert listing.owner_id == user.id
    assert listing.status == ListingStatus.DRAFT


def test_save_draft_rejects_negative_price(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"price_draft": "-5"},
        format="json",
    )
    assert resp.status_code == 400


def test_save_draft_persists(auth_client, user, stub_categories):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"title_draft": "Nice bike", "price_draft": "200.00"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.title_draft == "Nice bike"


def test_publish_flow(auth_client, draft_listing):
    resp = auth_client.post(f"/listings/listings/{draft_listing.pk}/publish/")
    assert resp.status_code == 200, resp.content
    assert resp.data["published"] is True
    assert resp.data["status"] == ListingStatus.PENDING


def test_publish_invalid_returns_validation(auth_client, user, stub_categories):
    listing = Listing.objects.create(
        owner=user, category_id="7", description_draft="ok enough",
        images_draft=["product/x"], features_draft={},  # missing mandatory mileage
    )
    resp = auth_client.post(f"/listings/listings/{listing.pk}/publish/")
    assert resp.status_code == 400
    assert resp.data["valid"] is False


def test_cannot_save_others_draft(auth_client, other_user):
    listing = Listing.objects.create(owner=other_user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/", {"title_draft": "x"}, format="json"
    )
    assert resp.status_code == 403


def test_favorite_and_unfavorite(auth_client, user, other_user):
    listing = Listing.objects.create(owner=other_user, category_id="7")
    resp = auth_client.post(f"/listings/listings/{listing.pk}/favorite/")
    assert resp.status_code == 200
    assert Favorite.objects.filter(user=user, listing=listing).exists()

    resp = auth_client.post(f"/listings/listings/{listing.pk}/unfavorite/")
    assert resp.status_code == 200
    assert not Favorite.objects.filter(user=user, listing=listing).exists()


def test_status_endpoint_public(api_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = api_client.get(f"/listings/listings/{listing.pk}/status/")
    assert resp.status_code == 200
    assert resp.data["status"] == ListingStatus.DRAFT
    assert resp.data["owner_id"] == str(user.id)


def test_my_counters(auth_client, user):
    Listing.objects.create(owner=user, category_id="7", status=ListingStatus.DRAFT)
    Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PUBLISHED)
    resp = auth_client.get("/listings/listings/my/counters/")
    assert resp.status_code == 200
    assert resp.data["drafts"] == 1
    assert resp.data["active"] == 1


def test_destroy_active_conflicts(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    resp = auth_client.delete(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 409
