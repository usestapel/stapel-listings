"""Rewrite the KEYS of stored drafts when a category renames a feature slug.

The failure this exists for, measured on a live fleet (2026-09-05). A catalogue
re-import renamed five feature slugs in place — ``make_ref_select`` → ``make``,
``body_type_ref_select`` → ``body_type``, and three more of the same shape. The
category schema moved; the listings did not. Every draft composed against the
old schema kept its answers under the OLD keys in ``features_draft``, so from
the moment the import landed:

* the facet for that feature went empty — nothing answers ``make`` any more;
* the search projection lost those values on the next re-projection;
* ``listings_reproject_features`` could not repair it, because it keys on the
  CURRENT slugs: an answer stored under ``make_ref_select`` is, to the current
  schema, an unknown field, and the pass would have DROPPED it.

A rename is therefore not a category-side edit at all. It is a two-sided data
migration: the schema moves in stapel-categories, and the stored answers move
here. This module is the second half, addressed by name over comm
(``listings.rename_feature_keys``) so the loader that performs the first half
can perform the second in the same run without importing this package.

What it does, per listing in the category subtree whose draft carries an old
key:

1. rewrite the KEY, in place, keeping the value and the key's position;
2. re-project the touched categories through the very same code path
   ``listings_reproject_features`` uses, so a repaired projection and a
   published one cannot mean different things;
3. let those writes emit ``listing.updated`` exactly as they normally do — a
   search index still holding a document without the renamed values is the
   half of the damage nobody would otherwise see.

**A collision is never resolved by guessing.** If a draft already holds the NEW
key as well as the old one, both are left exactly as they are and the pair is
reported as a conflict. The alternative — overwriting one answer with the other
— is a silent edit of a seller's data on a run whose whole purpose is to stop
being silent about renames.

**Scope.** ``category_id`` names a subtree, resolved rung by rung over
``categories.children`` (the ``CATEGORY_CHILDREN_FUNCTION`` seam) because a
feature defined on a parent is inherited by every category under it. That walk
sees ACTIVE rungs only, which is what that Function answers; coverage does not
depend on it in the case that matters, because ``load_catalog`` calls this hook
once per category it renamed a feature under. With no children provider wired
at all the call still runs, over the single category named, and says so in
``subtree_resolved``.
"""
from __future__ import annotations

import logging
from typing import Any

from ..conf import listings_settings
from ..models import Listing

logger = logging.getLogger(__name__)

#: Guard rail on the subtree walk: a catalogue is a tree of thousands, not of
#: millions, and a provider answering its own id as a child would otherwise
#: spin forever. A cycle is impossible in a tree and cheap to refuse anyway.
MAX_SUBTREE_NODES = 5000

#: At most this many conflicts are returned in the result payload; every one of
#: them is logged. The COUNT is always exact.
CONFLICT_SAMPLE = 200


class RenameError(ValueError):
    """The rename map itself is unusable — nothing was read or written."""


def validate_renames(renames: Any) -> dict:
    """Normalize and refuse a rename map that cannot mean anything.

    A rename onto itself, an empty key or a non-string one is not a
    conservative no-op: it is a typo in a migration that is about to rewrite
    seller data, and the run should stop before it touches a row.
    """
    if not isinstance(renames, dict) or not renames:
        raise RenameError("renames must be a non-empty {old_slug: new_slug} object")
    out: dict[str, str] = {}
    for old, new in renames.items():
        old_s, new_s = str(old).strip(), str(new).strip()
        if not old_s or not new_s:
            raise RenameError(f"empty slug in rename {old!r} -> {new!r}")
        if old_s == new_s:
            raise RenameError(f"rename {old_s!r} -> {new_s!r} renames nothing")
        out[old_s] = new_s
    # Two old slugs landing on one new slug would make the outcome depend on
    # dict order per listing. Refuse rather than pick.
    targets = list(out.values())
    if len(set(targets)) != len(targets):
        raise RenameError(
            "two or more old slugs rename onto the same new slug — "
            "the result would depend on iteration order"
        )
    overlap = set(out) & set(out.values())
    if overlap:
        raise RenameError(
            "a slug is both an old and a new key in the same map "
            f"({', '.join(sorted(overlap))}) — apply such a chain one step at a time"
        )
    return out


def subtree_ids(category_id: Any) -> tuple[list[str], bool]:
    """``([category_id, …descendants], resolved)`` over the children seam.

    ``resolved`` is False when no children provider answered — the caller is
    then working on the single category it named, and the result says so rather
    than implying a subtree it never walked.
    """
    from stapel_core.comm import FunctionNotRegistered, call

    root = str(category_id)
    ids = [root]
    seen = {root}
    frontier = [category_id]
    function = listings_settings.CATEGORY_CHILDREN_FUNCTION
    if not function:
        return ids, False
    while frontier:
        parent = frontier.pop()
        try:
            answer = call(function, {"parent_id": _as_int(parent)})
        except FunctionNotRegistered:
            logger.warning(
                "rename_feature_keys: no %r provider — working on category %s alone",
                function, root,
            )
            return ids, False
        except Exception as exc:  # comm is a network boundary
            logger.warning(
                "rename_feature_keys: %r failed for parent %s (%s: %s) — "
                "that rung's descendants are outside this run",
                function, parent, exc.__class__.__name__, exc,
            )
            continue
        for child in (answer or {}).get("children") or []:
            child_id = str(child.get("id"))
            if child_id in seen:
                continue
            seen.add(child_id)
            ids.append(child_id)
            if len(ids) >= MAX_SUBTREE_NODES:
                logger.warning(
                    "rename_feature_keys: subtree of category %s exceeds %d nodes "
                    "— stopping the walk here",
                    root, MAX_SUBTREE_NODES,
                )
                return ids, True
            if child.get("children_count"):
                frontier.append(child_id)
    return ids, True


def _as_int(value: Any) -> Any:
    """Send an all-digit opaque id as an integer (the children schema types it
    that way), leaving genuine string ids untouched — same coercion
    ``category_schema`` applies to ``categories.features``."""
    s = str(value)
    return int(s) if s.isdigit() else value


def rename_draft(draft: dict, renames: dict) -> tuple[dict, list[str], list[dict]]:
    """``(new_draft, renamed_keys, conflicts)`` for one stored draft.

    Key ORDER is preserved: a renamed key keeps its position, because the
    composer's per-field sidecar and every human reading the JSON take the
    order as the order the seller answered in.
    """
    renamed: list[str] = []
    conflicts: list[dict] = []
    out: dict = {}
    for key, value in draft.items():
        new_key = renames.get(key)
        if new_key is None:
            out[key] = value
            continue
        if new_key in draft:
            # Both answers exist. Neither is overwritten and neither is
            # dropped: this run has no way to know which one the seller meant.
            conflicts.append({"old": key, "new": new_key})
            out[key] = value
            continue
        out[new_key] = value
        renamed.append(key)
    return out, renamed, conflicts


def _new_result() -> dict:
    return {
        "listings_scanned": 0,
        "listings_changed": 0,
        "keys_renamed": 0,
        "conflicts": [],
        "conflicts_total": 0,
        "conflicts_truncated": False,
        "categories": [],
        "subtree_resolved": True,
        # Rows in scope this pass does not apply to. Counted, never silent.
        "deleted_skipped": 0,
        "dry_run": False,
        "reprojected": None,
    }


def rename_feature_keys(
    *,
    category_id: Any,
    renames: dict,
    dry_run: bool = False,
    batch_size: int = 500,
) -> dict:
    """Rewrite draft keys across a category subtree; re-project what moved.

    Returns ``{listings_scanned, listings_changed, keys_renamed, conflicts,
    categories, subtree_resolved, dry_run, reprojected}``.

    Soft-deleted listings are outside the pass (``Listing.objects``), the same
    population ``reproject_listings`` walks and for the same reason: a deleted
    row renders nowhere and has already told the index it is gone, so touching
    it here would announce an update for a document that is supposed to have
    disappeared. Their drafts keep the old keys; a restore is followed by a
    repair run, which is a fact worth knowing rather than a silence — the
    result counts them as ``deleted_skipped``.

    Idempotent: a second run finds no draft carrying an old key, changes
    nothing and emits nothing.
    """
    renames = validate_renames(renames)
    result = _new_result()
    result["dry_run"] = bool(dry_run)

    category_ids, resolved = subtree_ids(category_id)
    result["categories"] = category_ids
    result["subtree_resolved"] = resolved

    # The memoized schema is the one thing that can quietly undo this whole
    # pass. ``category_schema`` caches feature configs under a revision-keyed
    # entry, and the re-projection below reads it: if the pointer still names
    # the PRE-rename revision — the ``category.changed`` has not arrived yet,
    # or the source moved a slug without moving a revision — every listing is
    # re-projected against the schema that has just been retired, the renamed
    # answers read as unknown fields and the repair writes back exactly the
    # loss it was called to undo. A rename is proof the schema moved, so the
    # pass refuses to trust its own cache.
    from . import category_schema

    for cid in category_ids:
        category_schema.invalidate(cid)

    scope = Listing.objects.filter(category_id__in=category_ids).order_by("pk")
    result["deleted_skipped"] = (
        Listing.all_objects.filter(category_id__in=category_ids).count()
        - scope.count()
    )

    touched_categories: set[str] = set()
    for listing in scope.iterator(chunk_size=batch_size):
        draft = listing.features_draft or {}
        if not isinstance(draft, dict) or not draft:
            continue
        result["listings_scanned"] += 1
        if not any(old in draft for old in renames):
            continue

        new_draft, renamed, conflicts = rename_draft(draft, renames)
        for conflict in conflicts:
            _record_conflict(result, listing.pk, conflict)
        if not renamed:
            continue

        result["listings_changed"] += 1
        result["keys_renamed"] += len(renamed)
        touched_categories.add(str(listing.category_id))
        if dry_run:
            continue

        listing.features_draft = new_draft
        # Only the draft column. The projections are rebuilt below through the
        # repair pass rather than here, so there is exactly one implementation
        # of "what a projection is" in this library.
        listing.save(update_fields=["features_draft"])

    if not dry_run and touched_categories:
        from .reproject import reproject_listings

        result["reprojected"] = reproject_listings(
            category_ids=sorted(touched_categories), batch_size=batch_size
        )

    logger.info("listings.rename_feature_keys: %r", _loggable(result))
    return result


def _record_conflict(result: dict, listing_id: Any, conflict: dict) -> None:
    result["conflicts_total"] += 1
    if len(result["conflicts"]) < CONFLICT_SAMPLE:
        result["conflicts"].append(
            {"listing_id": str(listing_id), "old": conflict["old"], "new": conflict["new"]}
        )
    else:
        result["conflicts_truncated"] = True
    logger.warning(
        "listings.rename_feature_keys: listing %s already answers %r as well as "
        "%r — both kept, neither renamed",
        listing_id, conflict["new"], conflict["old"],
    )


def _loggable(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "categories"}


__all__ = [
    "rename_feature_keys",
    "rename_draft",
    "subtree_ids",
    "validate_renames",
    "RenameError",
    "MAX_SUBTREE_NODES",
    "CONFLICT_SAMPLE",
]
