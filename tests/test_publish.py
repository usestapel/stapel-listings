"""Publish workflow: draft->publish promotion and the four feature projections."""
import pytest

from stapel_listings.models import ListingStatus, ModerationStatus
from stapel_listings.services import publish as publish_service

pytestmark = pytest.mark.django_db


def test_publish_promotes_draft_fields(draft_listing):
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.PENDING
    assert draft_listing.moderation_status == ModerationStatus.PENDING
    assert draft_listing.title == "Toyota Camry"
    assert draft_listing.description == "A well kept car in great condition."
    assert draft_listing.price == draft_listing.price_draft
    assert draft_listing.images == ["product/abc123"]
    assert draft_listing.expires_at is not None


def test_publish_builds_four_projections(draft_listing):
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    # features: ordered DAO list carrying both values
    slugs = {dao["slug"] for dao in draft_listing.features}
    assert slugs == {"mileage", "condition"}

    # features_title: mileage flagged show_at_title
    assert [d["slug"] for d in draft_listing.features_title] == ["mileage"]

    # features_badges: condition flagged show_as_badge
    assert [d["slug"] for d in draft_listing.features_badges] == ["condition"]

    # features_search: {slug: [values]}, numbers stay numbers
    assert draft_listing.features_search == {
        "mileage": [42000],
        "condition": ["used"],
    }


def test_publish_requires_image_when_configured(draft_listing, settings):
    draft_listing.images_draft = []
    draft_listing.save()
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        publish_service.publish_listing(draft_listing)


def test_publish_missing_mandatory_feature_is_invalid(draft_listing):
    draft_listing.features_draft = {}  # mileage is mandatory
    draft_listing.save()
    result = publish_service.validate_draft(draft_listing)
    assert result.valid is False
    assert any(r.slug == "mileage" for r in result.results)


def test_failing_submit_emit_rolls_back_publish(draft_listing, monkeypatch):
    """Atomicity: if the listing.submitted emit fails, the whole promotion rolls
    back — the listing stays DRAFT, never PENDING-without-an-event."""
    from stapel_listings import events

    def boom(_listing):
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "emit_listing_submitted", boom)
    with pytest.raises(RuntimeError):
        publish_service.publish_listing(draft_listing)

    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.DRAFT


def test_auto_approve_on_publish_publishes_immediately(draft_listing, settings):
    settings.STAPEL_LISTINGS = {"AUTO_APPROVE_ON_PUBLISH": True}
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.PUBLISHED
    assert draft_listing.moderation_status == ModerationStatus.APPROVED
