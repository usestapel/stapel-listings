"""Favorites as first-class engagement (replacing the stats read-caches)."""
import pytest

from stapel_listings.models import Favorite, Listing

pytestmark = pytest.mark.django_db


def test_favorite_is_unique_per_user_listing(user):
    listing = Listing.objects.create(owner=user, category_id="7")
    Favorite.objects.create(user=user, listing=listing)
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        Favorite.objects.create(user=user, listing=listing)


def test_with_favorited_annotation(user, other_user):
    listing = Listing.objects.create(owner=other_user, category_id="7")
    Favorite.objects.create(user=user, listing=listing)

    annotated = Listing.objects.with_favorited(user).get(pk=listing.pk)
    assert annotated.is_favorited is True

    annotated_other = Listing.objects.with_favorited(other_user).get(pk=listing.pk)
    assert annotated_other.is_favorited is False


def test_with_favorited_anonymous_is_null(user):
    listing = Listing.objects.create(owner=user, category_id="7")

    class Anon:
        is_authenticated = False

    annotated = Listing.objects.with_favorited(Anon()).get(pk=listing.pk)
    assert annotated.is_favorited is None
