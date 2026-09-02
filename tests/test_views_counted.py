"""Opening a listing counts a view — once per viewer per window, never the owner's.

The legacy catalog carried ``UserAdView`` as an external read-cache and it was
deliberately not ported (``models.py`` "engagement is a first-class
``Favorite``"). What it left behind was a board where nothing a buyer does is
visible: no seller could tell a listing nobody opens from one everybody opens,
and a returning buyer could not tell a card they had already read from a new
one.

The two costs this module refuses to pay are named in the tests below and not
only in a docstring: a database write per PAGE READ, and a counter that a
seller can inflate by reloading their own page.
"""
import pytest

from stapel_listings.models import Listing, ListingStatus, ListingView

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def published(db, other_user):
    return Listing.objects.create(
        owner=other_user,
        category_id="7",
        title="Toyota Camry",
        status=ListingStatus.PUBLISHED,
    )


def _record(listing, **kwargs):
    from stapel_listings.services.engagement import record_view

    return record_view(listing, **kwargs)


# --------------------------------------------------------------------------
# the count
# --------------------------------------------------------------------------


def test_a_view_is_counted(published, user):
    assert _record(published, user=user) is True
    published.refresh_from_db()
    assert published.view_count == 1


def test_the_same_viewer_in_the_window_counts_once(published, user):
    assert _record(published, user=user) is True
    assert _record(published, user=user) is False
    assert _record(published, user=user) is False
    published.refresh_from_db()
    assert published.view_count == 1


def test_a_repeat_open_writes_nothing(published, user):
    """The hot path is a READ. The first open pays one write; a reload pays none.

    Asserted by counting queries rather than by reading the docstring's
    promise, because "cheap" is the kind of claim that rots silently: a
    presenter that grew an ``update_or_create`` would still pass every count
    assertion above.
    """
    from django.test.utils import CaptureQueriesContext
    from django.db import connection

    _record(published, user=user)

    with CaptureQueriesContext(connection) as captured:
        assert _record(published, user=user) is False
    assert list(captured) == [], "a repeat open inside the window must not touch the DB"


def test_the_owner_does_not_count_their_own_opens(published, other_user):
    """`other_user` owns it. A seller reloading their own page is not demand."""
    assert _record(published, user=other_user) is False
    published.refresh_from_db()
    assert published.view_count == 0


def test_an_unpublished_listing_is_not_counted(db, user, other_user):
    draft = Listing.objects.create(owner=other_user, category_id="7")
    assert _record(draft, user=user) is False
    draft.refresh_from_db()
    assert draft.view_count == 0


def test_two_viewers_count_twice(published, user, other_user):
    from django.contrib.auth import get_user_model

    carol = get_user_model().objects.create(username="carol", email="c@example.com")
    assert _record(published, user=user) is True
    assert _record(published, user=carol) is True
    published.refresh_from_db()
    assert published.view_count == 2


# --------------------------------------------------------------------------
# anonymous viewers, honestly
# --------------------------------------------------------------------------


def test_an_anonymous_session_is_deduplicated(published):
    assert _record(published, session_key="sess-a") is True
    assert _record(published, session_key="sess-a") is False
    assert _record(published, session_key="sess-b") is True
    published.refresh_from_db()
    assert published.view_count == 2


def test_an_anonymous_viewer_leaves_no_durable_row(published):
    """No ``ListingView`` for a stranger: the row exists to answer «have I
    seen this», and there is nobody to answer it for.

    Keeping one per (session, listing) would be a table that grows with
    traffic and answers no question — the legacy read-cache's exact shape.
    """
    _record(published, session_key="sess-a")
    assert ListingView.objects.count() == 0
    published.refresh_from_db()
    assert published.view_count == 1


def test_a_viewer_with_no_session_falls_back_to_a_coarse_key(published):
    """No session and no user: the client fingerprint is IP + User-Agent.

    Declared as coarse rather than dressed up: two people behind one NAT with
    the same browser are one viewer to this counter, and the count is a
    floor, never an inflated number.
    """
    assert _record(published, client_key="203.0.113.7|Mozilla/5.0") is True
    assert _record(published, client_key="203.0.113.7|Mozilla/5.0") is False
    assert _record(published, client_key="203.0.113.9|Mozilla/5.0") is True
    published.refresh_from_db()
    assert published.view_count == 2


def test_a_viewer_with_nothing_at_all_is_not_counted(published):
    """No user, no session, no fingerprint — no way to deduplicate, so no
    count. A number that rises once per HTTP request is not a view count."""
    assert _record(published) is False
    published.refresh_from_db()
    assert published.view_count == 0


# --------------------------------------------------------------------------
# «просмотрено» — the per-viewer flag
# --------------------------------------------------------------------------


def test_an_authenticated_view_is_remembered(published, user):
    _record(published, user=user)
    row = ListingView.objects.get(user=user, listing=published)
    assert row.first_seen_at is not None

    annotated = Listing.objects.with_viewed(user).get(pk=published.pk)
    assert annotated.viewed is True


def test_viewed_is_false_for_a_viewer_who_has_not_opened_it(published, user):
    annotated = Listing.objects.with_viewed(user).get(pk=published.pk)
    assert annotated.viewed is False


def test_viewed_is_null_for_anonymous(published):
    class Anon:
        is_authenticated = False

    annotated = Listing.objects.with_viewed(Anon()).get(pk=published.pk)
    assert annotated.viewed is None, "unknown is not the same as «not viewed»"


def test_a_second_window_refreshes_last_seen_without_double_counting(published, user):
    from django.core.cache import cache

    _record(published, user=user)
    first = ListingView.objects.get(user=user, listing=published).last_seen_at

    # The window elapsed: the dedup key is what expires, nothing else.
    cache.clear()
    assert _record(published, user=user) is True

    row = ListingView.objects.get(user=user, listing=published)
    assert row.last_seen_at > first
    published.refresh_from_db()
    assert published.view_count == 2


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------


def test_the_detail_read_counts_and_serves_the_flags(api_client, published, user):
    api_client.force_authenticate(user)
    body = api_client.get(f"/listings/listings/{published.pk}/").json()

    assert body["view_count"] == 1, "the opened listing counts its own open"
    assert body["viewed"] is False, "false on the open that first sees it"
    assert body["is_favorited"] is False

    body = api_client.get(f"/listings/listings/{published.pk}/").json()
    assert body["view_count"] == 1, "a reload is the same view"
    assert body["viewed"] is True


def test_the_card_carries_the_flags(api_client, published, user):
    api_client.force_authenticate(user)
    api_client.get(f"/listings/listings/{published.pk}/")

    row = api_client.get("/listings/listings/").json()["items"][0]
    assert row["view_count"] == 1
    assert row["viewed"] is True
    assert row["is_favorited"] is False


def test_an_anonymous_card_says_unknown_not_false(api_client, published):
    api_client.get(f"/listings/listings/{published.pk}/")
    row = api_client.get("/listings/listings/").json()["items"][0]
    assert row["viewed"] is None
    assert row["is_favorited"] is None
    assert row["view_count"] == 1, "the count is public; who viewed is not"


def test_a_listing_read_does_not_count_for_its_owner_over_http(
    api_client, published, other_user
):
    api_client.force_authenticate(other_user)
    body = api_client.get(f"/listings/listings/{published.pk}/").json()
    assert body["view_count"] == 0


# --------------------------------------------------------------------------
# the batched read other modules use
# --------------------------------------------------------------------------


def test_the_engagement_function_answers_a_keyed_batch(published, user):
    from stapel_core.comm import call

    _record(published, user=user)

    answer = call(
        "listings.engagement",
        {"keys": [str(published.pk)], "user_id": str(user.pk)},
    )
    assert answer == {
        str(published.pk): {"view_count": 1, "viewed": True, "is_favorited": False}
    }


def test_the_engagement_function_without_a_user_says_unknown(published, user):
    from stapel_core.comm import call

    _record(published, user=user)

    answer = call("listings.engagement", {"keys": [str(published.pk)]})
    assert answer[str(published.pk)] == {
        "view_count": 1,
        "viewed": None,
        "is_favorited": None,
    }


def test_the_engagement_function_omits_an_absent_listing(published):
    from stapel_core.comm import call

    answer = call("listings.engagement", {"keys": [str(published.pk), "999999"]})
    assert set(answer) == {str(published.pk)}


def test_the_engagement_endpoint_answers_a_page_in_one_call(api_client, published, user):
    api_client.force_authenticate(user)
    api_client.get(f"/listings/listings/{published.pk}/")

    body = api_client.get("/listings/listings/engagement/", {"ids": f"{published.pk},999999"}).json()
    assert body["items"] == {
        str(published.pk): {"view_count": 1, "viewed": True, "is_favorited": False}
    }


def test_the_engagement_endpoint_is_open_to_a_guest(api_client, published):
    """`view_count` is public and the per-viewer flags answer null, so a
    storefront makes the SAME request signed in or not — a guest's grid is
    not a second code path."""
    api_client.get(f"/listings/listings/{published.pk}/")
    body = api_client.get("/listings/listings/engagement/", {"ids": str(published.pk)}).json()
    row = body["items"][str(published.pk)]
    assert row == {"view_count": 1, "viewed": None, "is_favorited": None}


def test_the_engagement_batch_is_capped(api_client, published, settings):
    settings.STAPEL_LISTINGS = {"ENGAGEMENT_BATCH_LIMIT": 1}
    ids = ",".join(str(n) for n in range(1, 50))
    body = api_client.get("/listings/listings/engagement/", {"ids": ids}).json()
    assert len(body["items"]) <= 1
