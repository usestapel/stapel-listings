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


def _pointer_key(category_id: Any) -> str:
    """Key holding the latest known revision for a category (the pointer)."""
    return f"{_CACHE_PREFIX}rev:{category_id}"


def _data_key(category_id: Any, revision: Any) -> str:
    """Key holding the feature configs for one specific revision."""
    return f"{_CACHE_PREFIX}data:{category_id}:{revision}"


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

    Cache design (M-6): configs are stored under a *revision-versioned* key,
    with a separate pointer key naming the current revision. This closes the
    read-then-set race the old single-key ``get -> call -> set`` had: a
    ``category.changed`` arriving mid-fetch only advances the pointer, so a
    concurrent fetch can never re-cache a stale schema *under the live key* —
    its result lands under its own (older) revision and is simply never read
    again. ``categories.features`` returns the revision the features belong to,
    so the pair is always internally consistent (paired with the M-6 snapshot
    read on the categories side).
    """
    from stapel_core.comm import call

    if use_cache:
        revision = cache.get(_pointer_key(category_id))
        if revision is not None:
            cached = cache.get(_data_key(category_id, revision))
            if cached is not None:
                return cached

    result = call(
        listings_settings.CATEGORY_FEATURES_FUNCTION,
        {"category_id": _coerce_category_id(category_id)},
    )
    features = result.get("features", []) if isinstance(result, dict) else []
    revision = result.get("revision") if isinstance(result, dict) else None

    if use_cache and revision is not None:
        ttl = listings_settings.FEATURE_CONFIG_CACHE_TIMEOUT
        cache.set(_data_key(category_id, revision), features, ttl)
        _advance_pointer(category_id, revision, ttl)
    return features


def _advance_pointer(category_id: Any, revision: Any, ttl: Any) -> None:
    """Move the pointer forward to *revision*; never downgrade it.

    A ``category.changed`` may have already advanced the pointer past the
    revision a slow in-flight fetch is returning — keep the newer pointer so
    the stale fetch's result is never promoted to "current".
    """
    current = cache.get(_pointer_key(category_id))
    if current is None or revision >= current:
        cache.set(_pointer_key(category_id), revision, ttl)


def note_changed(category_id: Any, revision: Any) -> None:
    """React to a ``category.changed`` event by advancing the pointer.

    With a known revision this just moves the pointer forward (idempotent, safe
    under at-least-once delivery). Without one, fall back to a hard reset so the
    next read refetches.
    """
    if revision is None:
        invalidate(category_id)
        return
    _advance_pointer(
        category_id, revision, listings_settings.FEATURE_CONFIG_CACHE_TIMEOUT
    )


def invalidate(category_id: Any) -> None:
    """Hard reset: drop the revision pointer so the next read refetches.

    (Versioned data entries are left to expire on their own — they are inert
    once no pointer names them.)
    """
    cache.delete(_pointer_key(category_id))
