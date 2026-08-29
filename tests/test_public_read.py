"""The public-read posture of the listing surface, as a contract.

A classified storefront is read by strangers: most of its traffic arrives from
a search engine, has no session and will never get one. That browsing works
today is not an accident of which permission class happened to be typed on
``ListingViewSet`` — it is the shape the surface is *for*, and it is the one
property no test named until now. ``test_authz.py`` proves what a stranger may
NOT see; ``test_anonymous_writes.py`` proves what a *guest* (an anonymous but
authenticated user) may not write. Neither pins the read itself: swap
``IsAuthenticatedOrReadOnly`` for ``IsAuthenticated`` and both files stay
green while every catalogue page on the internet turns into a 401.

The three halves pinned here:

* **Reads are open with no credentials at all.** List and detail answer 200 to
  a client that has never authenticated — not to a guest account, to nobody.
* **Openness is not leakage.** The public list is the published rows; another
  user's draft is absent from it and 404s by id, exactly as an absent row does
  (the ``visible_to`` contract of 0.6.2, restated from the anonymous side).
* **The read costs no cookie.** A response that carried ``Set-Cookie`` would
  be uncacheable at the edge and would start a session per bot; a CDN in front
  of this surface must be able to serve one cached body to everyone.

Writes stay shut: with no credentials the create is 401 — the door is
*locked*, not merely absent, so a caller learns to authenticate rather than
that the route does not exist.
"""
import pytest
from rest_framework.permissions import IsAuthenticatedOrReadOnly

from stapel_listings.models import Listing, ListingStatus
from stapel_listings.views import ListingViewSet

pytestmark = pytest.mark.django_db


@pytest.fixture
def anonymous_client(api_client):
    """No credentials whatsoever — not ``force_authenticate``d, no session.

    Deliberately distinct from ``test_anonymous_writes.anonymous_client``,
    which authenticates a real anonymous *user* row. This one is a stranger.
    """
    return api_client


@pytest.fixture
def published_listing(user):
    return Listing.objects.create(
        owner=user,
        category_id="7",
        title="Toyota Camry",
        status=ListingStatus.PUBLISHED,
    )


@pytest.fixture
def others_draft(other_user):
    return Listing.objects.create(
        owner=other_user, category_id="7", title_draft="Their bike"
    )


def _set_cookie_headers(response):
    """Every ``Set-Cookie`` the response would put on the wire."""
    return list(response.cookies.keys())


# --- the permission class itself -------------------------------------------


def test_listing_viewset_reads_are_open_to_anyone():
    """The one line the whole public catalogue rests on.

    Named so a regression to ``IsAuthenticated`` fails a test that says why,
    rather than fifty tests that say ``401 != 200``.
    """
    assert ListingViewSet.permission_classes == [IsAuthenticatedOrReadOnly]


# --- list ------------------------------------------------------------------


def test_anonymous_can_list(anonymous_client, published_listing):
    resp = anonymous_client.get("/listings/listings/")
    assert resp.status_code == 200, resp.content
    assert [row["id"] for row in resp.data["items"]] == [published_listing.pk]


def test_anonymous_list_omits_someone_elses_draft(
    anonymous_client, published_listing, others_draft
):
    resp = anonymous_client.get("/listings/listings/")
    assert resp.status_code == 200, resp.content
    ids = [row["id"] for row in resp.data["items"]]
    assert published_listing.pk in ids
    assert others_draft.pk not in ids


# --- detail ----------------------------------------------------------------


def test_anonymous_can_retrieve_a_published_listing(
    anonymous_client, published_listing
):
    resp = anonymous_client.get(f"/listings/listings/{published_listing.pk}/")
    assert resp.status_code == 200, resp.content
    assert resp.data["id"] == published_listing.pk


def test_anonymous_gets_404_on_someone_elses_draft(anonymous_client, others_draft):
    """A hidden row is indistinguishable from an absent one — no 403 that
    would confirm the id exists."""
    resp = anonymous_client.get(f"/listings/listings/{others_draft.pk}/")
    assert resp.status_code == 404, resp.content


# --- no session is started by a read ---------------------------------------


def test_anonymous_reads_set_no_cookie(
    anonymous_client, published_listing, others_draft
):
    """Cacheable at the edge, and no ``UserSession`` row per crawler."""
    for url in (
        "/listings/listings/",
        f"/listings/listings/{published_listing.pk}/",
        f"/listings/listings/{others_draft.pk}/",
    ):
        resp = anonymous_client.get(url)
        assert _set_cookie_headers(resp) == [], (url, resp.cookies)
        assert not resp.has_header("Set-Cookie"), url


# --- writes stay shut ------------------------------------------------------


def test_anonymous_cannot_create_a_listing(anonymous_client):
    """Refused, and nothing is written.

    The code is 403 under *this* suite's settings, which configure no
    authenticators, so DRF falls back to its own default and asks the first of
    them — ``SessionAuthentication`` — for a challenge it does not offer. What
    the surface promises is the refusal; the next test pins the code a real
    deployment sees.
    """
    resp = anonymous_client.post(
        "/listings/listings/", {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code in (401, 403), resp.content
    assert Listing.objects.count() == 0


def test_anonymous_create_is_401_where_a_challenge_exists(monkeypatch, api_client):
    """A fleet mounts ``JWTCookieAuthentication`` (stapel-core), which offers a
    ``WWW-Authenticate: Bearer`` challenge, so DRF answers **401**, not 403.

    That distinction is the whole difference between "sign in" and "you signed
    in and still may not" on a storefront's write door, and 401 is what the
    live surface returns. Set on the view rather than through
    ``settings.REST_FRAMEWORK``: DRF binds ``authentication_classes`` as a
    class attribute at import time, so a settings override arrives too late
    for an already-imported viewset — the test would pass for the wrong reason.
    """
    from stapel_core.django.jwt.authentication import JWTCookieAuthentication

    monkeypatch.setattr(
        ListingViewSet, "authentication_classes", [JWTCookieAuthentication]
    )
    resp = api_client.post(
        "/listings/listings/", {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code == 401, resp.content
    assert Listing.objects.count() == 0
