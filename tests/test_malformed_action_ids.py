"""A malformed id in an action payload must not become a poison pill.

``user.deleted`` / ``user.merged`` / ``moderation.completed`` carry ids as
plain strings — the consumed contracts say ``{"type": "string"}`` and
``jsonschema`` does not enforce ``format: uuid`` — so a bad id reaches the
handler. Django answers a key it cannot coerce to the column's type with
``django.core.exceptions.ValidationError``, which is NOT a subclass of
``ValueError``: a guard that catches only ``(ValueError, TypeError)`` lets it
escape, ``consume_actions`` re-raises to the bus, and the event is redelivered
forever over a payload no retry can repair.

Pinned here: every such handler ACKs the malformed payload (returns without
raising) and touches no rows.
"""
import types
import uuid

import pytest
from stapel_core.comm import emit

from stapel_listings.actions import (
    handle_moderation_completed,
    handle_user_deleted,
    handle_user_merged,
)
from stapel_listings.models import Favorite, Listing, ListingStatus

pytestmark = pytest.mark.django_db

BAD_IDS = ["not-a-uuid", "", "  ", "42", "['x']"]


def _event(**payload):
    return types.SimpleNamespace(payload=payload, event_id=str(uuid.uuid4()))


@pytest.fixture
def rows(db, user, other_user):
    """One listing owned by ``user`` and hearted by ``other_user``."""
    listing = Listing.objects.create(
        owner=user, category_id="7", title="Camry", status=ListingStatus.PUBLISHED
    )
    Favorite.objects.create(user=other_user, listing=listing)
    return listing


def _snapshot():
    return (
        sorted(Listing.all_objects.values_list("id", "owner_id", "status")),
        sorted(Favorite.objects.values_list("user_id", "listing_id")),
    )


def test_user_deleted_with_a_malformed_id_acks_and_erases_nothing(rows):
    before = _snapshot()
    for bad in BAD_IDS:
        emit("user.deleted", {"user_id": bad})
    assert _snapshot() == before


def test_user_deleted_without_a_user_id_acks(rows):
    before = _snapshot()
    handle_user_deleted(_event())
    handle_user_deleted(_event(user_id=None))
    assert _snapshot() == before


def test_user_merged_with_a_malformed_id_acks_and_moves_nothing(rows, user, other_user):
    """Both directions: a bad *from* id, and — the second door — a bad *into*
    id while the guest genuinely owns rows here."""
    before = _snapshot()
    for bad in BAD_IDS:
        emit("user.merged", {"from_user_id": bad, "into_user_id": str(other_user.id)})
        emit("user.merged", {"from_user_id": str(user.id), "into_user_id": bad})
    assert _snapshot() == before


def test_user_merged_without_ids_acks(rows):
    before = _snapshot()
    handle_user_merged(_event())
    handle_user_merged(_event(from_user_id=None, into_user_id=None))
    assert _snapshot() == before


def test_moderation_completed_with_a_malformed_target_acks(rows):
    before = _snapshot()
    for bad in BAD_IDS[:1] + BAD_IDS[2:]:
        handle_moderation_completed(_event(target_key=bad, decision="approved"))
    assert _snapshot() == before


def test_a_wellformed_unknown_user_still_takes_the_quiet_path(rows):
    """The guard must not swallow the ordinary "never seen this id" case into
    something noisier — a stranger's id is a clean no-op, as before."""
    before = _snapshot()
    stranger = str(uuid.uuid4())
    emit("user.deleted", {"user_id": stranger})
    emit("user.merged", {"from_user_id": stranger, "into_user_id": stranger})
    assert _snapshot() == before


def test_a_real_deletion_still_erases(user, other_user):
    """The guard is narrow: a valid id still runs the erasure."""
    listing = Listing.objects.create(owner=user, category_id="7", title="Camry")
    Favorite.objects.create(user=other_user, listing=listing)

    emit("user.deleted", {"user_id": str(user.id)})

    assert not Listing.all_objects.filter(owner=user).exists()
    assert not Favorite.objects.exists()


def test_unknown_user_model_row_is_not_confused_with_a_bad_id(user):
    """A survivor id that parses but has no row here still RAISES when the
    guest owns rows — the retry signal must survive the widened guard."""
    from stapel_core.comm.exceptions import ActionDeliveryError

    from stapel_listings.actions import MergeTargetNotReady

    Listing.objects.create(owner=user, category_id="7", title="Camry")
    with pytest.raises(ActionDeliveryError) as excinfo:
        emit(
            "user.merged",
            {"from_user_id": str(user.id), "into_user_id": str(uuid.uuid4())},
        )
    (cause,) = excinfo.value.errors
    assert isinstance(cause, MergeTargetNotReady)
