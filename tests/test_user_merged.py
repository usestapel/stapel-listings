"""``user.merged`` — a guest's saved listings survive signing in.

stapel-auth absorbs an anonymous account into an existing one and then DELETES
the guest row; every row this module owns hangs off it by CASCADE. What is
pinned here:

* the carry-over walk goes through the endpoints a browser actually calls —
  the guest hearts a listing over HTTP, the event is delivered, and the
  survivor's ``my/favorites`` answers with it;
* the ``uniq_user_listing_fav`` collision (both accounts hearted the same
  listing) folds to exactly one row instead of raising;
* ``Listing.owner`` moves too, soft-deleted rows included;
* the handler is idempotent, and a no-op for ids it has never seen;
* a guest with rows to carry and a survivor this service has not projected
  yet RAISES rather than reporting success, so the outbox redelivers instead
  of silently discarding the transfer.
"""
import pytest
from stapel_core.comm import emit
from stapel_core.django.users.models import User

from stapel_listings.actions import MergeTargetNotReady
from stapel_listings.models import Favorite, Listing, ListingStatus

pytestmark = pytest.mark.django_db

FAVORITES_URL = "/listings/listings/my/favorites/"


@pytest.fixture
def guest(db):
    return User.create_anonymous_user()


def _merge(from_user, into_user):
    emit(
        "user.merged",
        {
            "from_user_id": str(from_user.id),
            "into_user_id": str(into_user.id),
            "reason": "anonymous_promotion",
        },
    )


def _published(owner, title="Camry"):
    return Listing.objects.create(
        owner=owner, category_id="7", title=title, status=ListingStatus.PUBLISHED
    )


def test_guest_favorite_survives_the_merge_over_http(api_client, guest, user, other_user):
    """The walk: heart it as a guest, sign in, still have it — all over REST."""
    listing = _published(other_user)

    api_client.force_authenticate(user=guest)
    resp = api_client.post(f"/listings/listings/{listing.pk}/favorite/")
    assert resp.status_code == 200, resp.content

    # The survivor has nothing saved yet.
    api_client.force_authenticate(user=user)
    assert api_client.get(FAVORITES_URL).data["count"] == 0

    _merge(guest, user)

    resp = api_client.get(FAVORITES_URL)
    assert resp.status_code == 200, resp.content
    assert [row["id"] for row in resp.data["items"]] == [listing.pk]


def test_collision_folds_to_one_row_under_the_survivor(api_client, guest, user, other_user):
    listing = _published(other_user)

    api_client.force_authenticate(user=guest)
    assert api_client.post(f"/listings/listings/{listing.pk}/favorite/").status_code == 200
    api_client.force_authenticate(user=user)
    assert api_client.post(f"/listings/listings/{listing.pk}/favorite/").status_code == 200

    _merge(guest, user)  # would raise IntegrityError on a blind .update()

    assert Favorite.objects.filter(listing=listing).count() == 1
    assert Favorite.objects.filter(listing=listing, user=user).exists()
    assert not Favorite.objects.filter(user=guest).exists()
    assert [row["id"] for row in api_client.get(FAVORITES_URL).data["items"]] == [listing.pk]


def test_non_colliding_and_colliding_favorites_in_one_merge(guest, user, other_user):
    shared = _published(other_user, "Shared")
    guest_only = _published(other_user, "Guest only")
    Favorite.objects.create(user=guest, listing=shared)
    Favorite.objects.create(user=guest, listing=guest_only)
    Favorite.objects.create(user=user, listing=shared)

    _merge(guest, user)

    assert set(
        Favorite.objects.filter(user=user).values_list("listing_id", flat=True)
    ) == {shared.pk, guest_only.pk}
    assert Favorite.objects.filter(user=guest).count() == 0


def test_listing_owner_is_reassigned(guest, user):
    live = Listing.objects.create(owner=guest, category_id="7", title="Bike")
    gone = Listing.objects.create(owner=guest, category_id="7", title="Skis")
    gone.delete()  # soft delete

    _merge(guest, user)

    assert Listing.objects.get(pk=live.pk).owner_id == user.id
    assert Listing.all_objects.get(pk=gone.pk).owner_id == user.id
    assert Listing.all_objects.filter(owner=guest).count() == 0


def test_second_delivery_changes_nothing(guest, user, other_user):
    listing = _published(other_user)
    Favorite.objects.create(user=guest, listing=listing)
    owned = Listing.objects.create(owner=guest, category_id="7")

    _merge(guest, user)
    before = list(
        Favorite.objects.filter(user=user).values_list("id", "listing_id")
    )
    _merge(guest, user)  # at-least-once delivery

    assert list(Favorite.objects.filter(user=user).values_list("id", "listing_id")) == before
    assert Favorite.objects.count() == 1
    assert Listing.objects.get(pk=owned.pk).owner_id == user.id


def test_guest_owning_nothing_is_a_clean_no_op(guest, user):
    _merge(guest, user)
    assert Favorite.objects.count() == 0
    assert Listing.all_objects.count() == 0


def test_unknown_user_ids_are_a_clean_no_op(user):
    """Neither side owns anything here, so there is nothing to carry."""
    import uuid

    stranger = uuid.uuid4()
    emit(
        "user.merged",
        {
            "from_user_id": str(stranger),
            "into_user_id": str(user.id),
            "reason": "anonymous_promotion",
        },
    )
    emit(
        "user.merged",
        {
            "from_user_id": str(user.id),
            "into_user_id": str(stranger),
            "reason": "anonymous_promotion",
        },
    )
    assert Favorite.objects.count() == 0


# ── the survivor has not been projected here yet ────────────────────────


def test_unknown_survivor_raises_and_moves_nothing(guest, other_user):
    """The guest HAS rows: returning success would let the outbox mark the
    event delivered and lose them forever. Raise so it is redelivered."""
    import uuid

    from stapel_core.comm.exceptions import ActionDeliveryError

    listing = _published(other_user)
    Favorite.objects.create(user=guest, listing=listing)
    owned = Listing.objects.create(owner=guest, category_id="7")
    survivor_id = uuid.uuid4()

    with pytest.raises(ActionDeliveryError) as excinfo:
        emit(
            "user.merged",
            {
                "from_user_id": str(guest.id),
                "into_user_id": str(survivor_id),
                "reason": "anonymous_promotion",
            },
        )

    (cause,) = excinfo.value.errors
    assert isinstance(cause, MergeTargetNotReady)
    # An operator staring at a redelivery loop can name both accounts.
    assert str(guest.id) in str(cause) and str(survivor_id) in str(cause)

    # Nothing half-moved: a redelivery finds the rows intact under the guest.
    assert Favorite.objects.filter(user=guest, listing=listing).exists()
    assert Listing.all_objects.get(pk=owned.pk).owner_id == guest.id


def test_redelivery_after_the_survivor_appears_completes_the_transfer(guest, other_user):
    """The raise is a real retry path, not just a louder failure."""
    import uuid

    from stapel_core.comm.exceptions import ActionDeliveryError

    listing = _published(other_user)
    Favorite.objects.create(user=guest, listing=listing)
    owned = Listing.objects.create(owner=guest, category_id="7")
    survivor_id = uuid.uuid4()
    payload = {
        "from_user_id": str(guest.id),
        "into_user_id": str(survivor_id),
        "reason": "anonymous_promotion",
    }

    with pytest.raises(ActionDeliveryError):
        emit("user.merged", payload)

    # The survivor's user projection lands...
    survivor = User.objects.create(id=survivor_id, username="late", email="late@example.com")

    emit("user.merged", payload)  # ...and the outbox redelivers.

    assert Favorite.objects.filter(user=survivor, listing=listing).exists()
    assert not Favorite.objects.filter(user=guest).exists()
    assert Listing.all_objects.get(pk=owned.pk).owner_id == survivor.id


def test_unknown_survivor_with_an_empty_guest_stays_quiet(guest):
    """No rows to carry — a genuine no-op, and the retry loop must not start."""
    import uuid

    emit(
        "user.merged",
        {
            "from_user_id": str(guest.id),
            "into_user_id": str(uuid.uuid4()),
            "reason": "anonymous_promotion",
        },
    )
    assert Favorite.objects.count() == 0


def test_second_delivery_after_a_completed_merge_never_raises(guest, user, other_user):
    """Post-merge the guest owns nothing, so redelivery takes the quiet path
    even though the guest row itself may be long gone."""
    listing = _published(other_user)
    Favorite.objects.create(user=guest, listing=listing)

    _merge(guest, user)
    _merge(guest, user)  # must not raise MergeTargetNotReady

    assert Favorite.objects.filter(user=user, listing=listing).exists()


def test_merge_into_self_is_a_no_op(guest, other_user):
    listing = _published(other_user)
    Favorite.objects.create(user=guest, listing=listing)
    _merge(guest, guest)
    assert Favorite.objects.filter(user=guest, listing=listing).exists()
