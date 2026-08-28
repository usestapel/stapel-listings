"""The guest wall on the listing authorship writes (ALLOW_ANONYMOUS_WRITES).

A storefront that mints an anonymous account the moment a stranger presses
"save this listing" hands that stranger a real, authenticated session.
``IsAuthenticated`` cannot tell it from a registered one, so before this
switch existed the "sign up to sell" wall lived only in the frontend as a
disabled button and the endpoint underneath took the POST.

The switch is CLOSED by default: a deployment that mints no anonymous users
has none to reject, so the default costs it nothing.

The two halves this file has to keep apart:

* AUTHORING a listing (create / PUT / PATCH / save-draft / publish) — refused
  to a guest under the default. A seller nobody can reach again is not a
  seller.
* FAVOURITING one — never refused. That is the whole feature the anonymous
  session exists for, and breaking it would be getting this exactly backwards.

Reads are untouched in both positions.
"""
import pytest

from stapel_listings.models import Favorite, Listing, ListingStatus

pytestmark = pytest.mark.django_db

ERR_ANON = "error.403.listing_anonymous_not_allowed"


@pytest.fixture
def anonymous_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().create_anonymous_user()


@pytest.fixture
def anonymous_client(api_client, anonymous_user):
    """A guest session exactly as the storefront's heart button mints it:
    a real User row with ``is_anonymous=True`` and a valid session."""
    api_client.force_authenticate(user=anonymous_user)
    return api_client


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def open_wall(settings):
    """``ALLOW_ANONYMOUS_WRITES = True`` — "guests may sell here"."""
    settings.STAPEL_LISTINGS = {"ALLOW_ANONYMOUS_WRITES": True}


@pytest.fixture
def anon_draft(anonymous_user, stub_categories):
    """A ready-to-publish draft already owned by the guest.

    Built through the ORM on purpose: under the closed default the guest
    cannot create one over HTTP, and the point of these cases is what happens
    to the writes AFTER creation.
    """
    return Listing.objects.create(
        owner=anonymous_user,
        category_id="7",
        title_draft="Toyota Camry",
        description_draft="A well kept car in great condition.",
        price_draft="15000.00",
        currency="EUR",
        images_draft=["product/abc123"],
        features_draft={
            "mileage": {"type": "int", "value": 42000},
            "condition": {"type": "select", "value": ["used"]},
        },
    )


def test_the_guest_is_a_real_authenticated_user(anonymous_user):
    """The premise. Were this False the wall would need no switch."""
    assert anonymous_user.is_authenticated is True
    assert anonymous_user.is_anonymous is True


# --- authoring: refused by default ------------------------------------------


def test_guest_cannot_create_a_listing_by_default(anonymous_client):
    resp = anonymous_client.post(
        "/listings/listings/", {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert resp.data["localizable_error"] == ERR_ANON
    assert Listing.objects.count() == 0


@pytest.mark.parametrize("method", ["put", "patch"])
def test_guest_cannot_write_its_own_draft_by_default(
    anonymous_client, anon_draft, method
):
    resp = getattr(anonymous_client, method)(
        f"/listings/listings/{anon_draft.pk}/",
        {"category_id": "7", "title_draft": "Renamed"},
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert resp.data["localizable_error"] == ERR_ANON
    anon_draft.refresh_from_db()
    assert anon_draft.title_draft == "Toyota Camry"


def test_guest_cannot_save_draft_by_default(anonymous_client, anon_draft):
    resp = anonymous_client.post(
        f"/listings/listings/{anon_draft.pk}/save-draft/",
        {"title_draft": "Renamed"},
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert resp.data["localizable_error"] == ERR_ANON


def test_guest_cannot_publish_by_default(anonymous_client, anon_draft):
    resp = anonymous_client.post(f"/listings/listings/{anon_draft.pk}/publish/")
    assert resp.status_code == 403, resp.content
    assert resp.data["localizable_error"] == ERR_ANON
    anon_draft.refresh_from_db()
    assert anon_draft.status == ListingStatus.DRAFT


# --- authoring: allowed when the wall is opened -----------------------------


def test_guest_can_create_a_listing_when_opened(open_wall, anonymous_client):
    resp = anonymous_client.post(
        "/listings/listings/", {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code == 201, resp.content


def test_guest_can_save_draft_when_opened(open_wall, anonymous_client, anon_draft):
    resp = anonymous_client.post(
        f"/listings/listings/{anon_draft.pk}/save-draft/",
        {"title_draft": "Renamed"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    anon_draft.refresh_from_db()
    assert anon_draft.title_draft == "Renamed"


def test_guest_can_publish_when_opened(open_wall, anonymous_client, anon_draft):
    resp = anonymous_client.post(f"/listings/listings/{anon_draft.pk}/publish/")
    assert resp.status_code == 200, resp.content
    anon_draft.refresh_from_db()
    assert anon_draft.status == ListingStatus.PENDING


# --- a registered user is caught by neither position ------------------------


def test_registered_user_can_create_while_closed(auth_client):
    resp = auth_client.post(
        "/listings/listings/", {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code == 201, resp.content


def test_registered_user_can_create_while_open(open_wall, auth_client):
    resp = auth_client.post(
        "/listings/listings/", {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code == 201, resp.content


def test_registered_user_can_publish_while_closed(auth_client, draft_listing):
    resp = auth_client.post(f"/listings/listings/{draft_listing.pk}/publish/")
    assert resp.status_code == 200, resp.content


def test_registered_user_can_publish_while_open(open_wall, auth_client, draft_listing):
    resp = auth_client.post(f"/listings/listings/{draft_listing.pk}/publish/")
    assert resp.status_code == 200, resp.content


# --- favourites: the feature this whole task exists to deliver --------------


def test_guest_can_favourite_under_the_closed_default(anonymous_client, user):
    """The heart button. If this ever 403s the refusal was scoped wrong."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    resp = anonymous_client.post(f"/listings/listings/{listing.pk}/favorite/")
    assert resp.status_code == 200, resp.content
    assert resp.data["favorited"] is True
    assert Favorite.objects.filter(listing=listing).count() == 1


def test_guest_can_unfavourite_under_the_closed_default(
    anonymous_client, anonymous_user, user
):
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    Favorite.objects.create(user=anonymous_user, listing=listing)

    resp = anonymous_client.post(f"/listings/listings/{listing.pk}/unfavorite/")
    assert resp.status_code == 200, resp.content
    assert resp.data["favorited"] is False
    assert Favorite.objects.filter(listing=listing).count() == 0


def test_guest_can_read_its_favourites_under_the_closed_default(
    anonymous_client, anonymous_user, user
):
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    Favorite.objects.create(user=anonymous_user, listing=listing)

    resp = anonymous_client.get("/listings/listings/my/favorites/")
    assert resp.status_code == 200, resp.content
    assert [row["id"] for row in resp.data["items"]] == [listing.pk]


# --- reads stay open in both positions --------------------------------------


@pytest.mark.parametrize("allow", [False, True])
def test_guest_can_still_browse(settings, anonymous_client, user, allow):
    settings.STAPEL_LISTINGS = {"ALLOW_ANONYMOUS_WRITES": allow}
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )

    listed = anonymous_client.get("/listings/listings/")
    assert listed.status_code == 200, listed.content
    assert [row["id"] for row in listed.data["items"]] == [listing.pk]

    detail = anonymous_client.get(f"/listings/listings/{listing.pk}/")
    assert detail.status_code == 200, detail.content
