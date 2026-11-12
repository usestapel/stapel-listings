"""Purchasable extras and stock, for the resolver and the detail read.

Two seams, both answered from settings with no import of any sibling:

- **Extras** (``PURCHASABLE_ATTRIBUTES``): which typed attribute of a listing
  means ``weight_grams`` (integer grams) and which means ``shippable`` (bool).
  The values ride the purchasable resolver answer as extra fields, which a
  cart passes on as ``lines[].attrs`` — that is how a delivery module prices
  by weight with no glue code. An empty mapping answers neither field.
- **Stock** (``INVENTORY_FUNCTION``): when that comm Function is reachable in
  this process (``function_unreachable_reason`` — presence, never an import),
  ``available`` / ``max_quantity`` and the detail's ``stock`` come from it, in
  bounded batches. Absent, unanswered or malformed: the listing's own
  ``countable`` / ``stock_quantity`` answer, exactly as before.

When the inventory answers, the inventory owns the count: listings never
decrements ``stock_quantity`` itself (no ``orders.order.placed`` reaction
here), so the two cannot both take a sale off.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from ..conf import listings_settings

logger = logging.getLogger(__name__)

EXTRA_FIELDS = ("weight_grams", "shippable")

#: Who answered a stock indicator.
STOCK_SOURCES = ("inventory", "listing")


def _attribute_rule(field: str) -> tuple[str, Decimal] | None:
    """``(slug, multiplier)`` for an extra field, or None when unmapped."""
    raw = (listings_settings.PURCHASABLE_ATTRIBUTES or {}).get(field)
    if isinstance(raw, str) and raw:
        return raw, Decimal(1)
    if isinstance(raw, dict) and isinstance(raw.get("slug"), str) and raw["slug"]:
        try:
            return raw["slug"], Decimal(str(raw.get("multiplier", 1)))
        except InvalidOperation:
            return None
    return None


def _values_by_slug(listing) -> dict:
    """Published, PUBLIC attribute values by slug.

    A value the catalogue stamped ``owner`` / ``staff`` never leaves through
    the resolver: a cart snapshots these fields onto lines a buyer reads.
    """
    from stapel_attributes import public_daos

    out = {}
    for dao in public_daos([d for d in listing.features or [] if isinstance(d, dict)]):
        if dao.get("slug"):
            out.setdefault(str(dao["slug"]), dao.get("value"))
    return out


def _weight(value, multiplier: Decimal) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        grams = (Decimal(str(value)) * multiplier).to_integral_value()
    except (InvalidOperation, ValueError):
        return None
    return int(grams) if grams >= 0 else None


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def purchasable_extras(listing) -> dict:
    """``{"weight_grams"?: int, "shippable"?: bool}`` from the published attributes.

    A field is present only when it is mapped and the listing's published
    value converts cleanly; a missing or unreadable value is absent, never a
    guess (a weight of 0 would ship a piano for free).
    """
    rules = {field: _attribute_rule(field) for field in EXTRA_FIELDS}
    if not any(rules.values()):
        return {}
    values = _values_by_slug(listing)
    extras = {}
    weight_rule = rules["weight_grams"]
    if weight_rule and weight_rule[0] in values:
        grams = _weight(values[weight_rule[0]], weight_rule[1])
        if grams is not None:
            extras["weight_grams"] = grams
    ship_rule = rules["shippable"]
    if ship_rule and ship_rule[0] in values:
        flag = _flag(values[ship_rule[0]])
        if flag is not None:
            extras["shippable"] = flag
    return extras


def inventory_function() -> str | None:
    """The inventory Function name when it is reachable here, else None."""
    from stapel_core.comm import function_unreachable_reason

    name = str(listings_settings.INVENTORY_FUNCTION or "")
    if not name or function_unreachable_reason(name):
        return None
    return name


def inventory_items(keys: list[str]) -> dict | None:
    """``{key: item}`` from the inventory, or None to fall back.

    Batched by ``INVENTORY_BATCH_SIZE``. Any failure (unreachable, raised,
    timed out, malformed) answers None for the whole call — a half-known
    stock picture mixed with the listing's own counts would be worse than
    either.
    """
    name = inventory_function()
    if name is None or not keys:
        return None
    from stapel_core.comm import call

    size = max(1, int(listings_settings.INVENTORY_BATCH_SIZE))
    timeout = float(listings_settings.INVENTORY_TIMEOUT)
    ptype = str(listings_settings.INVENTORY_PURCHASABLE_TYPE)
    items: dict = {}
    for start in range(0, len(keys), size):
        batch = keys[start:start + size]
        try:
            answer = call(name, {"purchasable_type": ptype, "keys": batch}, timeout=timeout)
        except Exception as exc:
            logger.warning("listings: %s did not answer; using listing stock: %s", name, exc)
            return None
        got = answer.get("items") if isinstance(answer, dict) else None
        if not isinstance(got, dict):
            logger.warning("listings: %s answered without items; using listing stock", name)
            return None
        items.update({str(k): v for k, v in got.items() if isinstance(v, dict)})
    return items


def own_stock(listing) -> tuple[bool, int | None]:
    """``(in_stock, quantity)`` from the listing's own columns (no inventory)."""
    if not listing.countable:
        return True, None
    stock = listing.stock_quantity or 0
    return stock > 0, stock


def stock_from_item(listing, item: dict | None) -> tuple[bool, int | None, str]:
    """``(in_stock, max_quantity, source)`` for one listing.

    A tracked item is the inventory's word: ``in_stock`` as answered, the
    quantity capped at ``available`` under the ``deny`` policy and uncapped
    under ``backorder``. An untracked key keeps the listing's own count and
    is in stock only if the inventory also says so (its ``TRACK_UNKNOWN``
    policy). No item at all is the listing's own answer.
    """
    in_stock, quantity = own_stock(listing)
    if not isinstance(item, dict):
        return in_stock, quantity, "listing"
    answered = bool(item.get("in_stock"))
    if item.get("tracked"):
        available = item.get("available")
        if item.get("policy") == "deny" and isinstance(available, int):
            return answered, max(0, available), "inventory"
        return answered, None, "inventory"
    return in_stock and answered, quantity, "listing"


def stock_for(listings) -> dict:
    """``{pk: (in_stock, max_quantity, source)}`` for a batch of listings."""
    listings = list(listings)
    items = inventory_items([str(x.pk) for x in listings])
    if items is None:
        return {x.pk: (*own_stock(x), "listing") for x in listings}
    return {x.pk: stock_from_item(x, items.get(str(x.pk))) for x in listings}


__all__ = [
    "EXTRA_FIELDS",
    "STOCK_SOURCES",
    "inventory_function",
    "inventory_items",
    "own_stock",
    "purchasable_extras",
    "stock_for",
    "stock_from_item",
]
