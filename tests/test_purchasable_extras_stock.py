"""Resolver extras (weight_grams / shippable) and inventory-backed stock.

The inventory is a fake comm Function registered by name — exactly what the
real one is to this module, which never imports it.
"""
from decimal import Decimal

import pytest
from django.utils import timezone
from stapel_core.comm import call
from stapel_core.comm.registry import function_registry

from stapel_listings.models import Listing, ListingStatus

pytestmark = pytest.mark.django_db

INVENTORY = "inventory.availability"
CALLS: list[dict] = []
STOCK: dict[str, dict] = {}


def fake_inventory(payload):
    CALLS.append(payload)
    return {"items": {k: STOCK[k] for k in payload["keys"] if k in STOCK}}


def broken_inventory(payload):
    raise RuntimeError("inventory down")


def tracked(available, *, policy="deny", in_stock=None):
    return {"tracked": True, "on_hand": available, "held": 0, "available": available,
            "in_stock": available > 0 if in_stock is None else in_stock, "policy": policy,
            "low_stock": False, "locations": []}


UNTRACKED = {"tracked": False, "on_hand": 0, "held": 0, "available": None, "in_stock": True,
             "policy": "deny", "low_stock": False, "locations": []}


@pytest.fixture(autouse=True)
def _registry():
    CALLS.clear()
    STOCK.clear()
    yield
    function_registry._providers.pop(INVENTORY, None)
    function_registry._schemas.pop(INVENTORY, None)


@pytest.fixture
def inventory():
    function_registry.register(INVENTORY, fake_inventory)
    return STOCK


def feature(slug, value, type_="number"):
    return {"slug": slug, "type": type_, "value": value, "name": slug}


def make(user, **fields):
    defaults = dict(
        owner=user, category_id="7", title="Kettle", price=Decimal("20"), currency="usd",
        status=ListingStatus.PUBLISHED, stock_quantity=4, images=["product/p1"],
        published_at=timezone.now(),
        features=[feature("weight_kg", 1.25), feature("ships", True, "bool"), feature("colour", "red", "text")],
    )
    defaults.update(fields)
    return Listing.objects.create(**defaults)


def resolve(*listings):
    return call("listings.purchasables", {"keys": [str(x.pk) for x in listings]})


# ── extras ───────────────────────────────────────────────────────────


def test_no_mapping_answers_no_extras(user):
    item = resolve(make(user)).popitem()[1]
    assert "weight_grams" not in item and "shippable" not in item


def test_mapping_answers_weight_and_shippable(user, settings):
    settings.STAPEL_LISTINGS = {"PURCHASABLE_ATTRIBUTES": {
        "weight_grams": {"slug": "weight_kg", "multiplier": 1000}, "shippable": "ships",
    }}
    item = resolve(make(user)).popitem()[1]
    assert item["weight_grams"] == 1250 and isinstance(item["weight_grams"], int)
    assert item["shippable"] is True


def test_plain_slug_mapping_is_grams(user, settings):
    settings.STAPEL_LISTINGS = {"PURCHASABLE_ATTRIBUTES": {"weight_grams": "weight_g"}}
    listing = make(user, features=[feature("weight_g", "380")])
    assert resolve(listing)[str(listing.pk)]["weight_grams"] == 380


@pytest.mark.parametrize("value", [None, "heavy", -5, True])
def test_unreadable_weight_is_absent_never_zero(user, settings, value):
    settings.STAPEL_LISTINGS = {"PURCHASABLE_ATTRIBUTES": {"weight_grams": "weight_g"}}
    listing = make(user, features=[feature("weight_g", value)])
    assert "weight_grams" not in resolve(listing)[str(listing.pk)]


def test_hidden_attribute_never_leaves_through_the_resolver(user, settings):
    settings.STAPEL_LISTINGS = {"PURCHASABLE_ATTRIBUTES": {"weight_grams": "weight_g"}}
    listing = make(user, features=[{**feature("weight_g", 500), "visibility": "owner"}])
    assert "weight_grams" not in resolve(listing)[str(listing.pk)]


def test_listing_without_the_attribute_has_no_extra(user, settings):
    settings.STAPEL_LISTINGS = {"PURCHASABLE_ATTRIBUTES": {"weight_grams": "weight_g", "shippable": "ships"}}
    listing = make(user, features=[])
    item = resolve(listing)[str(listing.pk)]
    assert "weight_grams" not in item and "shippable" not in item


def test_extras_are_scalars_orders_snapshots_as_attrs(user, settings):
    settings.STAPEL_LISTINGS = {"PURCHASABLE_ATTRIBUTES": {"weight_grams": "weight_kg", "shippable": "ships"}}
    item = resolve(make(user)).popitem()[1]
    known = {"title", "price", "currency", "available", "max_quantity", "seller_id", "image", "url"}
    extras = {k: v for k, v in item.items() if k not in known}
    assert extras == {"weight_grams": 1, "shippable": True}
    assert all(isinstance(v, (str, int, float, bool)) or v is None for v in extras.values())


# ── stock from the inventory Function ───────────────────────────────


def test_without_inventory_the_listing_counts(user):
    listing = make(user, stock_quantity=3)
    item = resolve(listing)[str(listing.pk)]
    assert item["available"] is True and item["max_quantity"] == 3
    assert CALLS == []


def test_tracked_inventory_answers_available_and_max_quantity(user, inventory):
    a, b = make(user, stock_quantity=50), make(user, stock_quantity=50)
    inventory[str(a.pk)] = tracked(2)
    inventory[str(b.pk)] = tracked(0)
    answer = resolve(a, b)
    assert answer[str(a.pk)]["available"] is True and answer[str(a.pk)]["max_quantity"] == 2
    assert answer[str(b.pk)]["available"] is False and answer[str(b.pk)]["max_quantity"] == 0
    assert len(CALLS) == 1
    assert CALLS[0]["purchasable_type"] == "listing"
    assert set(CALLS[0]["keys"]) == {str(a.pk), str(b.pk)}


def test_backorder_policy_has_no_cap(user, inventory):
    listing = make(user, stock_quantity=0)
    inventory[str(listing.pk)] = tracked(0, policy="backorder", in_stock=True)
    item = resolve(listing)[str(listing.pk)]
    assert item["available"] is True and item["max_quantity"] is None


def test_untracked_key_keeps_the_listing_count(user, inventory):
    listing = make(user, stock_quantity=5)
    inventory[str(listing.pk)] = UNTRACKED
    item = resolve(listing)[str(listing.pk)]
    assert item["available"] is True and item["max_quantity"] == 5


def test_untracked_key_denied_by_inventory_policy(user, inventory):
    listing = make(user, stock_quantity=5)
    inventory[str(listing.pk)] = {**UNTRACKED, "in_stock": False}
    assert resolve(listing)[str(listing.pk)]["available"] is False


def test_unpublished_listing_stays_unavailable_whatever_the_stock(user, inventory):
    listing = make(user, status=ListingStatus.DRAFT)
    inventory[str(listing.pk)] = tracked(9)
    assert resolve(listing)[str(listing.pk)]["available"] is False


def test_unanswered_inventory_falls_back(user):
    function_registry.register(INVENTORY, broken_inventory)
    listing = make(user, stock_quantity=0)
    item = resolve(listing)[str(listing.pk)]
    assert item["available"] is False and item["max_quantity"] == 0


def test_inventory_disabled_by_setting(user, inventory, settings):
    settings.STAPEL_LISTINGS = {"INVENTORY_FUNCTION": ""}
    listing = make(user, stock_quantity=1)
    inventory[str(listing.pk)] = tracked(0)
    assert resolve(listing)[str(listing.pk)]["available"] is True
    assert CALLS == []


def test_inventory_called_in_bounded_batches(user, inventory, settings):
    settings.STAPEL_LISTINGS = {"INVENTORY_BATCH_SIZE": 2}
    rows = [make(user) for _ in range(5)]
    for row in rows:
        inventory[str(row.pk)] = tracked(1)
    resolve(*rows)
    assert [len(c["keys"]) for c in CALLS] == [2, 2, 1]


def test_reading_stock_never_writes_the_listing_count(user, inventory):
    listing = make(user, stock_quantity=7)
    inventory[str(listing.pk)] = tracked(1)
    resolve(listing)
    listing.refresh_from_db()
    assert listing.stock_quantity == 7


# ── the detail's stock indicator ───────────────────────────────────


def test_detail_stock_indicator_from_inventory(user, api_client, inventory):
    listing = make(user, stock_quantity=10)
    inventory[str(listing.pk)] = tracked(3)
    body = api_client.get(f"/listings/listings/{listing.pk}/").json()
    data = body.get("data", body)
    assert data["stock"] == {"in_stock": True, "quantity": 3, "source": "inventory"}
    assert data["stock_quantity"] == 10


def test_detail_stock_indicator_without_inventory(user, api_client):
    listing = make(user, stock_quantity=0)
    body = api_client.get(f"/listings/listings/{listing.pk}/").json()
    data = body.get("data", body)
    assert data["stock"] == {"in_stock": False, "quantity": 0, "source": "listing"}
