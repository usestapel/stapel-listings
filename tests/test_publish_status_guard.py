"""A takedown is not undone by publishing again.

``POST /publish/`` accepted a listing in any status and ``publish_listing``
wrote ``status = PENDING`` past the FSM. Under ``MODERATION_GATE="post"`` (and
``AUTO_APPROVE_ON_PUBLISH``) a BLOCKED listing then went straight back into the
window, either through the bare door or through the owner's BLOCKED -> DRAFT
edge followed by a publish. A listing that carries a standing rejection now
waits for a verdict whatever the gate, and the door refuses statuses whose way
back is a transition, not a publication (PAUSED, SOLD).
"""
import pytest

from stapel_listings.models import Listing, ListingStatus, ModerationStatus
from stapel_listings.services import publish as publish_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def taken_down(draft_listing, settings):
    """Published under the post gate, then taken down by a moderator."""
    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}
    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("rejected", note="Counterfeit goods")
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.BLOCKED
    return draft_listing


def _publish(client, listing):
    return client.post(f"/listings/listings/{listing.pk}/publish/")


def test_post_gate_republish_of_a_blocked_listing_waits_for_a_verdict(
    taken_down, owner_client, capture_events
):
    submitted = capture_events("listing.submitted")
    published = capture_events("listing.published")

    resp = _publish(owner_client, taken_down)

    assert resp.status_code == 200
    taken_down.refresh_from_db()
    assert taken_down.status == ListingStatus.PENDING
    assert taken_down.moderation_status == ModerationStatus.PENDING
    assert not Listing.objects.published().filter(pk=taken_down.pk).exists()
    assert len(submitted) == 1
    assert published == []


def test_post_gate_blocked_to_draft_then_publish_is_not_live(taken_down, owner_client):
    resp = owner_client.post(
        f"/listings/listings/{taken_down.pk}/transition/", {"to": "draft"}, format="json"
    )
    assert resp.status_code == 200, resp.content
    resp = _publish(owner_client, taken_down)
    assert resp.status_code == 200
    taken_down.refresh_from_db()
    assert taken_down.status == ListingStatus.PENDING


def test_auto_approve_does_not_reinstate_a_takedown(taken_down, settings):
    settings.STAPEL_LISTINGS = {"AUTO_APPROVE_ON_PUBLISH": True}
    publish_service.publish_listing(taken_down)
    taken_down.refresh_from_db()
    assert taken_down.status == ListingStatus.PENDING
    assert taken_down.moderation_status == ModerationStatus.PENDING


def test_an_approval_after_resubmission_puts_it_back(taken_down):
    publish_service.publish_listing(taken_down)
    taken_down.apply_moderation("approved")
    taken_down.refresh_from_db()
    assert taken_down.status == ListingStatus.PUBLISHED


@pytest.mark.parametrize("status", [ListingStatus.PAUSED, ListingStatus.SOLD])
def test_publish_refuses_statuses_whose_way_back_is_a_transition(
    draft_listing, owner_client, status
):
    Listing.objects.filter(pk=draft_listing.pk).update(status=status)
    resp = _publish(owner_client, draft_listing)
    assert resp.status_code == 409
    body = resp.json()
    assert body["localizable_error"] == "error.409.invalid_listing_transition"
    assert body["params"] == {"from_status": status}
    draft_listing.refresh_from_db()
    assert draft_listing.status == status


def test_post_gate_first_publish_still_goes_live(draft_listing, settings, owner_client):
    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}
    resp = _publish(owner_client, draft_listing)
    assert resp.status_code == 200
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.PUBLISHED
