"""Authorization of the listing surface — who may read and write what.

Two holes are pinned here, both fixed in 0.6.2:

* ``PUT``/``PATCH`` were the plain ``ModelViewSet`` writes over
  ``Listing.objects.all()`` under ``IsAuthenticatedOrReadOnly``, so any
  authenticated caller could write any listing's draft fields. They now pass
  the module's one ownership gate, ``views.ListingViewSet._get_own`` — the
  same gate ``save-draft``, ``publish``, the transitions and ``destroy`` use,
  and with the same shapes (404 absent, 403 someone else's).
* ``GET /{pk}/`` served a draft, a rejected and a blocked listing to anyone
  holding the id. It now resolves through ``ListingQuerySet.visible_to``:
  the indexed statuses for everyone, plus one's own rows whatever their
  status. A hidden row 404s exactly as an absent one does.

Staff are deliberately NOT special anywhere here: this module gives them no
API bypass, they moderate through the admin and the ``moderation.completed``
contract (MODULE.md, "Admin categories" / the moderation seam).
"""
import pytest

from stapel_listings.models import Favorite, Listing, ListingStatus

pytestmark = pytest.mark.django_db

# Every status the detail read must hide from a stranger — everything outside
# ``INDEXED_STATUSES``, with the three the design names explicitly (draft,
# pending, blocked) among them.
HIDDEN_STATUSES = [
    ListingStatus.DRAFT,
    ListingStatus.PENDING,
    ListingStatus.BLOCKED,
    ListingStatus.REJECTED,
    ListingStatus.PAUSED,
    ListingStatus.EXPIRED,
    ListingStatus.SOLD,
    ListingStatus.ARCHIVED,
]


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def staff_user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(
        username="mod", email="mod@example.com", is_staff=True, is_superuser=True
    )


@pytest.fixture
def staff_client(api_client, staff_user):
    api_client.force_authenticate(user=staff_user)
    return api_client


@pytest.fixture
def others_draft(other_user):
    """A draft that belongs to somebody else."""
    return Listing.objects.create(
        owner=other_user, category_id="7", title_draft="Their bike"
    )


# --- PUT / PATCH: the ownership gate ---------------------------------------


def test_owner_can_patch_own_draft(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.patch(
        f"/listings/listings/{listing.pk}/", {"title_draft": "Mine"}, format="json"
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.title_draft == "Mine"


def test_owner_can_put_own_draft(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.put(
        f"/listings/listings/{listing.pk}/",
        {"category_id": "7", "title_draft": "Mine"},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.title_draft == "Mine"


@pytest.mark.parametrize("method", ["patch", "put"])
def test_other_user_cannot_write_someone_elses_draft(auth_client, others_draft, method):
    payload = {"category_id": "7", "title_draft": "Hijacked"}
    resp = getattr(auth_client, method)(
        f"/listings/listings/{others_draft.pk}/", payload, format="json"
    )
    assert resp.status_code == 403
    # Same envelope every other owner operation returns for a stranger.
    assert resp.data["localizable_error"] == "error.403.listing_not_owner"
    others_draft.refresh_from_db()
    assert others_draft.title_draft == "Their bike"


@pytest.mark.parametrize("method", ["patch", "put"])
def test_staff_get_no_write_bypass(staff_client, others_draft, method):
    resp = getattr(staff_client, method)(
        f"/listings/listings/{others_draft.pk}/",
        {"category_id": "7", "title_draft": "Moderated"},
        format="json",
    )
    assert resp.status_code == 403
    others_draft.refresh_from_db()
    assert others_draft.title_draft == "Their bike"


@pytest.mark.parametrize("method", ["patch", "put"])
def test_anonymous_cannot_write(api_client, others_draft, method):
    resp = getattr(api_client, method)(
        f"/listings/listings/{others_draft.pk}/",
        {"category_id": "7", "title_draft": "Hijacked"},
        format="json",
    )
    # 403 under DRF's default authenticators, 401 wherever the host installs
    # one that challenges — either way the write never lands.
    assert resp.status_code in (401, 403)
    others_draft.refresh_from_db()
    assert others_draft.title_draft == "Their bike"


def test_write_to_absent_listing_is_404(auth_client):
    resp = auth_client.patch(
        "/listings/listings/999999/", {"title_draft": "x"}, format="json"
    )
    assert resp.status_code == 404
    assert resp.data["localizable_error"] == "error.404.listing_not_found"


# --- GET /{pk}/: the visibility filter -------------------------------------


@pytest.mark.parametrize("status", HIDDEN_STATUSES)
def test_stranger_cannot_read_an_unindexed_listing(auth_client, other_user, status):
    listing = Listing.objects.create(owner=other_user, category_id="7", status=status)
    resp = auth_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", HIDDEN_STATUSES)
def test_anonymous_cannot_read_an_unindexed_listing(api_client, user, status):
    listing = Listing.objects.create(owner=user, category_id="7", status=status)
    resp = api_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 404


@pytest.mark.parametrize("status", HIDDEN_STATUSES)
def test_owner_reads_own_listing_in_any_status(auth_client, user, status):
    listing = Listing.objects.create(owner=user, category_id="7", status=status)
    resp = auth_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 200, resp.content
    assert resp.data["id"] == listing.pk


def test_published_listing_stays_publicly_readable(api_client, user):
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    resp = api_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 200, resp.content


def test_staff_get_no_read_bypass(staff_client, other_user):
    listing = Listing.objects.create(
        owner=other_user, category_id="7", status=ListingStatus.BLOCKED
    )
    resp = staff_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 404


def test_hidden_is_indistinguishable_from_absent(auth_client, other_user):
    """The uniform-404 canon: no oracle for "this id exists"."""
    hidden = Listing.objects.create(
        owner=other_user, category_id="7", status=ListingStatus.DRAFT
    )
    hidden_resp = auth_client.get(f"/listings/listings/{hidden.pk}/")
    absent_resp = auth_client.get("/listings/listings/999999/")
    assert hidden_resp.status_code == absent_resp.status_code == 404
    assert hidden_resp.data == absent_resp.data


def test_soft_deleted_own_listing_stays_hidden(auth_client, user):
    """``visible_to`` widens the owner's view, never past the soft delete."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    listing.delete()
    resp = auth_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 404


def test_list_still_shows_published_only(api_client, user, other_user):
    Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PUBLISHED)
    Listing.objects.create(owner=other_user, category_id="7", status=ListingStatus.DRAFT)
    resp = api_client.get("/listings/listings/")
    assert resp.status_code == 200
    assert len(resp.data["items"]) == 1


def test_own_draft_is_not_leaked_into_the_list(auth_client, user):
    """The list is the public index for everyone, owner included (ask #1)."""
    Listing.objects.create(owner=user, category_id="7", status=ListingStatus.DRAFT)
    resp = auth_client.get("/listings/listings/")
    assert resp.status_code == 200
    assert resp.data["items"] == []


# --- the same hole on the favorites routes ---------------------------------


def test_cannot_favorite_a_strangers_draft(auth_client, user, others_draft):
    resp = auth_client.post(f"/listings/listings/{others_draft.pk}/favorite/")
    assert resp.status_code == 404
    assert not Favorite.objects.filter(user=user, listing=others_draft).exists()


def test_can_favorite_own_unpublished_listing(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(f"/listings/listings/{listing.pk}/favorite/")
    assert resp.status_code == 200, resp.content
    assert Favorite.objects.filter(user=user, listing=listing).exists()


def test_my_favorites_drops_a_listing_that_left_the_index(
    auth_client, user, other_user
):
    listing = Listing.objects.create(
        owner=other_user, category_id="7", status=ListingStatus.PUBLISHED
    )
    Favorite.objects.create(user=user, listing=listing)

    resp = auth_client.get("/listings/listings/my/favorites/")
    assert [row["id"] for row in resp.data["items"]] == [listing.pk]

    Listing.objects.filter(pk=listing.pk).update(status=ListingStatus.BLOCKED)
    resp = auth_client.get("/listings/listings/my/favorites/")
    assert resp.data["items"] == []
