"""GDPR: provider export/delete and the user.deleted consumer."""
import pytest

from stapel_listings.gdpr import ListingsGDPRProvider
from stapel_listings.models import Favorite, Listing, ListingStatus

pytestmark = pytest.mark.django_db


def test_provider_registered():
    from stapel_core.gdpr import gdpr_registry

    assert "listings" in gdpr_registry.sections


def test_export_returns_listings_and_favorites(user, other_user):
    listing = Listing.objects.create(owner=user, category_id="7", title="Car")
    other = Listing.objects.create(owner=other_user, category_id="7")
    Favorite.objects.create(user=user, listing=other)

    data = ListingsGDPRProvider().export(user.id)
    assert len(data["listings"]) == 1
    assert data["listings"][0]["title"] == "Car"
    assert data["favorites"] == [other.pk]
    assert listing.pk == data["listings"][0]["id"]


def test_delete_erases_owned_listings_and_favorites(user, other_user):
    mine = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    others = Listing.objects.create(owner=other_user, category_id="7")
    Favorite.objects.create(user=user, listing=others)
    Favorite.objects.create(user=other_user, listing=mine)

    ListingsGDPRProvider().delete(user.id)

    assert Listing.all_objects.filter(owner=user).count() == 0
    assert Favorite.objects.filter(user=user).count() == 0
    # A favorite of my (now deleted) listing is gone too.
    assert Favorite.objects.filter(listing=mine).count() == 0
    # Other user's listing survives.
    assert Listing.all_objects.filter(owner=other_user).count() == 1


def test_delete_emits_removed_for_indexed(user, capture_events):
    removed = capture_events("listing.removed")
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.transition_to(ListingStatus.PUBLISHED)
    ListingsGDPRProvider().delete(user.id)
    assert any(e.payload["reason"] == "user_deleted" for e in removed)


def test_user_deleted_consumer_erases(user):
    from stapel_core.comm import emit

    Listing.objects.create(owner=user, category_id="7")
    emit("user.deleted", {"user_id": str(user.id), "deleted_at": "2026-01-01T00:00:00Z", "trigger": "manual"})
    assert Listing.all_objects.filter(owner=user).count() == 0
