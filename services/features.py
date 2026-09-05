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

The two card projections additionally carry the **card badge contract** on the
way out (:func:`decorate_card_elements`) — the per-element ``label`` / ``unit``
/ ``name`` / ``presentation`` a card needs to draw an unambiguous line without
a category schema in hand.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

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


def build_projections_from_list(
    features_list: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The four projections from an already-assembled DAO list.

    The same four derivations as :func:`build_projections`, entered one step
    later — for the caller that has a DAO list rather than a draft. Split out
    so "what the projections are" still has ONE definition: this function and
    the tail of ``build_projections`` must not be two lists that drift.
    """
    return {
        "features": features_list,
        "features_title": build_features_title(features_list),
        "features_badges": build_features_badges(features_list),
        "features_search": build_features_search_from_list(features_list),
    }


def build_projections_partial(
    configs,
    features_draft: Dict[str, Any] | None,
    *,
    skip_slugs: Set[str],
    stored_features: List[Dict[str, Any]] | None,
) -> Dict[str, Any]:
    """Projections for every field EXCEPT *skip_slugs*, keeping their old DAOs.

    The repair path. One field of a draft that no longer validates used to
    cost the listing its whole re-projection: every other field stayed as some
    older writer left it, printing storage slugs at people, because one
    unrelated attribute had drifted out of its category's bounds.

    So the invalid fields are set aside and everything else is re-derived
    normally. Their **stored** DAOs are then merged back in rather than
    dropped — dropping them would delete an attribute from the card, which
    turns a stale value into a missing one and makes the repair a regression
    for the field it could not fix. A preserved DAO is stale, and it was
    already stale before this ran; nothing about it gets worse.

    The merged list is re-sorted by ``order`` so a preserved field keeps its
    place in the table, and the other three projections are derived from the
    merged list — not from the fresh half — so ``features_title``,
    ``features_badges`` and ``features_search`` stay consistent with
    ``features`` rather than quietly disagreeing about which fields exist.
    """
    kept_draft = {
        key: value
        for key, value in (features_draft or {}).items()
        if str(key) not in skip_slugs
    }
    fresh = build_projections(configs, kept_draft)
    preserved = [
        dao
        for dao in (stored_features or [])
        if isinstance(dao, dict) and str(dao.get("slug")) in skip_slugs
    ]
    if not preserved:
        return fresh
    merged = sorted(
        list(fresh["features"]) + preserved,
        key=lambda dao: dao.get("order", 0),
    )
    return build_projections_from_list(merged)


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


# --- The card badge contract ---------------------------------------------
#
# A card draws ``features_title`` / ``features_badges`` as one short summary
# line. Until 0.21.3 an element carried the DAO and nothing else, so the only
# obvious thing to print was its value — and a live apartment card read
# «Кирпичный · 3 · 9»: three answers with the questions missing. Nothing on the
# element said that 3 was a floor, that 9 was the building's height, or that
# neither was a count of anything.
#
# So every element of the two card projections now also carries, on the way
# out, what a card needs to be unambiguous WITHOUT fetching a category schema:
#
#   ``value``        unchanged — the stored value (term CODES for a select);
#   ``label``        the caption for that value: the write-time label snapshot
#                    for a select, the number for a number, the true caption
#                    for a boolean. Translation key or literal, exactly as the
#                    catalogue wrote it — this module never translates;
#   ``unit``         the feature's unit when it has one («м²», «эт.»);
#   ``name``         the feature's own caption («Этаж»);
#   ``presentation`` which of four shapes to render.
#
# ONE rule decides ``presentation``, server-side, so every client draws the
# same line:
#
#   PRESENTATION_VALUE       «Кирпичный»  caption is not a number
#   PRESENTATION_VALUE_UNIT  «42 м²»      caption is a number, unit known
#   PRESENTATION_NAME_VALUE  «Этаж 3»     caption is a number, no unit
#   PRESENTATION_NAME        «Балкон»     a true boolean — the name IS the fact
#
# A false boolean is dropped from the line: «Балкон: нет» is noise on a card,
# and it is the one element the contract removes rather than annotates.
#
# "Is a number" is decided on the CAPTION, not on the stored type, and that is
# the whole trick. On a real catalogue ``floor`` and ``floors`` are
# vocabulary-backed — stored ``ref_select`` with ``labels: ["3"]`` — so a
# type-driven rule would file them under "dictionary value, print it alone"
# and reproduce the exact bug this contract exists to fix. A caption of several
# joined labels is never numeric, whatever its parts look like.
#
# 0.22.1 (Д421) refines the numeric caption itself, in two ways:
#
# - it is grouped per locale (RU: a non-breaking space every three digits,
#   from five digits up — ``20000`` -> «20 000». Below that a number prints
#   bare, which is what keeps a year («2019») from being split into «2 019»:
#   see ``GROUPING_THRESHOLD``. Grouping only ever touches a caption built
#   from the feature's raw stored value; a vocabulary label (``floor``,
#   ``floors`` above) is exactly what the catalogue wrote and stays untouched,
#   same as ``postfix1000`` already leaves it alone;
# - for ``PRESENTATION_NAME_VALUE`` specifically, ``name`` gets a trailing
#   colon UNLESS the caption is that same vocabulary label. «Этаж 3» reads as
#   a name-then-count because «Этаж» is a term; «Модель 90» does not, because
#   «90» is the feature's raw value with no term behind it, and the two used
#   to read as one glued phrase («HONOR · Модель 90», PASS-16 Д421). The
#   colon lands in ``name`` rather than in a new key so a client already
#   joining ``name`` and ``label`` with one space — the 0.21.3 contract's own
#   reference renderer — reads «Модель: 90» with no client-side change.

#: Render the caption alone.
PRESENTATION_VALUE = "value"
#: Render the caption followed by the unit.
PRESENTATION_VALUE_UNIT = "value_unit"
#: Render the feature name followed by the caption.
PRESENTATION_NAME_VALUE = "name_value"
#: Render the feature name alone.
PRESENTATION_NAME = "name"

#: Every presentation this module emits — a client that branches on the field
#: has this as its closed set, and an unknown value means it is older than the
#: server.
PRESENTATIONS: Tuple[str, ...] = (
    PRESENTATION_VALUE,
    PRESENTATION_VALUE_UNIT,
    PRESENTATION_NAME_VALUE,
    PRESENTATION_NAME,
)

#: Keys the contract ADDS to a stored DAO. Nothing is renamed and nothing is
#: dropped, so a client written against the pre-0.21.3 shape keeps working.
CARD_ELEMENT_KEYS: Tuple[str, ...] = ("label", "unit", "name", "presentation")


def decorate_card_elements(
    daos: List[Dict[str, Any]] | None,
) -> List[Dict[str, Any]]:
    """A card projection with the card badge contract on every element.

    Applied on the way OUT rather than at build time on purpose: the contract
    is derived wholly from the stored DAO, so deriving it at the wire edge
    fixes every listing already in the database at once — no re-projection
    pass, no migration, and no fourth copy of the projection to keep in step.
    Elements that render to nothing (a header, a redacted stub, a blank value,
    a false boolean) are dropped.
    """
    out: List[Dict[str, Any]] = []
    for dao in daos or []:
        if not isinstance(dao, dict):
            continue
        element = decorate_card_element(dao)
        if element is not None:
            out.append(element)
    return out


def decorate_card_element(dao: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One card element, or ``None`` when it has nothing to say.

    See this section's header comment for the rule.
    """
    if dao.get("type") == "header" or dao.get("redacted"):
        return None

    name = str(dao.get("name") or dao.get("slug") or "")

    if dao.get("type") == "bool":
        if not dao.get("value"):
            return None
        caption = dao.get("trueLabel") or name
        return {
            **dao,
            "name": name,
            "label": str(caption),
            "presentation": PRESENTATION_NAME,
        }

    label, single, from_vocabulary = _card_caption(dao)
    if not label:
        return None

    unit = _card_unit(dao)
    if single and _is_numeric_caption(label):
        presentation = PRESENTATION_VALUE_UNIT if unit else PRESENTATION_NAME_VALUE
    else:
        presentation = PRESENTATION_VALUE

    element = {**dao, "name": name, "label": label, "presentation": presentation}
    if unit:
        element["unit"] = unit

    # ``name_value`` is the one presentation that puts two separate wire
    # fields («Этаж», «3») next to each other on a card, and a bare number
    # next to a bare caption reads as one glued phrase («HONOR · Модель 90» —
    # Д421). A vocabulary-backed caption (below, ``from_vocabulary``) is
    # exempt: its label is a catalogue TERM, and a term's own name already
    # reads as a natural prefix word («Этаж 3», «Комнат 2») — this is the
    # schema marking the name as a prefix word, via the vocabulary mechanism
    # it already uses to resolve the term's display text. A caption built
    # from the feature's raw stored value has no such term behind it, so its
    # name gets a trailing colon: any client already joining ``name`` and
    # ``label`` with a single space (the 0.21.3 contract's own reference
    # renderer) now reads «Модель: 90» instead of «Модель 90» — the fix
    # lands without a second, coordinated client release.
    if presentation == PRESENTATION_NAME_VALUE and not from_vocabulary:
        element["name"] = f"{name}:"
    return element


def _card_caption(dao: Dict[str, Any]) -> Tuple[str, bool, bool]:
    """``(caption, is a single value, is vocabulary-backed)`` for a non-bool DAO.

    The second half exists so a multi-select never takes the numeric branch:
    two labels joined can parse as a number («1, 2») and mean nothing of the
    kind.

    The third half tells :func:`decorate_card_element` whether the caption is
    a catalogue TERM (a ``select`` / ``ref_select`` option's ``labels``
    snapshot, or an already-resolved caption like ``hex_color``'s) rather than
    the feature's raw stored value rendered as text. Only the raw-value case
    gets locale number grouping and the ``name_value`` colon fix below — a
    vocabulary label is exactly what the catalogue wrote and this module
    still never touches it, the same rule ``_card_unit`` already applies to
    ``postfix``.
    """
    if dao.get("type") == "convertible_unit":
        return _convertible_caption(dao), True, False

    labels = dao.get("labels")
    if isinstance(labels, list):
        parts = [str(item) for item in labels if item not in (None, "")]
        if parts:
            return ", ".join(parts), len(parts) == 1, True

    # hex_color keeps its caption under ``label`` already; the contract reuses
    # it rather than printing a #RRGGBB at a person.
    existing = dao.get("label")
    if isinstance(existing, str) and existing:
        return existing, True, True

    value = dao.get("value")
    if isinstance(value, list):
        parts = [str(item) for item in value if item not in (None, "")]
        return ", ".join(parts), len(parts) == 1, True
    if value is None or value == "":
        return "", True, False
    if isinstance(value, bool):
        return str(value), True, False
    if isinstance(value, (int, float)):
        return _format_number(value), True, False
    return str(value), True, False


def _card_unit(dao: Dict[str, Any]) -> Optional[str]:
    """The feature's unit, translation key or literal as the catalogue wrote it.

    ``postfix`` is where a unit actually lives on an int / float / string
    feature (stapel-attributes stamps it onto the DAO from the config, so no
    schema fetch is needed here). ``postfix1000`` is deliberately NOT consulted:
    it is a typographic abbreviation of the VALUE («150 тыс. км» for 150000 км),
    and swapping it in would mean also rescaling ``label``, i.e. the card and
    the detail table would print different magnitudes for one stored number.
    """
    if dao.get("type") == "convertible_unit":
        code = dao.get("unit_m") or dao.get("unit_i")
        return f"feature.unit.{code}.name" if code else None
    postfix = dao.get("postfix")
    return str(postfix) if postfix else None


def _convertible_caption(dao: Dict[str, Any]) -> str:
    """The stored base-unit number rendered in the unit ``_card_unit`` reports.

    Mirrors ``ConvertibleUnitFeatureType.format_value``: the DAO's ``value`` is
    always in the family's base unit, so a caption paired with ``unit_m`` has
    to be converted or the card prints metres labelled kilometres.
    """
    value = dao.get("value")
    if value is None:
        return ""
    code = dao.get("unit_m") or dao.get("unit_i")
    unit_type = dao.get("unitType")
    if code and unit_type:
        try:
            from stapel_attributes.types.convertible_unit import convert_from_base

            value = convert_from_base(value, code, unit_type)
        except Exception:  # unknown family/unit — print the stored number
            pass
    return _format_number(value) if isinstance(value, (int, float)) else str(value)


def _trim_float(value: float) -> str:
    """``42.0`` -> ``"42"``, ``42.50`` -> ``"42.5"``."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


#: Below this magnitude a number is printed bare. RU typography groups
#: thousands from five digits up precisely so a plain four-digit number —
#: most often a year, on a real catalogue (``built_year``, ``year``) stored as
#: a raw ``int`` with no vocabulary behind it — is never split into «2 019».
#: Below the threshold nothing needed the exemption in the first place, so
#: one constant does both jobs: mileage (``20000`` -> «20 000», Д421) crosses
#: it, a year never does.
GROUPING_THRESHOLD = 10_000


def _format_number(value: float) -> str:
    """*value* with RU thousands grouping, non-breaking space as separator.

    ``42`` -> ``"42"``, ``20000`` -> ``"20\xa0000"``, ``2019`` -> ``"2019"``
    (below :data:`GROUPING_THRESHOLD`), ``-1234567.5`` ->
    ``"-1\xa0234\xa0567.5"``. The separator is U+00A0, the character
    :func:`_is_numeric_caption` already tolerates — that tolerance predates
    this function, written for a write-time vocabulary label that already
    carried one.
    """
    text = _trim_float(value) if isinstance(value, float) else str(value)
    negative = text.startswith("-")
    body = text[1:] if negative else text
    int_part, dot, frac_part = body.partition(".")
    if int(int_part) >= GROUPING_THRESHOLD:
        digits = int_part
        groups: List[str] = []
        while len(digits) > 3:
            groups.insert(0, digits[-3:])
            digits = digits[:-3]
        groups.insert(0, digits)
        int_part = "\xa0".join(groups)
    result = int_part + (f".{frac_part}" if dot else "")
    return f"-{result}" if negative else result


def _is_numeric_caption(caption: str) -> bool:
    """Whether this caption reads as a bare number to a person.

    Tolerant of the thin/space thousands separator and of a decimal comma,
    because a write-time label snapshot is whatever the catalogue's language
    produced.
    """
    text = caption.strip().replace(" ", "").replace(" ", "")
    if not text or text.count(",") + text.count(".") > 1:
        return False
    try:
        float(text.replace(",", "."))
    except ValueError:
        return False
    return True
