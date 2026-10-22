"""``GET /listings/?category_id=`` narrows the public board to categories.

A category page needs the listings of its category and its subtree; the
board answered every published listing whatever the page was, so a
storefront without the search module had no category grid (Tier-0 e2e D5).
"""
import pytest

from stapel_listings.models import Listing, ListingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def board(user):
    rows = {}
    for category, title in (("7", "Camry"), ("8", "Phone"), ("9", "Sofa")):
        rows[category] = Listing.objects.create(
            owner=user, category_id=category, title=title,
            status=ListingStatus.PUBLISHED,
        )
    Listing.objects.create(owner=user, category_id="7", title_draft="Draft")
    return rows


def _titles(resp):
    assert resp.status_code == 200, resp.content
    return sorted(row["title"] for row in resp.data["items"])


def test_one_category(api_client, board):
    assert _titles(api_client.get("/listings/listings/?category_id=7")) == ["Camry"]


def test_a_subtree_comma_separated_or_repeated(api_client, board):
    assert _titles(api_client.get("/listings/listings/?category_id=7,8")) == ["Camry", "Phone"]
    assert _titles(
        api_client.get("/listings/listings/?category_id=7&category_id=9")
    ) == ["Camry", "Sofa"]


def test_absent_means_every_category(api_client, board):
    assert _titles(api_client.get("/listings/listings/")) == ["Camry", "Phone", "Sofa"]
