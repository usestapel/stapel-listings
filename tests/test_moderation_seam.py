"""The moderation seam: target-generic verdicts, takedown, content pull.

Before 0.4 the consumed contract was ``required: ["listing_id"]`` with
``additionalProperties: false``, so a target-generic moderation queue could not
address this module at all; and ``apply_moderation("rejected")`` assigned
``status`` directly, so a takedown of a LIVE listing was both unexpressible
(no edge, no state) and silent (no ``listing.removed``).
"""
import json
from pathlib import Path

import jsonschema
import pytest

import stapel_listings
from stapel_listings.models import Listing, ListingStatus, ModerationStatus

pytestmark = pytest.mark.django_db

SCHEMA = json.loads(
    (
        Path(stapel_listings.__file__).parent
        / "schemas"
        / "consumes"
        / "moderation.completed.json"
    ).read_text()
)


@pytest.fixture
def live_listing(user):
    """A published, approved listing — the state a takedown acts on."""
    listing = Listing.objects.create(
        owner=user,
        category_id="7",
        title="Toyota Camry",
        status=ListingStatus.PENDING,
    )
    listing.apply_moderation("approved")
    assert listing.status == ListingStatus.PUBLISHED
    return listing


# --- the widened contract accepts old AND new payloads -------------------


def test_schema_accepts_the_legacy_listing_id_payload():
    jsonschema.validate({"listing_id": 1, "decision": "approved"}, SCHEMA)
    jsonschema.validate({"listing_id": 1, "decision": "rejected", "note": "spam"}, SCHEMA)


def test_schema_accepts_the_target_generic_payload():
    jsonschema.validate(
        {
            "case_id": "c-1",
            "target_type": "listing",
            "target_key": "1",
            "decision": "rejected",
            "reason_code": "counterfeit",
            "note": "Trademark violation",
        },
        SCHEMA,
    )


def test_schema_accepts_emitter_owned_extras_and_the_dismissed_decision():
    """The producer owns this contract; growth must not break the consumer."""
    jsonschema.validate(
        {
            "case_id": 42,
            "target_type": "review",
            "target_key": "9",
            "decision": "dismissed",
            "source": "human",
            "decided_at": "2026-08-21T10:00:00Z",
        },
        SCHEMA,
    )


def test_schema_still_rejects_an_unknown_decision():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"target_key": "1", "decision": "maybe"}, SCHEMA)


# --- the handler is target-generic ---------------------------------------


def test_verdict_by_target_type_and_key(user):
    from stapel_core.comm import emit

    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    emit(
        "moderation.completed",
        {
            "case_id": "c-1",
            "target_type": "listing",
            "target_key": str(listing.pk),
            "decision": "approved",
        },
    )
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PUBLISHED
    assert listing.moderation_status == ModerationStatus.APPROVED


def test_verdict_for_another_target_type_is_ignored(user):
    from stapel_core.comm import emit

    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    emit(
        "moderation.completed",
        {"target_type": "review", "target_key": str(listing.pk), "decision": "rejected"},
    )
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PENDING
    assert listing.moderation_status == ModerationStatus.PENDING


def test_target_type_name_is_configurable(user, settings):
    """A composite may register listings under another target type name."""
    from stapel_core.comm import emit

    settings.STAPEL_LISTINGS = {"MODERATION_TARGET_TYPE": "classified_ad"}
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    emit(
        "moderation.completed",
        {"target_type": "classified_ad", "target_key": str(listing.pk),
         "decision": "approved"},
    )
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PUBLISHED


def test_reason_code_lands_in_the_note_when_no_note_is_sent(user):
    from stapel_core.comm import emit

    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    emit(
        "moderation.completed",
        {"target_type": "listing", "target_key": str(listing.pk),
         "decision": "rejected", "reason_code": "counterfeit"},
    )
    listing.refresh_from_db()
    assert listing.moderation_note == "counterfeit"


def test_dismissed_leaves_the_target_untouched(live_listing, capture_events):
    from stapel_core.comm import emit

    removed = capture_events("listing.removed")
    emit(
        "moderation.completed",
        {"target_type": "listing", "target_key": str(live_listing.pk),
         "decision": "dismissed"},
    )
    live_listing.refresh_from_db()
    assert live_listing.status == ListingStatus.PUBLISHED
    assert live_listing.moderation_status == ModerationStatus.APPROVED
    assert removed == []


# --- takedown: published -> blocked, and it announces itself -------------


def test_takedown_of_a_live_listing_blocks_and_emits_removed(live_listing, capture_events):
    removed = capture_events("listing.removed")

    live_listing.apply_moderation("rejected", note="Counterfeit goods")

    assert live_listing.status == ListingStatus.BLOCKED
    assert live_listing.moderation_status == ModerationStatus.REJECTED
    assert live_listing.moderation_note == "Counterfeit goods"
    assert len(removed) == 1
    assert removed[0].payload["reason"] == ListingStatus.BLOCKED
    assert removed[0].payload["listing_id"] == live_listing.pk


def test_takedown_over_the_bus(live_listing, capture_events):
    """End to end: the verdict a moderation module actually emits."""
    from stapel_core.comm import emit

    removed = capture_events("listing.removed")
    emit(
        "moderation.completed",
        {
            "case_id": "c-7",
            "target_type": "listing",
            "target_key": str(live_listing.pk),
            "decision": "rejected",
            "reason_code": "illegal_content",
        },
    )
    live_listing.refresh_from_db()
    assert live_listing.status == ListingStatus.BLOCKED
    assert len(removed) == 1


def test_a_blocked_listing_is_invisible(live_listing):
    live_listing.apply_moderation("rejected")
    assert Listing.objects.published().filter(pk=live_listing.pk).count() == 0
    assert live_listing.is_active is False


def test_pre_publication_rejection_still_goes_to_rejected(user, capture_events):
    removed = capture_events("listing.removed")
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    listing.apply_moderation("rejected")
    assert listing.status == ListingStatus.REJECTED
    assert removed == []  # it was never indexed


def test_reinstatement_after_appeal_republishes(live_listing, capture_events):
    published = capture_events("listing.published")
    live_listing.apply_moderation("rejected")
    assert live_listing.status == ListingStatus.BLOCKED

    live_listing.apply_moderation("approved", note="Appeal upheld")

    assert live_listing.status == ListingStatus.PUBLISHED
    assert live_listing.moderation_status == ModerationStatus.APPROVED
    assert len(published) == 1


def test_blocked_is_reachable_only_from_published(user):
    from stapel_listings.models import TransitionError

    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    with pytest.raises(TransitionError):
        listing.transition_to(ListingStatus.BLOCKED)


def test_owner_may_archive_a_blocked_listing(live_listing):
    live_listing.apply_moderation("rejected")
    live_listing.transition_to(ListingStatus.ARCHIVED)
    assert live_listing.status == ListingStatus.ARCHIVED


def test_failing_removed_emit_rolls_back_the_takedown(live_listing, monkeypatch):
    """One verdict, one transaction — including the takedown's event."""
    from stapel_listings import events

    def boom(_listing, **_kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "emit_listing_removed", boom)
    with pytest.raises(RuntimeError):
        live_listing.apply_moderation("rejected")

    live_listing.refresh_from_db()
    assert live_listing.status == ListingStatus.PUBLISHED
    assert live_listing.moderation_status == ModerationStatus.APPROVED
