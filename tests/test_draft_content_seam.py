"""``listings.draft_content`` — the owner-scoped draft read another service makes.

The defect this seam closes: a composer's analysis service is addressed by a
draft id and has no request body to read content out of. The only read it
could reach was the PUBLIC detail one, which serves the published columns —
empty on a listing that has never been published. It therefore analysed
nothing, hashed two empty strings, and its screening stage reported the
listing as having no content, while ``images_draft`` held both photographs.
"""
import json
from pathlib import Path

import jsonschema
import pytest
from stapel_core.comm import call

import stapel_listings

pytestmark = pytest.mark.django_db

SCHEMA = json.loads(
    (
        Path(stapel_listings.__file__).parent
        / "schemas"
        / "functions"
        / "listings.draft_content.json"
    ).read_text()
)


def _call(listing, owner):
    return call(
        "listings.draft_content",
        {"listing_id": listing.pk, "owner_id": str(owner.pk)},
    )


# --- the twins are what a composer sees ----------------------------------


def test_the_draft_twins_are_what_the_call_answers(draft_listing, user):
    answer = _call(draft_listing, user)

    assert answer["title"] == "Toyota Camry"
    assert answer["description"] == "A well kept car in great condition."
    assert answer["images"] == ["product/abc123"]
    assert answer["features"]["mileage"] == {"type": "int", "value": 42000}
    assert answer["category_id"] == "7"
    assert answer["owner_id"] == str(user.pk)
    assert answer["is_empty"] is False


def test_a_published_listing_with_no_twins_falls_back_to_the_columns(user, db):
    from stapel_listings.models import Listing, ListingStatus

    listing = Listing.objects.create(
        owner=user,
        category_id="7",
        title="iPhone 13",
        description="Green, 128 GB.",
        images=["product/published"],
        status=ListingStatus.PUBLISHED,
    )

    answer = _call(listing, user)

    assert answer["title"] == "iPhone 13"
    assert answer["description"] == "Green, 128 GB."
    assert answer["images"] == ["product/published"]
    assert answer["is_empty"] is False


def test_an_edit_in_progress_wins_over_what_is_published(user, db):
    """The opposite order to ``moderation_content`` on purpose.

    Screening judges what is LIVE; a composer works on what the seller is
    writing right now, and must see the edit rather than the old text.
    """
    from stapel_listings.models import Listing, ListingStatus

    listing = Listing.objects.create(
        owner=user,
        category_id="7",
        title="iPhone 13",
        description="Green, 128 GB.",
        images=["product/published"],
        title_draft="iPhone 13 Pro",
        images_draft=["product/new1", "product/new2"],
        status=ListingStatus.PUBLISHED,
    )

    answer = _call(listing, user)

    assert answer["title"] == "iPhone 13 Pro"
    assert answer["images"] == ["product/new1", "product/new2"]
    # The half that was not edited still answers from the published column.
    assert answer["description"] == "Green, 128 GB."


def test_a_genuinely_empty_draft_says_so(user, db):
    from stapel_listings.models import Listing

    listing = Listing.objects.create(owner=user, category_id="7")

    answer = _call(listing, user)

    assert answer["is_empty"] is True
    assert answer["title"] == ""
    assert answer["images"] == []
    assert answer["features"] == {}


def test_whitespace_is_not_content(user, db):
    from stapel_listings.models import Listing

    listing = Listing.objects.create(
        owner=user, category_id="7", title_draft="   ", description_draft="\n"
    )

    assert _call(listing, user)["is_empty"] is True


def test_a_categoryless_draft_answers_a_null_category(user, db):
    from stapel_listings.models import Listing

    listing = Listing.objects.create(owner=user, title_draft="Something")

    assert _call(listing, user)["category_id"] is None


# --- owner scope ----------------------------------------------------------


def test_somebody_elses_draft_is_not_readable(draft_listing, other_user):
    with pytest.raises(Exception) as caught:
        _call(draft_listing, other_user)
    assert "not found" in str(caught.value)


def test_a_missing_owner_id_is_refused(draft_listing):
    with pytest.raises(Exception):
        call("listings.draft_content", {"listing_id": draft_listing.pk, "owner_id": ""})


def test_not_yours_and_not_there_are_worded_identically(draft_listing, other_user):
    """No existence oracle over other people's drafts."""
    missing = None
    forbidden = None
    try:
        call("listings.draft_content", {"listing_id": 10**9, "owner_id": str(other_user.pk)})
    except Exception as exc:  # noqa: BLE001
        missing = str(exc)
    try:
        _call(draft_listing, other_user)
    except Exception as exc:  # noqa: BLE001
        forbidden = str(exc)

    assert missing and forbidden
    assert missing.replace(str(10**9), "X") == forbidden.replace(str(draft_listing.pk), "X")


# --- the contract file --------------------------------------------------


def test_the_schema_accepts_the_payload_and_refuses_a_bare_id():
    jsonschema.validate({"listing_id": 1, "owner_id": "u-1"}, SCHEMA)
    jsonschema.validate({"listing_id": "1", "owner_id": "u-1"}, SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"listing_id": 1}, SCHEMA)
