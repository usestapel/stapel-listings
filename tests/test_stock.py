"""The ``countable`` / ``stock_quantity`` invariant.

``countable=True`` (a physical good — the default) requires a non-negative
``stock_quantity``; ``countable=False`` (a service, where "how many" doesn't
apply) requires it to be ``NULL``. Enforced three times, tested at each layer:

- ``Listing.clean()`` / ``validate_countable_stock()`` — the Python-level
  source of truth (admin, ``full_clean()`` callers);
- the DB ``listing_stock_invariant_chk`` CheckConstraint — the storage-level
  backstop for writes that skip ``clean()`` (bulk ops, raw SQL);
- ``ListingDraftSerializer.validate()`` — the API-facing 400.
"""
import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from stapel_listings.models import Listing, validate_countable_stock

pytestmark = pytest.mark.django_db


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


# --- defaults --------------------------------------------------------------


def test_default_is_countable_with_zero_stock(user):
    listing = Listing.objects.create(owner=user, category_id="7")
    assert listing.countable is True
    assert listing.stock_quantity == 0


# --- validate_countable_stock() / Listing.clean() ---------------------------


def test_countable_requires_stock_quantity():
    with pytest.raises(ValidationError):
        validate_countable_stock(True, None)


def test_countable_rejects_negative_stock():
    with pytest.raises(ValidationError):
        validate_countable_stock(True, -1)


def test_countable_true_with_nonnegative_stock_is_valid():
    validate_countable_stock(True, 0)
    validate_countable_stock(True, 42)


def test_uncountable_rejects_stock_quantity():
    with pytest.raises(ValidationError):
        validate_countable_stock(False, 0)


def test_uncountable_with_null_stock_is_valid():
    validate_countable_stock(False, None)


def test_clean_enforces_invariant(user):
    listing = Listing(owner=user, category_id="7", countable=True, stock_quantity=None)
    with pytest.raises(ValidationError):
        listing.clean()

    listing = Listing(owner=user, category_id="7", countable=False, stock_quantity=1)
    with pytest.raises(ValidationError):
        listing.clean()

    listing = Listing(owner=user, category_id="7", countable=False, stock_quantity=None)
    listing.clean()  # no raise


# --- DB CheckConstraint backstop --------------------------------------------


def test_db_constraint_rejects_countable_without_stock(user):
    listing = Listing(owner=user, category_id="7", countable=True, stock_quantity=None)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            listing.save()


def test_db_constraint_rejects_uncountable_with_stock(user):
    listing = Listing(owner=user, category_id="7", countable=False, stock_quantity=3)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            listing.save()


def test_db_constraint_allows_service_listing(user):
    listing = Listing.objects.create(
        owner=user, category_id="7", countable=False, stock_quantity=None
    )
    assert listing.pk is not None


# --- API surface (ListingDraftSerializer) -----------------------------------


def test_api_create_defaults_countable_stock(auth_client, user):
    resp = auth_client.post("/listings/listings/", {"category_id": "7"}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.data["countable"] is True
    assert resp.data["stock_quantity"] == 0


def test_api_create_service_listing(auth_client, user):
    resp = auth_client.post(
        "/listings/listings/",
        {"category_id": "7", "countable": False, "stock_quantity": None},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.data["countable"] is False
    assert resp.data["stock_quantity"] is None


def test_api_create_service_listing_without_explicit_null_rejected(auth_client, user):
    # countable=False alone isn't enough: the model default for a brand-new
    # stock_quantity is 0, and 0 conflicts with "uncountable must be NULL" —
    # the request must explicitly clear stock_quantity in the same call.
    resp = auth_client.post(
        "/listings/listings/", {"category_id": "7", "countable": False}, format="json"
    )
    assert resp.status_code == 400, resp.content


def test_api_save_draft_sets_stock_quantity(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"stock_quantity": 10},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.stock_quantity == 10


def test_api_save_draft_rejects_negative_stock_quantity(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"stock_quantity": -1},
        format="json",
    )
    assert resp.status_code == 400


def test_api_save_draft_switch_to_service_requires_explicit_null(auth_client, user):
    listing = Listing.objects.create(owner=user, category_id="7")  # countable=True, stock=0

    # Sending only countable=False leaves stock_quantity=0 (untouched by this
    # partial request) which conflicts with the invariant.
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"countable": False},
        format="json",
    )
    assert resp.status_code == 400

    # Sending both together switches cleanly.
    resp = auth_client.post(
        f"/listings/listings/{listing.pk}/save-draft/",
        {"countable": False, "stock_quantity": None},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.countable is False
    assert listing.stock_quantity is None


def test_api_listing_detail_exposes_stock_fields(api_client, user):
    listing = Listing.objects.create(owner=user, category_id="7", stock_quantity=5)
    resp = api_client.get(f"/listings/listings/{listing.pk}/")
    assert resp.status_code == 200
    assert resp.data["countable"] is True
    assert resp.data["stock_quantity"] == 5


# --- events: stock is deliberately NOT part of the listing.* payloads ------


def test_published_payload_excludes_stock_fields(user, capture_events):
    """listing.* index events carry identity/status/search projection only —
    same minimal shape as before this change (no price, title, images either)
    — so stock_quantity/countable are intentionally not added here. A future
    consumer that needs stock reads it via the listings.status Function or a
    dedicated Function, not the index event."""
    from stapel_listings.models import ListingStatus

    published = capture_events("listing.published")
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING, stock_quantity=7
    )
    listing.transition_to(ListingStatus.PUBLISHED)
    assert "stock_quantity" not in published[0].payload
    assert "countable" not in published[0].payload
