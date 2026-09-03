"""Refresh the four attribute projections a listing stores, from its draft and
the CURRENT category schema.

``publish_listing`` builds ``features`` / ``features_title`` /
``features_badges`` / ``features_search`` once, at publish time, and stores
them. That write-time snapshot is the right design and is not in question
here: a card renders its badges and a detail page renders its attribute table
without ever fetching the category, which is the only reason those columns
exist at all.

It BUILDS a projection that is missing and REFRESHES one that is stale, and
the summary counts the two apart. Building is not an afterthought: the pass
used to select ``exclude(features=[])`` — rows that already had a projection —
which meant a listing with a good draft and no projection was not merely
unrepaired but never examined, and so never reported. Fourteen listings on the
live stand sat with empty characteristics through every repair run for that
reason. The population is keyed on the DRAFT now; see ``_base_queryset``.

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

from django.db.models import Q

from stapel_attributes import validate_dto_structured
from stapel_attributes.results import ValidationStatus

from ..models import INDEXED_STATUSES, Listing
from . import category_schema
from .features import PROJECTION_FIELDS, build_projections_partial

logger = logging.getLogger(__name__)

# Why a row was left alone. Every one of these is COUNTED and its listing id
# logged — a projection this pass cannot re-derive is a fact about the data,
# not a reason to abort a run over the rest of the table.
SKIP_REASONS: tuple[str, ...] = (
    # ``categories.features`` could not answer for this listing's category:
    # the category was deleted, or no categories provider is wired at all.
    "category_unresolved",
    # The draft could not be validated AT ALL — it is not an object keyed by
    # slug, or the category's own ``rules`` break the grammar so no field can
    # be judged. This is the only remaining whole-listing validation skip:
    # a draft with SOME invalid fields is repaired per field (see
    # ``build_projections_partial``), because one drifted attribute is not a
    # reason to leave every other field on this listing printing storage
    # slugs.
    "draft_invalid",
    # The row carries projections but no draft to re-derive them from.
    # Projecting an empty draft would ERASE a listing's attributes, which is
    # data loss wearing a migration's clothes.
    "no_draft",
    # Anything else the projection raised. Recorded rather than swallowed.
    "projection_failed",
)

# The subset of SKIP_REASONS that means "there was damage here and this pass
# could not repair it". ``no_draft`` is deliberately NOT one: a row carrying
# projections with no draft to re-derive them from is not a failure, it is a
# row this pass does not apply to, and exiting non-zero over it would make the
# command red on catalogues that are perfectly healthy.
REPAIR_FAILURE_REASONS: tuple[str, ...] = (
    "category_unresolved",
    "draft_invalid",
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
        # ``changed`` split by what the change WAS. A run that builds is doing
        # different work from a run that refreshes, and a summary that cannot
        # tell them apart cannot show that building works at all.
        "built": 0,
        "refreshed": 0,
        "unchanged": 0,
        "skipped": 0,
        # Rows in scope this pass does not apply to: neither a draft nor a
        # projection. Counted, never silent — see ``_count_no_attributes``.
        "no_attributes": 0,
        "skipped_by_reason": {reason: 0 for reason in SKIP_REASONS},
        "skipped_ids": {reason: [] for reason in SKIP_REASONS},
        "skipped_ids_truncated": False,
        "events_emitted": 0,
        # Per-field repair bookkeeping. A listing can be BOTH changed and
        # carrying invalid fields — that is the normal outcome of a repair
        # and it is why "changed" alone is not enough to report.
        "repaired_with_invalid_fields": 0,
        "invalid_field_count": 0,
        "invalid_fields": {},
        "invalid_fields_truncated": False,
        "dry_run": False,
    }


def _record_invalid_fields(result: dict, listing_id, failures: dict) -> None:
    """Count and log the fields a listing could not re-derive.

    Loudly, per listing, with the slug and the engine's own message: the
    whole point of repairing around a bad field is that somebody still has to
    go and fix it, and a repair that hides what it worked around is how a
    catalogue quietly rots.
    """
    result["invalid_field_count"] += len(failures)
    if len(result["invalid_fields"]) < SKIPPED_ID_SAMPLE:
        result["invalid_fields"][listing_id] = dict(failures)
    else:
        result["invalid_fields_truncated"] = True
    for slug, message in failures.items():
        logger.warning(
            "listings_reproject_features: listing %s field %r not re-derived "
            "(kept its stored value): %s",
            listing_id, slug, message,
        )


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


#: A row whose ``features_draft`` holds something — the SOURCE a projection is
#: derived from, and therefore the thing that decides whether this pass applies.
HAS_DRAFT = Q(features_draft__isnull=False) & ~Q(features_draft={})

#: A row whose ``features`` holds something — a projection that already exists
#: and could be stale, or could be a snapshot whose draft has since gone.
HAS_PROJECTION = Q(features__isnull=False) & ~Q(features=[])


def _scoped(category_ids: Iterable[str] | None):
    """Every listing this run is responsible for, before any state filter.

    Soft-deleted rows are excluded (``Listing.objects``, not ``all_objects``):
    a deleted listing renders nowhere, already announced its ``listing.removed``
    to the index, and touching it here would announce an update for a document
    that is supposed to be gone.
    """
    qs = Listing.objects.all()
    if category_ids:
        qs = qs.filter(category_id__in=[str(c) for c in category_ids])
    return qs


def _base_queryset(category_ids: Iterable[str] | None):
    """Rows that have a draft to project FROM, or a projection to answer FOR.

    This used to be ``exclude(features=[])`` — rows that already HAVE a
    projection — and that was the defect. Keyed on the OUTPUT, the pass could
    refresh a stale projection and could never build a missing one: a listing
    carrying a perfectly good draft and no projection was not merely
    unrepaired, it was never examined, so no report ever named it. Fourteen
    listings on one live stand sat with empty characteristics through every
    repair run for exactly that reason.

    The population is keyed on the INPUT instead: ``HAS_DRAFT``. A draft is
    what ``build_projections_partial`` reads, so a row with one is a row this
    pass can answer for — whether the answer is a fresh build or a refresh.

    ``HAS_PROJECTION`` is unioned in rather than dropped, because
    projection-without-draft is not an absence, it is damage: a snapshot with
    no source. Those rows are examined and skipped as ``no_draft``, loudly, the
    way they always were. Narrowing the population to ``HAS_DRAFT`` alone would
    have traded one silence for another.

    What is left out — neither a draft nor a projection — is counted rather
    than ignored; see ``_count_no_attributes``.
    """
    return _scoped(category_ids).filter(HAS_DRAFT | HAS_PROJECTION).order_by("pk")


def _count_no_attributes(category_ids: Iterable[str] | None) -> int:
    """Rows in scope with neither a draft nor a projection.

    One aggregate, not a walk: there is nothing to derive and nothing to
    erase, so the pass genuinely does not apply to them. But it does have to
    SAY so. Silence about the rows outside the population is what let the
    original defect survive every repair run, so the summary accounts for the
    whole scope — ``examined + no_attributes`` is every listing the run was
    responsible for — and a population that starts skipping rows again shows
    up as a number that stops adding up.
    """
    return _scoped(category_ids).exclude(HAS_DRAFT | HAS_PROJECTION).count()


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

    Result keys: ``examined``, ``changed`` (split into ``built`` and
    ``refreshed``), ``unchanged``, ``skipped``, ``skipped_by_reason``,
    ``skipped_ids`` (a bounded sample; see ``SKIPPED_ID_SAMPLE``),
    ``skipped_ids_truncated``, ``no_attributes``, ``events_emitted``,
    ``dry_run``.

    ``examined + no_attributes`` is every listing in scope. That invariant is
    the point: the defect this pass was built around survived because rows
    outside the population produced no line in any report.
    """
    result = _new_result()
    result["dry_run"] = bool(dry_run)
    result["no_attributes"] = _count_no_attributes(category_ids)
    resolver = _ConfigResolver()

    for listing in _base_queryset(category_ids).iterator(chunk_size=batch_size):
        result["examined"] += 1
        features_draft = listing.features_draft or {}
        had_projection = bool(listing.features)

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
            verdict = validate_dto_structured(configs, features_draft)
        except Exception as exc:
            _record_skip(
                result, listing.pk, "projection_failed",
                f"validate_dto_structured raised {exc.__class__.__name__}: {exc}",
            )
            continue

        failures = _field_failures(verdict)
        if _ROOT in failures:
            # Not a field problem: the draft is not an object keyed by slug,
            # or the CATEGORY's rules break the grammar so no field can be
            # judged at all. Nothing here is per-field repairable.
            _record_skip(result, listing.pk, "draft_invalid", failures[_ROOT])
            continue

        if failures:
            _record_invalid_fields(result, listing.pk, failures)

        try:
            projections = build_projections_partial(
                configs,
                features_draft,
                skip_slugs=set(failures),
                stored_features=listing.features or [],
            )
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
        result["refreshed" if had_projection else "built"] += 1
        if failures:
            result["repaired_with_invalid_fields"] += 1
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


#: ``validate_dto_structured`` reports a failure that is not about any one
#: field under this slug — a draft that is not an object, or a category whose
#: ``rules`` break the grammar. Neither is repairable per field.
_ROOT = "_root"


def _field_failures(verdict) -> dict:
    """``{slug: message}`` for every field the current schema rejects.

    ``validate_dto_structured`` is used here instead of ``validate_dto``
    precisely because it answers per field. ``validate_dto`` raises one
    ``ValidationError`` for the whole draft, which is why this pass used to
    have to choose between projecting everything and projecting nothing.
    """
    if getattr(verdict, "valid", False):
        return {}
    failures: dict = {}
    for item in getattr(verdict, "results", []) or []:
        if getattr(item, "status", None) == ValidationStatus.VALIDATION_FAILED:
            failures[str(item.slug)] = item.message or str(item.error)
    return failures


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


def repair_failures(result: dict) -> int:
    """How many rows this pass was asked to repair and could not.

    The number the command's exit code is about. Kept here, beside the reason
    table it reads, so "what counts as a failure" is one definition rather
    than a filter written out again in the command.
    """
    return sum(
        result["skipped_by_reason"].get(reason, 0)
        for reason in REPAIR_FAILURE_REASONS
    )


__all__ = [
    "reproject_listings",
    "repair_failures",
    "SKIP_REASONS",
    "REPAIR_FAILURE_REASONS",
    "SKIPPED_ID_SAMPLE",
]
