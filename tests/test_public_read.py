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
import decimal
import inspect

import pytest
from rest_framework.permissions import IsAuthenticatedOrReadOnly

from stapel_listings import serializers as listing_serializers
from stapel_listings.conf import listings_settings
from stapel_listings.models import Listing, ListingStatus
from stapel_listings.serializers import AudienceRedactionMixin
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


# --- the seller's pin is not public ----------------------------------------
#
# Until 0.21.0 this surface published, to anyone with curl, `geohash` at
# precision 8 (a cell roughly 38m x 19m) AND `lat`/`lon` at the column's full
# six decimal places (~11cm). For a private person selling from home that pair
# is not "roughly where the sofa is" — it is their front door, joined to their
# phone number by the same page. It shipped on a live stand.
#
# The public statement is now an AREA, and it is the same statement the
# neighbouring search card already makes: `lat`/`lon` rounded to
# PUBLIC_COORD_PRECISION decimals plus `geo_precision_km`, which is what tells
# a client to draw a circle instead of a marker.
#
# The geohash is BLANKED rather than truncated, and that is the deliberate
# part. Two independently-derived areas around one true point intersect to
# something smaller than either of them: a geohash prefix beside a rounded
# pair, where the true point sits near a cell boundary, still pins it to a
# sliver tens of metres wide. One area, one encoding, no intersection to
# exploit. `""` is a value the column already has (`blank=True, default=""`)
# and every client already handles, so the public key set does not move.
#
# The exact point survives where it is legitimate: the owner's own read, staff,
# the service transport, the search feed (whose `distance_km` is computed
# server-side from the true coordinates and is not made less accurate by any
# of this), and the composer that loads a listing for editing.

PUBLIC_LISTING_CARD_KEYS = sorted([
    "id", "title", "price", "price_base", "currency", "images",
    "features_title", "features_badges",
    "location_label", "geohash", "lat", "lon", "geo_precision_km",
    "countable", "stock_quantity", "status",
    "is_favorited", "viewed", "view_count",
])

PUBLIC_LISTING_DETAIL_KEYS = sorted([
    "id", "owner", "category_id", "title", "description", "language",
    "price", "price_base", "currency", "images",
    "location_id", "location_label", "geohash", "lat", "lon",
    "geo_precision_km",
    "features", "features_title", "features_badges", "features_search",
    "status", "moderation_status", "auto_republish",
    "countable", "stock_quantity",
    "published_at", "expires_at", "created_at", "updated_at",
    "is_favorited", "viewed", "view_count",
])

#: The seller's true pin, at the column's full precision.
PIN_LAT = decimal.Decimal("54.991686")
PIN_LON = decimal.Decimal("73.372162")
PIN_GEOHASH = "v9u0vdgv"


@pytest.fixture
def pinned_listing(user):
    """A published listing carrying its seller's exact coordinates."""
    return Listing.objects.create(
        owner=user,
        category_id="7",
        title="Стол с электроподъёмом",
        status=ListingStatus.PUBLISHED,
        location_label="Омск, Куйбышевский",
        lat=PIN_LAT,
        lon=PIN_LON,
        geohash=PIN_GEOHASH,
    )


def _decimals(value) -> int:
    """How many decimal places the serialized coordinate actually carries."""
    exponent = decimal.Decimal(str(value)).as_tuple().exponent
    return max(0, -int(exponent))


def _card_of(resp, pk):
    return next(row for row in resp.data["items"] if row["id"] == pk)


# -- the frozen key sets ----------------------------------------------------


def test_public_card_payload_is_the_frozen_key_set(anonymous_client, pinned_listing):
    """Asserted EXACTLY, like the categories catalogue's public row.

    Not "does not contain lat" — the next leak will not be called lat. An
    exact contract makes adding a public field a conscious act: extend this
    list in the same commit, with "who may read this?" answered.
    """
    resp = anonymous_client.get("/listings/listings/")
    assert resp.status_code == 200, resp.content
    assert sorted(_card_of(resp, pinned_listing.pk)) == PUBLIC_LISTING_CARD_KEYS


def test_public_detail_payload_is_the_frozen_key_set(anonymous_client, pinned_listing):
    resp = anonymous_client.get(f"/listings/listings/{pinned_listing.pk}/")
    assert resp.status_code == 200, resp.content
    assert sorted(resp.data) == PUBLIC_LISTING_DETAIL_KEYS


# -- and the precision of what those keys carry -----------------------------


@pytest.mark.parametrize("surface", ["list", "detail"])
def test_anonymous_never_gets_the_pin(anonymous_client, pinned_listing, surface):
    """Both endpoints, one answer — a rounded pair on one and a precise one on
    the other is not a policy, it is a hole with a second door."""
    if surface == "list":
        resp = anonymous_client.get("/listings/listings/")
        row = _card_of(resp, pinned_listing.pk)
    else:
        resp = anonymous_client.get(f"/listings/listings/{pinned_listing.pk}/")
        row = resp.data
    assert resp.status_code == 200, resp.content

    places = int(listings_settings.PUBLIC_COORD_PRECISION)
    assert _decimals(row["lat"]) <= places, row["lat"]
    assert _decimals(row["lon"]) <= places, row["lon"]
    assert decimal.Decimal(str(row["lat"])) == round(PIN_LAT, places)
    assert decimal.Decimal(str(row["lon"])) == round(PIN_LON, places)

    # The other encoding of the same point, closed too.
    assert row["geohash"] == ""

    # And the payload says how wide the area it just handed over is, so a
    # client draws a circle rather than believing a marker.
    assert row["geo_precision_km"] > 0


def test_the_public_area_is_wider_than_a_street(anonymous_client, pinned_listing):
    """The number, not just its presence: ~1.1km at two decimals.

    A regression that quietly raised PUBLIC_COORD_PRECISION to 5 would keep
    every other assertion in this file green while publishing the pin again.
    """
    resp = anonymous_client.get(f"/listings/listings/{pinned_listing.pk}/")
    assert resp.data["geo_precision_km"] >= 1.0, resp.data["geo_precision_km"]


# -- the owner-facing surface is a different surface -------------------------


def test_the_owner_still_sees_their_own_pin(api_client, user, pinned_listing):
    """The seller placed that pin and edits it in the composer, which loads
    `lat`/`lon`/`geohash` off this very read (`fromDetail` in
    stapel-react/listings-react). Coarsening it for the owner would move the
    listing ~1km on every save."""
    api_client.force_authenticate(user)
    resp = api_client.get(f"/listings/listings/{pinned_listing.pk}/")
    assert resp.status_code == 200, resp.content
    assert decimal.Decimal(resp.data["lat"]) == PIN_LAT
    assert decimal.Decimal(resp.data["lon"]) == PIN_LON
    assert resp.data["geohash"] == PIN_GEOHASH
    assert resp.data["geo_precision_km"] == 0


def test_the_owners_own_card_keeps_the_pin(api_client, user, pinned_listing):
    api_client.force_authenticate(user)
    resp = api_client.get("/listings/listings/my/listings/")
    assert resp.status_code == 200, resp.content
    row = _card_of(resp, pinned_listing.pk)
    assert decimal.Decimal(row["lat"]) == PIN_LAT
    assert row["geohash"] == PIN_GEOHASH


def test_another_logged_in_user_is_still_a_stranger(
    api_client, other_user, pinned_listing
):
    """Authentication is not authorisation. A buyer with an account is exactly
    as entitled to the seller's address as a crawler is."""
    api_client.force_authenticate(other_user)
    resp = api_client.get(f"/listings/listings/{pinned_listing.pk}/")
    assert resp.status_code == 200, resp.content
    assert _decimals(resp.data["lat"]) <= int(
        listings_settings.PUBLIC_COORD_PRECISION
    )
    assert resp.data["geohash"] == ""


def test_staff_sees_the_pin(api_client, other_user, pinned_listing):
    """Support answering "the courier cannot find it" needs the real point."""
    other_user.is_staff = True
    other_user.save(update_fields=["is_staff"])
    api_client.force_authenticate(other_user)
    resp = api_client.get(f"/listings/listings/{pinned_listing.pk}/")
    assert decimal.Decimal(resp.data["lat"]) == PIN_LAT
    assert resp.data["geohash"] == PIN_GEOHASH


# -- the exact point stays where the server legitimately uses it -------------


def test_the_search_feed_still_carries_the_true_point(pinned_listing):
    """`distance_km` is a derived scalar computed server-side from the true
    coordinates — it does not hand out the location, and the two-band geo work
    depends on it being exact. Coarsening the FEED would make every distance
    on the site wrong by up to a kilometre."""
    from stapel_listings.services.search_feed import build_search_document

    doc = build_search_document(pinned_listing)
    assert decimal.Decimal(str(doc["lat"])) == PIN_LAT
    assert decimal.Decimal(str(doc["lon"])) == PIN_LON
    assert doc["geohash"] == PIN_GEOHASH


# -- the gate: a new serializer cannot leak a coordinate silently ------------


class TestEveryCoordinateColumnIsGated:
    """The sibling of ``test_feature_visibility``'s mixin gate, for the other
    column family this module publishes. The leak existed because `lat`, `lon`
    and `geohash` were plain model fields: every serializer that listed them
    inherited the disclosure for free and nothing anywhere said "wait"."""

    def _serializers_emitting_coordinates(self):
        for name, obj in vars(listing_serializers).items():
            if not inspect.isclass(obj) or not hasattr(obj, "_declared_fields"):
                continue
            if getattr(obj, "__module__", None) != listing_serializers.__name__:
                continue
            meta_fields = set(getattr(getattr(obj, "Meta", None), "fields", None) or ())
            if meta_fields & set(AudienceRedactionMixin.PRECISE_GEO_FIELDS):
                yield name, obj

    def test_there_is_at_least_one_to_check(self):
        assert list(self._serializers_emitting_coordinates())

    def test_all_of_them_are_audience_aware(self):
        offenders = [
            name
            for name, cls in self._serializers_emitting_coordinates()
            if not issubclass(cls, AudienceRedactionMixin)
        ]
        assert offenders == [], (
            f"{offenders} publish a coordinate column without "
            "AudienceRedactionMixin. `lat`/`lon`/`geohash` are the seller's "
            "home address; inherit the mixin so the payload is coarsened for "
            "whoever is actually asking."
        )

    def test_the_read_serializers_are_the_ones_gated(self):
        """Names, so the gate above cannot pass by finding nothing."""
        gated = {name for name, _ in self._serializers_emitting_coordinates()}
        assert {"ListingCardSerializer", "ListingDetailSerializer"} <= gated
