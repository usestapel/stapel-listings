"""``transition {"to": "pending"}`` is a publication, not a status hop.

The owner edges DRAFT -> PENDING and EXPIRED -> PENDING ran ``transition_to``:
the row became PENDING with no ``listing.submitted`` (no moderation case ever
opened, nothing would move it), and a DRAFT skipped validation and the
draft -> published promotion.
"""
import pytest

from stapel_listings.models import Listing, ListingStatus, ModerationStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


def _submit(client, listing):
    return client.post(
        f"/listings/listings/{listing.pk}/transition/", {"to": "pending"}, format="json"
    )


def test_submitting_a_draft_opens_a_moderation_case(draft_listing, owner_client, capture_events):
    submitted = capture_events("listing.submitted")
    resp = _submit(owner_client, draft_listing)
    assert resp.status_code == 200, resp.content
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.PENDING
    assert draft_listing.moderation_status == ModerationStatus.PENDING
    assert draft_listing.title == "Toyota Camry"
    assert len(submitted) == 1


def test_submitting_an_invalid_draft_is_refused(draft_listing, owner_client):
    draft_listing.images_draft = []
    draft_listing.save()
    resp = _submit(owner_client, draft_listing)
    assert resp.status_code == 400
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.DRAFT


def test_renewing_an_expired_listing_opens_a_case(draft_listing, owner_client, capture_events):
    Listing.objects.filter(pk=draft_listing.pk).update(status=ListingStatus.EXPIRED)
    submitted = capture_events("listing.submitted")
    resp = _submit(owner_client, draft_listing)
    assert resp.status_code == 200, resp.content
    assert len(submitted) == 1
