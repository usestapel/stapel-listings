"""``category_id`` holds an ID. It has never been able to say so.

The measured failure: listings 243, 244 and 245 on one live stand carry
``category_id = "32/149/163"`` — a search *path* stored in the opaque category
id column. A path is what ``stapel-search`` puts in ``?category=`` (see
``suggest.py``: ``"category": "/".join(path_ids)``), and it is a perfectly good
value *there*; it is not a category id, and this column has no other meaning.

Nothing rejected it. ``Listing.category_id`` is a bare ``CharField`` with no
validators, ``ListingDraftSerializer`` has no ``validate_category_id``, and the
categories seam is only consulted at publish — so a draft carrying a path is
written, stored and served without a single reader ever asking whether the id
is an id. The three rows are harmless only because they never got published:
``category_schema.get_feature_configs("32/149/163")`` reaches
``Category.objects.get(pk="32/149/163")``, which raises ``ValueError`` rather
than ``DoesNotExist`` — the wrong exception type, so it escapes the provider's
own ``except`` and surfaces as a 500 instead of the ``LookupError`` every
caller here is written against.

These tests pin this half — the column can only take an id. The other half,
a malformed id reaching the provider and coming back as the ``LookupError``
every caller here is written against, is pinned in stapel-categories beside
the provider itself.
"""
import pytest
from django.core.exceptions import ValidationError

from stapel_listings.models import Listing

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


# What a category id may look like: an int-like string (stapel-categories'
# AutoField) or a UUID (a deployment whose catalogue is
# keyed that way). Both are opaque tokens; neither has a separator in it.
VALID_IDS = [
    "7",
    "163",
    "9f8d3c1e-6b2a-4a77-9c31-0f5d2b7e4a10",
    "elektronika",
    "cat_163",
]

# Every one of these was accepted before. The first is the value actually on
# the stand.
INVALID_IDS = [
    "32/149/163",   # the SERP's ?category= path — the measured defect
    "/163",
    "163/",
    "tools/power",  # what the composer pair's own fixtures still assert
    "163 164",
]

# NOT invalid since 0.21.4: the empty string is how a client clears the field,
# and "no category yet" is a legal state for a draft. It is normalised to NULL
# on the way in so the column has one spelling of it.


class TestTheWritePathRefusesAPath:
    """One choke point: every client — composer, admin-adjacent API, probe,
    curl — writes through ``ListingDraftSerializer``."""

    @pytest.mark.parametrize("category_id", INVALID_IDS)
    def test_create_refuses(self, auth_client, category_id):
        resp = auth_client.post(
            "/listings/listings/", {"category_id": category_id}, format="json"
        )
        assert resp.status_code == 400, resp.content
        assert "category_id" in resp.data

    @pytest.mark.parametrize("category_id", VALID_IDS)
    def test_create_still_accepts_an_id(self, auth_client, category_id):
        resp = auth_client.post(
            "/listings/listings/", {"category_id": category_id}, format="json"
        )
        assert resp.status_code == 201, resp.content
        assert Listing.objects.get(pk=resp.data["id"]).category_id == category_id

    @pytest.mark.parametrize("category_id", ["", None])
    def test_create_accepts_no_category_at_all(self, auth_client, category_id):
        """0.21.4: the composer opens the row on the first photo, before the
        category step. Both spellings of "none" land as NULL."""
        resp = auth_client.post(
            "/listings/listings/", {"category_id": category_id}, format="json"
        )

        assert resp.status_code == 201, resp.content
        assert resp.data["category_id"] is None
        assert Listing.objects.get(pk=resp.data["id"]).category_id is None

    def test_save_draft_refuses_a_path(self, auth_client, user):
        """The PATCH door too, not only create: the three stand rows are
        drafts, and a draft is edited far more often than it is created."""
        listing = Listing.objects.create(owner=user, category_id="7")

        resp = auth_client.post(
            f"/listings/listings/{listing.pk}/save-draft/",
            {"category_id": "32/149/163"},
            format="json",
        )

        assert resp.status_code == 400, resp.content
        listing.refresh_from_db()
        assert listing.category_id == "7"


class TestTheRuleLivesOnTheModel:
    """Declared once, on the field, so it reaches the serializer, the admin
    form and ``full_clean()`` together.

    A ``validate_category_id`` written on the serializer alone would leave the
    Django admin — where ``category_id`` is editable and is NOT in
    ``readonly_fields`` — able to type a path straight in, which is one of the
    two writers that could not be ruled out for the three rows on the stand.
    """

    def test_full_clean_refuses_a_path(self, user):
        listing = Listing(owner=user, category_id="32/149/163")

        with pytest.raises(ValidationError) as exc:
            listing.full_clean(exclude=["title", "description"])

        assert "category_id" in exc.value.message_dict

    def test_full_clean_accepts_an_id(self, user):
        Listing(owner=user, category_id="163").full_clean(
            exclude=["title", "description"]
        )
