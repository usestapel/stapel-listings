"""API smoke tests for the ListingViewSet."""
import pytest
from django.utils import timezone

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


def test_republish_of_a_live_listing_answers_published(auth_client, draft_listing):
    """0.5: the endpoint reports the listing is still live under re-moderation."""
    from stapel_listings.models import ModerationStatus

    auth_client.post(f"/listings/listings/{draft_listing.pk}/publish/")
    draft_listing.refresh_from_db()
    draft_listing.apply_moderation("approved")

    draft_listing.title_draft = "Toyota Camry 2019"
    draft_listing.save()
    resp = auth_client.post(f"/listings/listings/{draft_listing.pk}/publish/")

    assert resp.status_code == 200, resp.content
    assert resp.data["status"] == ListingStatus.PUBLISHED
    draft_listing.refresh_from_db()
    assert draft_listing.moderation_status == ModerationStatus.PENDING


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
    listing = Listing.objects.create(
        owner=other_user, category_id="7", status=ListingStatus.PUBLISHED
    )
    resp = auth_client.post(f"/listings/listings/{listing.pk}/favorite/")
    assert resp.status_code == 200
    assert Favorite.objects.filter(user=user, listing=listing).exists()

    resp = auth_client.post(f"/listings/listings/{listing.pk}/unfavorite/")
    assert resp.status_code == 200
    assert not Favorite.objects.filter(user=user, listing=listing).exists()


def test_status_tells_a_stranger_only_whether_the_row_is_gone(api_client, user):
    """The capability is the feature; the disclosure was the defect.

    Listing ids are sequential, so returning `owner_id` and
    `moderation_status` to anyone was an enumeration oracle over every
    listing in the fleet — other people's drafts, rejected and deleted rows
    included. Found live on a stand by walking ids.

    A stranger still gets an answer, because a browser client needs to tell
    "this listing was removed" from "there was never a listing here", and a
    404 alone cannot. It just gets one boolean.
    """
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = api_client.get(f"/listings/listings/{listing.pk}/status/")
    assert resp.status_code == 200
    assert set(resp.json()) == {"is_deleted"}


def test_a_stranger_never_learns_the_owner_or_the_moderation_verdict(api_client, user):
    """Named separately from the shape test above: this is the fact that
    leaked, and it should fail loudly if either field is ever added back."""
    listing = Listing.objects.create(owner=user, category_id="7")
    body = api_client.get(f"/listings/listings/{listing.pk}/status/").json()
    assert "owner_id" not in body
    assert "moderation_status" not in body
    assert "status" not in body


def test_a_service_still_gets_the_full_status(api_client, user):
    """The `listings.status` function rides this endpoint over X-API-KEY."""
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = api_client.get(
        f"/listings/listings/{listing.pk}/status/",
        HTTP_X_API_KEY="test-service-key",
    )
    assert resp.status_code == 200
    assert resp.json()["owner_id"] == str(user.pk)
    assert "moderation_status" in resp.json()


def test_the_owner_still_gets_the_full_status(auth_client, user):
    """Their own moderation verdict is theirs to see."""
    listing = Listing.objects.create(owner=user, category_id="7")
    body = auth_client.get(f"/listings/listings/{listing.pk}/status/").json()
    assert body["owner_id"] == str(user.pk)


def test_another_signed_in_user_is_still_a_stranger(auth_client, other_user):
    """Being logged in is not being the owner — the oracle would otherwise
    just cost an attacker one free account."""
    listing = Listing.objects.create(owner=other_user, category_id="7")
    body = auth_client.get(f"/listings/listings/{listing.pk}/status/").json()
    assert set(body) == {"is_deleted"}


def test_a_soft_deleted_listing_still_answers(api_client, user):
    """The whole reason the probe reads all_objects."""
    listing = Listing.objects.create(owner=user, category_id="7")
    listing.deleted_at = timezone.now()
    listing.save(update_fields=["deleted_at"])
    resp = api_client.get(f"/listings/listings/{listing.pk}/status/")
    assert resp.status_code == 200
    assert resp.json()["is_deleted"] is True


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
