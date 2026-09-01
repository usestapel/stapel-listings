"""Build the four attribute JSON projections stored on a Listing.

Ported from the legacy catalog's ``ads/services/features_builder.py``, generalized:

- inputs are the DAO dict produced by ``stapel_attributes.normalize_to_dao``
  and the category's feature configs (fetched over comm), not ORM Feature rows;
- ``build_features_search`` is type-generic — the legacy catalog's ``size_grid``
  table mapping is gone (that type lives in an app-layer vertical, not here);
  unknown types fall back to extracting their scalar/list ``value``.

Projections:
- ``features``        — ordered DAO list, empty headers filtered;
- ``features_title``  — DAOs flagged ``title``;
- ``features_badges`` — DAOs flagged ``badge``;
- ``features_search`` — ``{slug: [values]}`` for a future stapel-search indexer.

Title and badge are the DAO itself, not a rendered string, which is what makes
a vocabulary-backed value work without a second lookup: a ``ref_select`` DAO
carries ``labels`` (the display snapshot taken at write time) alongside
``value`` (the term codes), and the reader picks the half it needs. Search
takes ``value``, always — see ``_LIST_VALUE_TYPES``.

**Three of the four projections are public artefacts; ``features`` is not.**
A feature the catalogue marked ``visibility: owner`` or ``staff`` — a VIN, an
IMEI, a serial number — carries that stamp on its own DAO (stapel-attributes
0.8.0), and this module keeps such a value out of ``features_title``,
``features_badges`` and ``features_search`` at BUILD time, permanently. Those
three columns are read raw by a card, by the search-document builder and by the
``listing.published``/``listing.updated`` bus payloads, none of which has a
viewer or a schema in hand; the only way they can be safe for every reader is
for the value never to enter them.

``features`` keeps everything, because the owner's own view and moderation both
need the value. It is redacted per viewer on the way out, in
``serializers.FeatureVisibilityMixin`` — the one read path that knows who is
asking.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from stapel_attributes import (
    coerce_feature_defs,
    get_feature_slug,
    is_public,
    normalize_to_dao,
    parse_config,
)

# The stored columns that are *entirely* derived from (category schema,
# features_draft). Named once so every writer of the projections — publish and
# the re-projection command — moves exactly this set and nothing else.
PROJECTION_FIELDS: Tuple[str, ...] = (
    "features",
    "features_title",
    "features_badges",
    "features_search",
)


def build_projections(
    configs, features_draft: Dict[str, Any] | None
) -> Dict[str, Any]:
    """The four projections, as ``{field name: value}``.

    THE definition of "what the projections are". ``publish_listing`` calls it
    to promote a validated draft; ``services.reproject`` calls it to refresh a
    snapshot taken by an older writer. Two call sites, one derivation — a
    second hand-rolled copy would be a projection that goes stale in one place
    and not the other, which is the class of bug this whole module already
    fights in :func:`build_features_search_from_list`.

    Does NOT validate: *features_draft* is expected to have passed
    ``validate_dto`` against *configs* already. Callers own that step because
    they disagree about what an invalid draft means (publish raises, the
    re-projection command counts and skips).
    """
    features_dao_dict = (
        normalize_to_dao(configs, features_draft) if features_draft else {}
    )
    features_list = build_features_list(
        features_dao_dict, get_consecutive_header_pairs(configs)
    )
    return {
        "features": features_list,
        "features_title": build_features_title(features_list),
        "features_badges": build_features_badges(features_list),
        "features_search": build_features_search(features_dao_dict),
    }


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
    # ``is_public`` is belt to the engine's braces: a non-public FeatureDef
    # already refuses to stamp ``title``/``badge`` onto its DAO. It is repeated
    # here because this list is read raw by every card in the fleet and by the
    # search document's free-text arm, and because a row projected by an older
    # writer (before the visibility axis) can still carry ``title: true`` until
    # ``listings_reproject_features`` has run over it.
    return [dao for dao in features_list if dao.get("title") is True and is_public(dao)]


def build_features_badges(features_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dao for dao in features_list if dao.get("badge") is True and is_public(dao)]


def build_features_search_from_list(
    features_list: List[Dict[str, Any]] | None,
) -> Dict[str, List[Any]]:
    """``{slug: [searchable values]}`` from the stored ``features`` DAO list.

    The same projection as :func:`build_features_search`, keyed off the
    published ``features`` list instead of the transient publish-time DAO
    dict — so ``features_search`` can be re-derived at any moment (a republish
    from PAUSED, an edit of a live listing) without re-running validation or
    re-fetching the category schema. ``features_search`` is a *derived* value;
    this is the derivation, and it has exactly one definition.
    """
    return build_features_search(
        {
            str(dao.get("slug")): dao
            for dao in (features_list or [])
            if isinstance(dao, dict) and dao.get("slug")
        }
    )


def build_features_search(
    features_dao_dict: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Any]]:
    """``{slug: [searchable values]}`` from the DAO dict (headers excluded).

    A non-public value never enters this column. That is not a display choice —
    an indexed identifier is an *oracle*: `?f.vin=<value>` either returns the
    listing or does not, which confirms to a stranger that this exact car is
    that exact VIN. The value has to be absent from the index, not merely
    absent from the facet panel.
    """
    search: Dict[str, List[Any]] = {}
    for slug, dao in features_dao_dict.items():
        if dao.get("type") == "header":
            continue
        if not is_public(dao):
            continue
        values = _extract_search_values(dao)
        if values:
            search[slug] = values
    return search


# Types whose ``value`` is already a list (path / multi-select). The two
# vocabulary-backed types belong here and not in the unknown-type fallback:
# their ``value`` is the term CODES, which is the filter axis, while the
# ``labels`` they also carry are a display snapshot that changes with the
# vocabulary's language and must never reach the index.
_LIST_VALUE_TYPES = frozenset(
    {"select", "hierarchical_select", "ref_select", "ref_hierarchical_select"}
)
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
