"""Resolve a category's feature schema over comm — without importing categories.

listings validates listing attribute values against the *category's* feature
configs. It obtains those configs through the ``categories.features`` comm
Function (name is the ``CATEGORY_FEATURES_FUNCTION`` seam), never by importing
stapel-categories. The result is cacheable by the category's ``revision``; the
``category.changed`` subscription (see ``actions.py``) drops the cache entry so
a schema change is picked up promptly.
"""
from __future__ import annotations

from typing import Any, List

from django.core.cache import cache

from ..conf import listings_settings

_CACHE_PREFIX = "stapel_listings:catconf:"


def _cache_key(category_id: Any) -> str:
    return f"{_CACHE_PREFIX}{category_id}"


def _coerce_category_id(category_id: Any) -> Any:
    """Pass an all-digit opaque id to the function as an int (the
    categories.features schema types it as integer) while leaving genuine
    string/UUID ids untouched."""
    s = str(category_id)
    return int(s) if s.isdigit() else s


def get_feature_configs(category_id: Any, *, use_cache: bool = True) -> List[dict]:
    """Return the list of feature-definition dicts for *category_id*.

    Each dict is consumable by ``stapel_attributes.coerce_feature_defs``
    (``{id, slug, name, mandatory, config}``). Raises whatever the comm layer
    raises (``FunctionNotRegistered`` when no categories provider is wired,
    ``LookupError`` for an unknown category) — callers decide fatality.
    """
    from stapel_core.comm import call

    key = _cache_key(category_id)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    result = call(
        listings_settings.CATEGORY_FEATURES_FUNCTION,
        {"category_id": _coerce_category_id(category_id)},
    )
    features = result.get("features", []) if isinstance(result, dict) else []

    if use_cache:
        cache.set(key, features, listings_settings.FEATURE_CONFIG_CACHE_TIMEOUT)
    return features


def invalidate(category_id: Any) -> None:
    """Drop the cached feature configs for *category_id* (idempotent)."""
    cache.delete(_cache_key(category_id))
