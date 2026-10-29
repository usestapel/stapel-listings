"""``listings.purchasables`` — listings answering the cart's resolver contract."""
import json
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest
from django.utils import timezone
from stapel_core.comm import call

from stapel_listings.models import Listing, ListingStatus

pytestmark = pytest.mark.django_db

#: The item shape a cart module validates every resolver reply against,
#: held here as data so this suite fails if listings drifts from it.
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "price": {"type": "string", "pattern": r"^[0-9]{1,10}(\.[0-9]{1,2})?$"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "available": {"type": "boolean"},
        "max_quantity": {"type": ["integer", "null"], "minimum": 0},
        "seller_id": {"type": ["string", "null"]},
        "image": {"type": ["string", "null"]},
        "url": {"type": ["string", "null"]},
    },
    "required": ["title", "price", "currency", "available"],
}


def make(user, **fields):
    defaults = dict(
        owner=user, category_id="7", title="Brake pad", price=Decimal("12.5"),
        currency="usd", status=ListingStatus.PUBLISHED, stock_quantity=4,
        images=["product/p1", "product/p2"], published_at=timezone.now(),
    )
    defaults.update(fields)
    return Listing.objects.create(**defaults)


def resolve(*listings, keys=None):
    return call("listings.purchasables", {"keys": keys or [str(x.pk) for x in listings]})


def test_published_countable_listing(user):
    listing = make(user)
    item = resolve(listing)[str(listing.pk)]
    assert item == {
        "title": "Brake pad", "price": "12.50", "currency": "USD", "available": True,
        "max_quantity": 4, "seller_id": str(user.pk), "image": "product/p1", "url": None,
    }
    jsonschema.validate(item, ITEM_SCHEMA)


def test_out_of_stock_is_unavailable(user):
    listing = make(user, stock_quantity=0)
    item = resolve(listing)[str(listing.pk)]
    assert item["available"] is False
    assert item["max_quantity"] == 0


def test_service_listing_has_no_quantity_cap(user):
    listing = make(user, countable=False, stock_quantity=None)
    item = resolve(listing)[str(listing.pk)]
    assert item["available"] is True
    assert item["max_quantity"] is None


@pytest.mark.parametrize("status", [ListingStatus.DRAFT, ListingStatus.ARCHIVED])
def test_non_public_listing_is_unavailable(user, status):
    listing = make(user, status=status)
    assert resolve(listing)[str(listing.pk)]["available"] is False


def test_unknown_deleted_unpriced_and_junk_keys_are_absent(user):
    gone = make(user, deleted_at=timezone.now())
    unpriced = make(user, price=None)
    assert resolve(keys=[str(gone.pk), str(unpriced.pk), "999999", "abc"]) == {}


def test_url_template(user, settings):
    settings.STAPEL_LISTINGS = {"LISTING_URL_TEMPLATE": "/l/{listing_id}"}
    listing = make(user, images=[])
    item = resolve(listing)[str(listing.pk)]
    assert item["url"] == f"/l/{listing.pk}"
    assert item["image"] is None


def test_batch_answers_each_key(user, other_user):
    a, b = make(user), make(other_user, price=Decimal("3"))
    answer = resolve(a, b)
    assert set(answer) == {str(a.pk), str(b.pk)}
    assert answer[str(b.pk)]["price"] == "3.00"
    assert answer[str(b.pk)]["seller_id"] == str(other_user.pk)


def test_input_schema_is_shipped():
    path = Path(__file__).resolve().parent.parent / "schemas" / "functions" / "listings.purchasables.json"
    schema = json.loads(path.read_text())
    assert schema["required"] == ["keys"]
