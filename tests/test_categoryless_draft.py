"""A draft exists before the category does.

The measured failure: a composer analyses the seller's FIRST PHOTO, and the
analysis job is addressed by the draft id — so the row has to exist before the
photo is described, long before anyone has picked a category. ``category_id``
was NOT NULL and ``ListingDraftSerializer`` inherited that as a required
field, so the create call was refused, no draft id came back, the job was
never started and the composer sat on a spinner. The client was patched to
invent a placeholder; this is the mechanism it was working around.

What these tests pin:

* a draft is created and saved with NO category, and the owner's read says
  ``"category_id": null`` — an explicit "not chosen yet", not a missing key;
* the category arrives later, on an ordinary save-draft;
* publish still refuses without one, with the existing
  ``publish_validation_failed``, at BOTH doors (the structured validate-draft
  batch and ``publish_listing`` itself);
* the NOT NULL the column gave up is kept where it was actually load-bearing:
  a row may be category-less only as a DRAFT (or an ARCHIVED one — a seller
  must still be able to put away a draft that never got that far);
* nothing category-less reaches a search index: no ``listing.*`` event is
  emitted for it, and the document it would export carries an empty category
  and a non-indexed status rather than the string "None".
"""
import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from stapel_listings.errors import ERR_400_PUBLISH_VALIDATION_FAILED
from stapel_listings.models import Listing, ListingStatus, TransitionError

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


class TestTheDraftCanBeOpenedFirst:

    def test_create_with_no_category_at_all(self, auth_client):
        """The very first call the composer makes: an empty POST."""
        resp = auth_client.post("/listings/listings/", {}, format="json")

        assert resp.status_code == 201, resp.content
        assert resp.data["category_id"] is None
        listing = Listing.objects.get(pk=resp.data["id"])
        assert listing.category_id is None
        assert listing.status == ListingStatus.DRAFT

    def test_save_draft_accepts_a_missing_category(self, auth_client, user):
        """Stage A of the analysis writes title/description before anyone has
        chosen a category — the save must not demand one."""
        listing = Listing.objects.create(owner=user)

        resp = auth_client.post(
            f"/listings/listings/{listing.pk}/save-draft/",
            {"title_draft": "Red bicycle", "description_draft": "As on the photo"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert resp.data["category_id"] is None
        listing.refresh_from_db()
        assert listing.title_draft == "Red bicycle"
        assert listing.category_id is None

    def test_the_owners_draft_read_says_null_explicitly(self, auth_client, user):
        listing = Listing.objects.create(owner=user)

        resp = auth_client.get(f"/listings/listings/{listing.pk}/draft/")

        assert resp.status_code == 200, resp.content
        assert "category_id" in resp.data
        assert resp.data["category_id"] is None

    def test_features_may_be_empty(self, auth_client, user):
        """No category means no schema, so there is nothing to fill in yet."""
        listing = Listing.objects.create(owner=user)

        resp = auth_client.get(f"/listings/listings/{listing.pk}/draft/")

        assert resp.status_code == 200, resp.content
        assert not resp.data["features_draft"]
        assert not listing.features

    def test_the_category_arrives_later(self, auth_client, user):
        listing = Listing.objects.create(owner=user)

        resp = auth_client.post(
            f"/listings/listings/{listing.pk}/save-draft/",
            {"category_id": "163"},
            format="json",
        )

        assert resp.status_code == 200, resp.content
        assert resp.data["category_id"] == "163"
        listing.refresh_from_db()
        assert listing.category_id == "163"


class TestPublishStillDemandsOne:

    def test_validate_draft_names_the_category(self, user):
        from stapel_listings.services import publish as publish_service

        listing = Listing.objects.create(owner=user, description_draft="x" * 40)

        result = publish_service.validate_draft(listing)

        assert result.valid is False
        failed = [r for r in result.results if r.slug == "category_id"]
        assert len(failed) == 1
        assert failed[0].localizable_error == ERR_400_PUBLISH_VALIDATION_FAILED

    def test_publish_listing_refuses(self, user):
        """The second door: a caller that skipped validate-draft."""
        from stapel_listings.services import publish as publish_service

        listing = Listing.objects.create(
            owner=user, description_draft="x" * 40, images_draft=["img-1"]
        )

        with pytest.raises(DjangoValidationError):
            publish_service.publish_listing(listing)

        listing.refresh_from_db()
        assert listing.status == ListingStatus.DRAFT

    def test_the_publish_endpoint_refuses(self, auth_client, user, stub_categories):
        listing = Listing.objects.create(
            owner=user, description_draft="x" * 40, images_draft=["img-1"]
        )

        resp = auth_client.post(f"/listings/listings/{listing.pk}/publish/")

        assert resp.status_code == 400, resp.content
        assert ERR_400_PUBLISH_VALIDATION_FAILED in str(resp.data)
        listing.refresh_from_db()
        assert listing.status == ListingStatus.DRAFT


class TestTheGuaranteeSurvivesAsAService:
    """``status != draft`` still means "has a category" — enforced by the
    lifecycle rather than by the column."""

    def test_a_categoryless_row_cannot_leave_the_draft_track(self, user):
        listing = Listing.objects.create(owner=user)

        with pytest.raises(TransitionError):
            listing.transition_to(ListingStatus.PENDING)

        listing.refresh_from_db()
        assert listing.status == ListingStatus.DRAFT

    def test_but_the_seller_can_still_put_it_away(self, user):
        """0.20.0's way forward has to work on a draft that never got a
        category — otherwise the relaxation traps the row it created."""
        listing = Listing.objects.create(owner=user)

        listing.transition_to(ListingStatus.ARCHIVED)

        listing.refresh_from_db()
        assert listing.status == ListingStatus.ARCHIVED

    def test_with_a_category_the_track_reopens(self, user):
        listing = Listing.objects.create(owner=user, category_id="163")

        listing.transition_to(ListingStatus.PENDING)

        listing.refresh_from_db()
        assert listing.status == ListingStatus.PENDING


class TestTheIndexNeverSeesIt:

    def test_no_listing_event_is_emitted(self, auth_client, capture_events):
        published = capture_events("listing.published")
        updated = capture_events("listing.updated")
        submitted = capture_events("listing.submitted")

        resp = auth_client.post(
            "/listings/listings/", {"title_draft": "Red bicycle"}, format="json"
        )
        auth_client.post(
            f"/listings/listings/{resp.data['id']}/save-draft/",
            {"description_draft": "As on the photo", "images_draft": ["img-1"]},
            format="json",
        )

        assert published == []
        assert updated == []
        assert submitted == []

    def test_the_export_document_carries_no_category_and_a_draft_status(self, user):
        """The indexer's predicate is ``status in INDEXED_STATUSES``; what it
        must never get is the string "None" in the category facet."""
        from stapel_listings.services.search_feed import build_search_document

        listing = Listing.objects.create(owner=user, title_draft="Red bicycle")

        doc = build_search_document(listing)

        assert doc["category_id"] == ""
        assert doc["status"] == ListingStatus.DRAFT

    def test_reprojection_skips_it_by_name(self, user):
        """A category-less row is counted and named, never silently walked
        past and never sent to the categories seam as a NULL id."""
        from stapel_listings.services.reproject import reproject_listings

        Listing.objects.create(
            owner=user, features_draft={"mileage": {"type": "int", "value": 1}}
        )

        result = reproject_listings(dry_run=True)

        assert result["skipped_by_reason"]["category_unresolved"] == 1
        assert result["changed"] == 0
