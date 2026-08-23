"""``GET my/listings/`` — the owner's own rows, in every status.

The route that closes the one gap `@stapel/listings-react` recorded rather
than papered over (`packages/listings-react/MODULE.md` §3 ask 1,
`src/model/mineSource.ts`): ``list`` answers ``published()`` and takes no
owner parameter, so before 0.7.0 a seller's own DRAFTS were unreachable by
any call this contract offered.

What is pinned here:

* every status the owner has is visible through this route — including the
  eight that are not indexed, and BLOCKED, the one ``my/counters`` counts in
  no tab at all;
* a stranger's listing is never in the answer, at any status;
* an anonymous caller is refused, not given an empty page;
* the ``?status=`` filter in both spellings, and its 400 for a value that is
  not a status;
* soft-deleted rows are gone from here as they are from everywhere else;
* the pagination envelope is the module's, the same one ``my/favorites``
  returns.
"""
import pytest

from stapel_listings.models import Listing, ListingStatus, ModerationStatus

pytestmark = pytest.mark.django_db

URL = "/listings/listings/my/listings/"

ALL_STATUSES = [s for s in ListingStatus.values]


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


def _ids(resp):
    return [row["id"] for row in resp.data["items"]]


# --- what the owner sees -------------------------------------------------


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_owner_sees_own_listing_in_every_status(auth_client, user, status):
    listing = Listing.objects.create(owner=user, category_id="7", status=status)
    resp = auth_client.get(URL)
    assert resp.status_code == 200, resp.content
    assert _ids(resp) == [listing.pk]
    assert resp.data["items"][0]["status"] == status


def test_all_nine_statuses_come_back_in_one_page(auth_client, user):
    made = {
        status: Listing.objects.create(owner=user, category_id="7", status=status)
        for status in ALL_STATUSES
    }
    resp = auth_client.get(URL)
    assert resp.status_code == 200
    assert set(_ids(resp)) == {row.pk for row in made.values()}
    assert resp.data["count"] == len(ALL_STATUSES)


def test_ordering_is_newest_first(auth_client, user):
    first = Listing.objects.create(owner=user, category_id="7")
    second = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.get(URL)
    assert _ids(resp) == [second.pk, first.pk]


# --- what the owner does NOT see ----------------------------------------


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_a_stranger_listing_is_never_in_the_answer(
    auth_client, other_user, status
):
    Listing.objects.create(owner=other_user, category_id="7", status=status)
    resp = auth_client.get(URL)
    assert resp.status_code == 200
    assert _ids(resp) == []


def test_published_listings_of_others_are_not_folded_in(auth_client, user, other_user):
    mine = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.DRAFT
    )
    Listing.objects.create(
        owner=other_user, category_id="7", status=ListingStatus.PUBLISHED
    )
    resp = auth_client.get(URL)
    assert _ids(resp) == [mine.pk]


def test_soft_deleted_is_excluded(auth_client, user):
    kept = Listing.objects.create(owner=user, category_id="7")
    gone = Listing.objects.create(owner=user, category_id="7")
    gone.delete()
    resp = auth_client.get(URL)
    assert _ids(resp) == [kept.pk]


def test_anonymous_is_refused_not_given_an_empty_page(api_client, user):
    Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PUBLISHED)
    resp = api_client.get(URL)
    # 403 under DRF's default authenticators, 401 wherever the host installs
    # one that sets WWW-Authenticate (the same pin test_authz.py carries).
    assert resp.status_code in (401, 403)


# --- the ?status= filter -------------------------------------------------


@pytest.fixture
def one_of_each(user):
    return {
        status: Listing.objects.create(owner=user, category_id="7", status=status)
        for status in ALL_STATUSES
    }


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_single_status_filter(auth_client, one_of_each, status):
    resp = auth_client.get(URL, {"status": status})
    assert resp.status_code == 200
    assert _ids(resp) == [one_of_each[status].pk]


def test_repeated_parameter_is_a_set(auth_client, one_of_each):
    resp = auth_client.get(URL + "?status=draft&status=rejected")
    assert resp.status_code == 200
    assert set(_ids(resp)) == {
        one_of_each[ListingStatus.DRAFT].pk,
        one_of_each[ListingStatus.REJECTED].pk,
    }


def test_comma_separated_is_the_same_set(auth_client, one_of_each):
    resp = auth_client.get(URL + "?status=draft,rejected")
    assert resp.status_code == 200
    assert set(_ids(resp)) == {
        one_of_each[ListingStatus.DRAFT].pk,
        one_of_each[ListingStatus.REJECTED].pk,
    }


def test_the_three_counter_tabs_add_up_to_the_counters(auth_client, one_of_each):
    """The tab groupings are the server's; rows and counts must agree."""
    counters = auth_client.get("/listings/listings/my/counters/").data
    tabs = {
        "active": "published,pending",
        "drafts": "draft,rejected",
        "archived": "archived,paused,expired,sold",
    }
    for tab, statuses in tabs.items():
        resp = auth_client.get(URL + f"?status={statuses}")
        assert resp.data["count"] == counters[tab], tab


def test_blocked_is_reachable_even_though_no_tab_counts_it(auth_client, one_of_each):
    resp = auth_client.get(URL + "?status=blocked")
    assert _ids(resp) == [one_of_each[ListingStatus.BLOCKED].pk]


def test_unknown_status_is_400_with_the_declared_key(auth_client, one_of_each):
    resp = auth_client.get(URL, {"status": "sould"})
    assert resp.status_code == 400
    assert resp.data["localizable_error"] == "error.400.listing_invalid_status_filter"


def test_one_bad_value_among_good_ones_still_400s(auth_client, one_of_each):
    resp = auth_client.get(URL + "?status=draft,nonsense")
    assert resp.status_code == 400


def test_empty_status_parameter_is_no_filter(auth_client, one_of_each):
    resp = auth_client.get(URL + "?status=")
    assert resp.status_code == 200
    assert resp.data["count"] == len(ALL_STATUSES)


# --- the row shape -------------------------------------------------------


def test_row_carries_both_axes(auth_client, user):
    """A published listing under re-review: the one sentence only the owner
    is owed, and it cannot be derived from ``status``."""
    Listing.objects.create(
        owner=user,
        category_id="7",
        status=ListingStatus.PUBLISHED,
        moderation_status=ModerationStatus.PENDING,
    )
    row = auth_client.get(URL).data["items"][0]
    assert row["status"] == ListingStatus.PUBLISHED
    assert row["moderation_status"] == ModerationStatus.PENDING


def test_a_never_published_draft_renders_from_its_twins(auth_client, user):
    """The published fields are empty on a draft — without the twins the
    drafts tab is a column of blank rows."""
    Listing.objects.create(
        owner=user,
        category_id="7",
        status=ListingStatus.DRAFT,
        title_draft="Nice bike",
        price_draft="200.00",
        images_draft=["product/abc"],
    )
    row = auth_client.get(URL).data["items"][0]
    assert row["title"] == ""
    assert row["title_draft"] == "Nice bike"
    assert row["price_draft"] == "200.00"
    assert row["images_draft"] == ["product/abc"]


def test_card_fields_are_the_public_card_plus_the_owner_ones(auth_client, user):
    Listing.objects.create(owner=user, category_id="7")
    row = auth_client.get(URL).data["items"][0]
    for field in ("id", "title", "price", "currency", "images", "status",
                  "features_title", "features_badges", "is_favorited"):
        assert field in row, field
    for field in ("moderation_status", "title_draft", "price_draft",
                  "images_draft", "created_at", "updated_at"):
        assert field in row, field
    # Not an owner-detail read: the heavy projections stay off the card.
    assert "features_search" not in row
    assert "description" not in row


def test_favorited_annotation_is_present(auth_client, user):
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    auth_client.post(f"/listings/listings/{listing.pk}/favorite/")
    row = auth_client.get(URL).data["items"][0]
    assert row["is_favorited"] is True


# --- pagination ----------------------------------------------------------


def test_limit_and_anchor_walk_the_pages(auth_client, user):
    made = [Listing.objects.create(owner=user, category_id="7") for _ in range(5)]
    newest_first = [row.pk for row in reversed(made)]

    first = auth_client.get(URL, {"limit": 2})
    assert first.status_code == 200
    assert _ids(first) == newest_first[:2]
    assert first.data["has_next"] is True
    assert first.data["has_prev"] is False

    second = auth_client.get(
        URL, {"limit": 2, "anchor": first.data["next_anchor"], "direction": "next"}
    )
    assert _ids(second) == newest_first[2:4]
    assert second.data["has_prev"] is True

    third = auth_client.get(
        URL, {"limit": 2, "anchor": second.data["next_anchor"], "direction": "next"}
    )
    assert _ids(third) == newest_first[4:]
    assert third.data["has_next"] is False


def test_the_filter_survives_a_page_turn(auth_client, user):
    drafts = [
        Listing.objects.create(
            owner=user, category_id="7", status=ListingStatus.DRAFT
        )
        for _ in range(3)
    ]
    Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    first = auth_client.get(URL, {"status": "draft", "limit": 2})
    assert _ids(first) == [drafts[2].pk, drafts[1].pk]
    second = auth_client.get(
        URL,
        {
            "status": "draft",
            "limit": 2,
            "anchor": first.data["next_anchor"],
            "direction": "next",
        },
    )
    assert _ids(second) == [drafts[0].pk]


def test_envelope_matches_my_favorites(auth_client, user):
    Listing.objects.create(owner=user, category_id="7")
    mine = auth_client.get(URL).data
    favorites = auth_client.get("/listings/listings/my/favorites/").data
    assert set(mine.keys()) == set(favorites.keys())
