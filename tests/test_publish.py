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


def test_publish_promotes_lat_lon_with_geohash(draft_listing, stub_geo):
    """§63/geo-stamp-defect: nullable lat/lon ride the same draft->publish
    promotion as geohash, and the geohash itself is now server-computed
    (Listing.save() -> compute_geohash_draft(), see test_geohash_stamp.py)
    rather than expected from the caller.
    """
    from decimal import Decimal

    draft_listing.lat_draft = Decimal("52.520008")
    draft_listing.lon_draft = Decimal("13.404954")
    draft_listing.save()

    expected_geohash = draft_listing.geohash_draft
    assert expected_geohash  # stamped by save(), not blank

    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.geohash == expected_geohash
    assert draft_listing.lat == Decimal("52.520008")
    assert draft_listing.lon == Decimal("13.404954")


def test_publish_lat_lon_default_null_stays_null(draft_listing):
    """No coordinates in the draft -> published lat/lon stay NULL (nullable canon)."""
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.lat is None
    assert draft_listing.lon is None
    assert draft_listing.geohash == ""


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


# --- re-moderation of a LIVE listing rides the moderation axis -----------
#
# Before 0.5 a re-publish assigned ``status = pending`` past the FSM: no
# event, and the listing dropped out of every public read for the duration of
# re-moderation. The model now: content goes live immediately, only
# ``moderation_status`` moves, and a rejecting verdict takes it down through
# the published -> blocked edge that already exists.


@pytest.fixture
def live_listing(draft_listing):
    """``draft_listing`` published and approved — a listing the public sees."""
    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")
    assert draft_listing.status == ListingStatus.PUBLISHED
    return draft_listing


def _edit(listing, title="Toyota Camry 2019"):
    listing.title_draft = title
    listing.save()


def test_republish_of_a_live_listing_keeps_it_published(live_listing):
    from stapel_listings.models import Listing

    _edit(live_listing)
    publish_service.publish_listing(live_listing)
    live_listing.refresh_from_db()

    assert live_listing.status == ListingStatus.PUBLISHED
    assert live_listing.moderation_status == ModerationStatus.PENDING
    # Visibility is the business status and nothing else: the edit stays in
    # every public read while re-moderation runs.
    assert Listing.objects.published().filter(pk=live_listing.pk).exists()
    assert live_listing.is_active is True
    assert live_listing.title == "Toyota Camry 2019"


def test_republish_of_a_live_listing_emits_updated_and_submitted(
    live_listing, capture_events
):
    """Both facts, once each: the index re-pulls, moderation re-screens."""
    updated = capture_events("listing.updated")
    submitted = capture_events("listing.submitted")
    removed = capture_events("listing.removed")
    published = capture_events("listing.published")

    _edit(live_listing)
    publish_service.publish_listing(live_listing)

    assert len(updated) == 1
    assert len(submitted) == 1
    # It never left the indexed set, so neither boundary event fires.
    assert removed == [] and published == []
    assert updated[0].payload["status"] == ListingStatus.PUBLISHED
    assert submitted[0].payload["listing_id"] == live_listing.pk
    assert submitted[0].payload["title"] == "Toyota Camry 2019"


def test_republish_intake_carries_the_edited_content(live_listing):
    """What a screener pulls after the re-publish is the NEW content.

    The intake event carries identity (stapel-moderation ignores its content
    fields and reads through ``listings.moderation_content`` at screening
    time), so the guarantee that matters is that the promotion is committed
    before the moderation request goes out.
    """
    from stapel_core.comm import call

    live_listing.title_draft = "Toyota Camry 2019"
    live_listing.description_draft = "Now with a new description entirely."
    live_listing.save()
    publish_service.publish_listing(live_listing)

    content = call("listings.moderation_content", {"listing_id": live_listing.pk})
    assert content["title"] == "Toyota Camry 2019"
    assert content["text"] == "Now with a new description entirely."
    assert content["status"] == ListingStatus.PUBLISHED
    assert content["moderation_status"] == ModerationStatus.PENDING


def test_approving_a_re_moderated_edit_touches_only_the_moderation_axis(
    live_listing, capture_events
):
    published = capture_events("listing.published")
    removed = capture_events("listing.removed")

    _edit(live_listing)
    publish_service.publish_listing(live_listing)
    live_listing.apply_moderation("approved", note="looks fine")

    live_listing.refresh_from_db()
    assert live_listing.status == ListingStatus.PUBLISHED
    assert live_listing.moderation_status == ModerationStatus.APPROVED
    # It was published the whole time — no lifecycle move, no index churn.
    assert published == [] and removed == []


def test_rejecting_a_re_moderated_edit_takes_the_listing_down(
    live_listing, capture_events
):
    from stapel_listings.models import Listing

    removed = capture_events("listing.removed")

    _edit(live_listing, title="Counterfeit Camry")
    publish_service.publish_listing(live_listing)
    live_listing.apply_moderation("rejected", note="Counterfeit goods")

    live_listing.refresh_from_db()
    assert live_listing.status == ListingStatus.BLOCKED
    assert live_listing.moderation_status == ModerationStatus.REJECTED
    assert len(removed) == 1
    assert removed[0].payload["reason"] == ListingStatus.BLOCKED
    assert not Listing.objects.published().filter(pk=live_listing.pk).exists()


def test_rejection_over_the_bus_takes_a_re_moderated_edit_down(
    live_listing, capture_events
):
    """The verdict as stapel-moderation actually delivers it."""
    from stapel_core.comm import emit

    removed = capture_events("listing.removed")
    _edit(live_listing)
    publish_service.publish_listing(live_listing)

    emit(
        "moderation.completed",
        {
            "case_id": "c-9",
            "target_type": "listing",
            "target_key": str(live_listing.pk),
            "decision": "rejected",
            "reason_code": "illegal_content",
        },
    )

    live_listing.refresh_from_db()
    assert live_listing.status == ListingStatus.BLOCKED
    assert len(removed) == 1


def test_republish_with_auto_approve_stays_published(live_listing, settings):
    """No moderation module deployed: the edit is live and approved at once."""
    settings.STAPEL_LISTINGS = {"AUTO_APPROVE_ON_PUBLISH": True}
    _edit(live_listing)
    publish_service.publish_listing(live_listing)
    live_listing.refresh_from_db()

    assert live_listing.status == ListingStatus.PUBLISHED
    assert live_listing.moderation_status == ModerationStatus.APPROVED


def test_first_publish_still_waits_for_the_verdict(draft_listing, capture_events):
    """Pre-moderation for a listing the public has never seen — unchanged."""
    from stapel_listings.models import Listing

    updated = capture_events("listing.updated")
    submitted = capture_events("listing.submitted")

    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.PENDING
    assert draft_listing.moderation_status == ModerationStatus.PENDING
    assert not Listing.objects.published().filter(pk=draft_listing.pk).exists()
    assert updated == []  # nothing was indexed, nothing to update
    assert len(submitted) == 1


def test_republish_of_a_paused_listing_still_goes_to_pending(draft_listing):
    """Only a listing in an INDEXED status takes the live path.

    A paused listing is invisible either way, so re-publishing it is a first
    publication again — the pre-0.5 flow, unchanged.
    """
    publish_service.publish_listing(draft_listing)
    draft_listing.apply_moderation("approved")
    draft_listing.transition_to(ListingStatus.PAUSED)

    _edit(draft_listing)
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.PENDING


# --- MODERATION_GATE = "post": publish first, review after ---------------
#
# The pre gate holds FIRST publication for a verdict — correct only where a
# moderator exists to give one. On a stand with none, every listing sits in
# PENDING forever: invisible, unindexed, and nothing in the system will ever
# move it. The post gate is the big-board model: the listing goes live in the
# same flow that requests moderation, review happens on the live content, and
# a rejecting verdict takes it down through the published -> blocked edge the
# live-republish path already uses. Unlike AUTO_APPROVE_ON_PUBLISH this is
# NOT a verdict: moderation_status stays PENDING, the case still opens, and a
# moderator's answer still lands.


def test_post_gate_first_publish_goes_live_still_awaiting_review(
    draft_listing, settings, capture_events
):
    from stapel_listings.models import Listing

    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}
    submitted = capture_events("listing.submitted")
    published = capture_events("listing.published")

    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.PUBLISHED
    # Live is not approved: the review is still owed, and only a verdict
    # (or AUTO_APPROVE, which is a different statement) may move this axis.
    assert draft_listing.moderation_status == ModerationStatus.PENDING
    # Both facts announced once each: moderation gets its case, the index
    # gets its document.
    assert len(submitted) == 1
    assert len(published) == 1
    assert Listing.objects.published().filter(pk=draft_listing.pk).exists()


def test_post_gate_approval_touches_only_the_moderation_axis(
    draft_listing, settings, capture_events
):
    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}
    publish_service.publish_listing(draft_listing)
    published = capture_events("listing.published")
    removed = capture_events("listing.removed")

    draft_listing.apply_moderation("approved", note="looks fine")
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.PUBLISHED
    assert draft_listing.moderation_status == ModerationStatus.APPROVED
    # It was published the whole time — no lifecycle move, no index churn.
    assert published == [] and removed == []


def test_post_gate_rejection_takes_the_live_listing_down(
    draft_listing, settings, capture_events
):
    from stapel_listings.models import Listing

    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}
    publish_service.publish_listing(draft_listing)
    removed = capture_events("listing.removed")

    draft_listing.apply_moderation("rejected", note="Counterfeit goods")
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.BLOCKED
    assert draft_listing.moderation_status == ModerationStatus.REJECTED
    assert len(removed) == 1
    assert not Listing.objects.published().filter(pk=draft_listing.pk).exists()


def test_explicit_pre_gate_is_the_default_behavior(draft_listing, settings):
    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "pre"}
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    assert draft_listing.status == ListingStatus.PENDING
    assert draft_listing.moderation_status == ModerationStatus.PENDING


def test_post_gate_atomicity_a_failed_submit_emit_rolls_back_the_publish(
    draft_listing, settings, monkeypatch
):
    """The go-live and the moderation request commit together or not at all.

    Under the post gate a listing that went PUBLISHED without its
    listing.submitted would be live content NOBODY will ever review — worse
    than the pre gate's stuck-PENDING, because it is public.
    """
    from stapel_listings import events

    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}

    def boom(_listing):
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "emit_listing_submitted", boom)
    with pytest.raises(RuntimeError):
        publish_service.publish_listing(draft_listing)

    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.DRAFT
