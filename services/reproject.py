"""Refresh the four attribute projections a listing stores, from its draft and
the CURRENT category schema.

``publish_listing`` builds ``features`` / ``features_title`` /
``features_badges`` / ``features_search`` once, at publish time, and stores
them. That write-time snapshot is the right design and is not in question
here: a card renders its badges and a detail page renders its attribute table
without ever fetching the category, which is the only reason those columns
exist at all.

The cost of a snapshot is that it is exactly as fresh as the last publish, and
until now there was no way to refresh one. Two things make that bite:

1. Every listing published before stapel-attributes 0.7.0 carries ``select``
   DAOs with no ``labels`` — the copy simply was not in the projection the
   engine produced — so its cards keep printing storage slugs at people
   ("Condition: b-u") for as long as nobody publishes it again.
2. The same staleness has always applied to ``ref_select``'s label snapshot,
   and to any category whose option copy an owner edits after the fact.

This module is the pass that repairs both. It re-derives the projections
through :func:`stapel_listings.services.features.build_projections` — the same
function ``publish_listing`` uses, so a refreshed snapshot and a freshly
published one cannot mean different things — and writes back nothing else.

**It is not a re-publication.** Lifecycle ``status``, ``moderation_status``,
``moderation_note``, ``expires_at``, ``published_at``, ``created_at`` and
``updated_at`` are untouched, and no ``listing.submitted`` is emitted: an
owner's listing does not go back through moderation because we fixed the
rendering of a value they already had approved. The write is a
``save(update_fields=...)`` naming only the four projection columns.

**``listing.updated`` IS wanted and IS emitted** — see
:func:`reproject_listings` for the argument and for how it is counted.

Idempotent by construction: the new projections are compared against the
stored ones and a row that would not move is not written, so a second full run
reports zero changes and emits nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.exceptions import ValidationError

from stapel_attributes import validate_dto

from ..models import INDEXED_STATUSES, Listing
from . import category_schema
from .features import PROJECTION_FIELDS, build_projections

logger = logging.getLogger(__name__)

# Why a row was left alone. Every one of these is COUNTED and its listing id
# logged — a projection this pass cannot re-derive is a fact about the data,
# not a reason to abort a run over the rest of the table.
SKIP_REASONS: tuple[str, ...] = (
    # ``categories.features`` could not answer for this listing's category:
    # the category was deleted, or no categories provider is wired at all.
    "category_unresolved",
    # The stored draft no longer validates against the current schema — a
    # feature was removed from the category, or its bounds tightened. Same
    # policy as ``publish_listing``, which would also refuse it.
    "draft_invalid",
    # The row carries projections but no draft to re-derive them from.
    # Projecting an empty draft would ERASE a listing's attributes, which is
    # data loss wearing a migration's clothes.
    "no_draft",
    # Anything else the projection raised. Recorded rather than swallowed.
    "projection_failed",
)

# Skipped ids are logged one by one (a stream costs no memory); the result dict
# keeps at most this many per reason so a run over a table with a million
# broken rows cannot itself run out of memory. The COUNTS are always exact.
SKIPPED_ID_SAMPLE = 50


def _new_result() -> dict:
    return {
        "examined": 0,
        "changed": 0,
        "unchanged": 0,
        "skipped": 0,
        "skipped_by_reason": {reason: 0 for reason in SKIP_REASONS},
        "skipped_ids": {reason: [] for reason in SKIP_REASONS},
        "skipped_ids_truncated": False,
        "events_emitted": 0,
        "dry_run": False,
    }


def _record_skip(result: dict, listing_id: Any, reason: str, detail: str) -> None:
    result["skipped"] += 1
    result["skipped_by_reason"][reason] += 1
    sample = result["skipped_ids"][reason]
    if len(sample) < SKIPPED_ID_SAMPLE:
        sample.append(listing_id)
    else:
        result["skipped_ids_truncated"] = True
    logger.warning(
        "listings_reproject_features: skipped listing %s (%s): %s",
        listing_id, reason, detail,
    )


class _ConfigResolver:
    """Per-run memo over ``category_schema.get_feature_configs``.

    The service has its own revision-versioned cache, but that is a cache with
    a TTL and a remote call behind it; a re-projection walks thousands of rows
    that share a handful of categories, and a failure is worth remembering for
    the run just as much as a success is.
    """

    def __init__(self) -> None:
        self._configs: dict[str, Any] = {}
        self._failures: dict[str, str] = {}

    def get(self, category_id: Any):
        """Return configs, or raise ``LookupError`` carrying the reason."""
        key = str(category_id)
        if key in self._failures:
            raise LookupError(self._failures[key])
        if key in self._configs:
            return self._configs[key]
        try:
            configs = category_schema.get_feature_configs(category_id)
        except Exception as exc:  # comm is a network boundary: never fatal here
            detail = f"{exc.__class__.__name__}: {exc}"
            self._failures[key] = detail
            raise LookupError(detail) from exc
        self._configs[key] = configs
        return configs


def _base_queryset(category_ids: Iterable[str] | None):
    """Rows that HAVE projections — the population a re-projection is about.

    Soft-deleted rows are excluded (``Listing.objects``, not ``all_objects``):
    a deleted listing renders nowhere, already announced its ``listing.removed``
    to the index, and touching it here would announce an update for a document
    that is supposed to be gone.
    """
    qs = Listing.objects.exclude(features__isnull=True).exclude(features=[])
    if category_ids:
        qs = qs.filter(category_id__in=[str(c) for c in category_ids])
    return qs.order_by("pk")


def reproject_listings(
    *,
    category_ids: Iterable[str] | None = None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict:
    """Re-derive and write back the four projections. Returns a count summary.

    **The ``listing.updated`` decision, made explicitly.** ``Listing.save()``
    emits ``listing.updated`` itself when a save writes a field an index holds
    and the row is in an indexed status (``INDEXED_CONTENT_FIELDS`` covers all
    four projection columns). We deliberately do NOT suppress it, and we
    deliberately do NOT use ``bulk_update`` — which would be faster and would
    emit nothing:

    a search index holding the stale text is *exactly* the failure this
    command exists to repair. Repairing the database row and leaving the index
    serving "Condition: b-u" would fix the half nobody looks at. So the write
    goes row by row through ``save(update_fields=[...])``, the event rides the
    same transaction ``save()`` already puts it in, and the number of events
    the run will produce is returned as ``events_emitted`` — a changed row in
    an indexed status — so the decision is observable in the summary rather
    than being something a reader has to infer.

    ``listing.submitted`` is NOT emitted: this is not a re-publication.

    Result keys: ``examined``, ``changed``, ``unchanged``, ``skipped``,
    ``skipped_by_reason``, ``skipped_ids`` (a bounded sample; see
    ``SKIPPED_ID_SAMPLE``), ``skipped_ids_truncated``, ``events_emitted``,
    ``dry_run``.
    """
    result = _new_result()
    result["dry_run"] = bool(dry_run)
    resolver = _ConfigResolver()

    for listing in _base_queryset(category_ids).iterator(chunk_size=batch_size):
        result["examined"] += 1
        features_draft = listing.features_draft or {}

        if not features_draft:
            _record_skip(
                result, listing.pk, "no_draft",
                "features_draft is empty; refusing to erase stored projections",
            )
            continue

        try:
            configs = resolver.get(listing.category_id)
        except LookupError as exc:
            _record_skip(
                result, listing.pk, "category_unresolved",
                f"category {listing.category_id}: {exc}",
            )
            continue

        try:
            validate_dto(configs, features_draft)
        except ValidationError as exc:
            _record_skip(result, listing.pk, "draft_invalid", str(exc))
            continue
        except Exception as exc:
            _record_skip(
                result, listing.pk, "projection_failed",
                f"validate_dto raised {exc.__class__.__name__}: {exc}",
            )
            continue

        try:
            projections = build_projections(configs, features_draft)
        except Exception as exc:
            _record_skip(
                result, listing.pk, "projection_failed",
                f"{exc.__class__.__name__}: {exc}",
            )
            continue

        if all(
            (getattr(listing, field) or _empty_for(field)) == projections[field]
            for field in PROJECTION_FIELDS
        ):
            result["unchanged"] += 1
            continue

        result["changed"] += 1
        if listing.status in INDEXED_STATUSES:
            result["events_emitted"] += 1
        if dry_run:
            continue

        for field, value in projections.items():
            setattr(listing, field, value)
        # Only the four derived columns. Notably NOT ``updated_at``: a listing
        # whose rendering we repaired was not edited by anyone, and a bumped
        # timestamp would tell every "recently updated" sort that it was.
        listing.save(update_fields=list(PROJECTION_FIELDS))

    logger.info("listings_reproject_features: %r", _loggable(result))
    return result


def _empty_for(field: str):
    """The zero value of a projection column, for comparing a NULL row.

    ``features``/``features_title``/``features_badges`` default to ``[]`` and
    ``features_search`` to ``{}``, but the columns are nullable and old rows
    can hold NULL; without this an all-empty projection would read as "changed"
    on every single run and idempotence would be a claim, not a property.
    """
    return {} if field == "features_search" else []


def _loggable(result: dict) -> dict:
    """The summary without the id samples (they are already logged one by one)."""
    return {k: v for k, v in result.items() if k != "skipped_ids"}


__all__ = ["reproject_listings", "SKIP_REASONS", "SKIPPED_ID_SAMPLE"]
