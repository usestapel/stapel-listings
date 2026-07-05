"""Lifecycle and moderation state machines."""
import pytest

from stapel_listings.models import (
    Listing,
    ListingStatus,
    ModerationStatus,
    TransitionError,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def listing(user):
    return Listing.objects.create(owner=user, category_id="7")


def test_allowed_transition(listing):
    assert listing.can_transition_to(ListingStatus.PENDING)
    listing.transition_to(ListingStatus.PENDING)
    assert listing.status == ListingStatus.PENDING


def test_disallowed_transition_raises(listing):
    # draft -> published is not a legal direct move (must go through pending)
    assert not listing.can_transition_to(ListingStatus.PUBLISHED)
    with pytest.raises(TransitionError):
        listing.transition_to(ListingStatus.PUBLISHED)


def test_same_status_is_noop(listing):
    listing.transition_to(ListingStatus.DRAFT)  # no raise, no change
    assert listing.status == ListingStatus.DRAFT


def test_entering_published_emits_published(user, capture_events):
    published = capture_events("listing.published")
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.transition_to(ListingStatus.PUBLISHED)
    assert len(published) == 1
    assert listing.published_at is not None


def test_leaving_published_emits_removed(user, capture_events):
    removed = capture_events("listing.removed")
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.transition_to(ListingStatus.PUBLISHED)
    listing.transition_to(ListingStatus.SOLD)
    assert len(removed) == 1
    assert removed[0].payload["reason"] == ListingStatus.SOLD


def test_moderation_approved_publishes(user, capture_events):
    published = capture_events("listing.published")
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.apply_moderation("approved")
    assert listing.moderation_status == ModerationStatus.APPROVED
    assert listing.status == ListingStatus.PUBLISHED
    assert len(published) == 1


def test_moderation_rejected(user):
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.apply_moderation("rejected", note="spam")
    assert listing.moderation_status == ModerationStatus.REJECTED
    assert listing.status == ListingStatus.REJECTED
    assert listing.moderation_note == "spam"


def test_moderation_needs_review_keeps_pending(user):
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.apply_moderation("needs_review")
    assert listing.moderation_status == ModerationStatus.NEEDS_REVIEW
    assert listing.status == ListingStatus.PENDING


def test_unknown_moderation_decision_raises(user):
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    with pytest.raises(ValueError):
        listing.apply_moderation("banana")


def test_failing_emit_rolls_back_transition(user, monkeypatch):
    """Atomicity: if the listing.published emit fails, the status change rolls
    back — never committed-but-unannounced (no published-but-unindexed row)."""
    from stapel_listings import events

    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)

    def boom(_listing):
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "emit_listing_published", boom)
    with pytest.raises(RuntimeError):
        listing.transition_to(ListingStatus.PUBLISHED)

    listing.refresh_from_db()
    assert listing.status == ListingStatus.PENDING


def test_failing_emit_rolls_back_moderation_approval(user, monkeypatch):
    """Atomicity: a failed publish emit rolls back BOTH the moderation flip and
    the lifecycle transition (one verdict, one transaction)."""
    from stapel_listings import events

    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)

    def boom(_listing):
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "emit_listing_published", boom)
    with pytest.raises(RuntimeError):
        listing.apply_moderation("approved")

    listing.refresh_from_db()
    assert listing.status == ListingStatus.PENDING
    assert listing.moderation_status == ModerationStatus.PENDING


def test_soft_delete_hides_and_emits_removed_if_indexed(user, capture_events):
    removed = capture_events("listing.removed")
    listing = Listing.objects.create(owner=user, category_id="7", status=ListingStatus.PENDING)
    listing.transition_to(ListingStatus.PUBLISHED)
    listing.delete()
    assert listing.is_deleted
    assert Listing.objects.filter(pk=listing.pk).count() == 0  # hidden by manager
    assert Listing.all_objects.filter(pk=listing.pk).count() == 1
    # one removed for SOLD-like leave... here published->deleted => one removed
    assert removed[-1].payload["reason"] == "deleted"
