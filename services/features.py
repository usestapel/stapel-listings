"""Build the four attribute JSON projections stored on a Listing.

Ported from legacy-catalog ``ads/services/features_builder.py``, generalized:

- inputs are the DAO dict produced by ``stapel_attributes.normalize_to_dao``
  and the category's feature configs (fetched over comm), not ORM Feature rows;
- ``build_features_search`` is type-generic — the legacy-specific ``size_grid``
  table mapping is gone (that type lives in an app-layer vertical, not here);
  unknown types fall back to extracting their scalar/list ``value``.

Projections:
- ``features``        — ordered DAO list, empty headers filtered;
- ``features_title``  — DAOs flagged ``title``;
- ``features_badges`` — DAOs flagged ``badge``;
- ``features_search`` — ``{slug: [values]}`` for a future stapel-search indexer.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from stapel_attributes import coerce_feature_defs, get_feature_slug, parse_config


def build_features_list(
    features_dao_dict: Dict[str, Dict[str, Any]],
    consecutive_header_pairs: Set[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """Ordered DAO list with the slug injected and empty headers filtered."""
    features_list = [
        {**dao, "slug": slug} for slug, dao in features_dao_dict.items()
    ]
    features_list.sort(key=lambda x: x.get("order", 0))
    return filter_empty_headers(features_list, consecutive_header_pairs)


def filter_empty_headers(
    features_list: List[Dict[str, Any]],
    consecutive_header_pairs: Set[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    """Drop header DAOs with no feature following them.

    Exception: two headers consecutive in the *category* order are kept
    (intentional grouping), tracked by *consecutive_header_pairs*.
    """
    if not features_list:
        return []

    result: List[Dict[str, Any]] = []
    for i in reversed(range(len(features_list))):
        dao = features_list[i]
        if dao.get("type") != "header":
            result.insert(0, dao)
            continue
        if not result:
            continue
        next_item = result[0]
        if next_item.get("type") != "header":
            result.insert(0, dao)
        else:
            pair = (dao.get("slug"), next_item.get("slug"))
            if pair in consecutive_header_pairs:
                result.insert(0, dao)
    return result


def get_consecutive_header_pairs(configs) -> Set[Tuple[str, str]]:
    """Set of (slug1, slug2) header pairs adjacent in the category order."""
    consecutive: Set[Tuple[str, str]] = set()
    prev_header_slug = None
    for feature in coerce_feature_defs(configs):
        slug = get_feature_slug(feature)
        try:
            is_header = parse_config(feature.config).type == "header"
        except Exception:
            is_header = False
        if is_header:
            if prev_header_slug is not None:
                consecutive.add((prev_header_slug, slug))
            prev_header_slug = slug
        else:
            prev_header_slug = None
    return consecutive


def build_features_title(features_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dao for dao in features_list if dao.get("title") is True]


def build_features_badges(features_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dao for dao in features_list if dao.get("badge") is True]


def build_features_search(
    features_dao_dict: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Any]]:
    """``{slug: [searchable values]}`` from the DAO dict (headers excluded)."""
    search: Dict[str, List[Any]] = {}
    for slug, dao in features_dao_dict.items():
        if dao.get("type") == "header":
            continue
        values = _extract_search_values(dao)
        if values:
            search[slug] = values
    return search


# Types whose ``value`` is already a list (path / multi-select).
_LIST_VALUE_TYPES = frozenset({"select", "hierarchical_select"})
# Scalar types kept as-is (numbers stay numbers, strings stay strings).
_SCALAR_VALUE_TYPES = frozenset({"int", "float", "string", "bool", "date"})


def _extract_search_values(dao: Dict[str, Any]) -> List[Any]:
    """Type-generic search-value extraction from a single DAO."""
    feat_type = dao.get("type")

    if feat_type in _SCALAR_VALUE_TYPES:
        value = dao.get("value")
        # Keep falsy-but-valid values (0, False) — only drop None / "".
        if value is None or value == "":
            return []
        return [value]

    if feat_type in _LIST_VALUE_TYPES:
        value = dao.get("value", [])
        return list(value) if isinstance(value, list) else [value]

    if feat_type == "hex_color":
        return [
            v for v in (dao.get("simple"), dao.get("hex"), dao.get("label")) if v
        ]

    # Unknown / custom type: fall back to its value (scalar or list).
    value = dao.get("value")
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, list) else [value]
