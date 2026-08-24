"""Backfill ``geohash`` / ``geohash_draft`` on listings that predate the
stamp-on-save wiring.

Before this fix, ``Listing.save()`` never called ``geo.geohash_encode`` —
stapel-geo's own MODULE.md documented listings as the consumer, but nothing
here ever made the call — so every listing carrying ``lat``/``lon`` (or
``lat_draft``/``lon_draft``) also carried an empty geohash. stapel-search
0.2.2 made the lat/lon box authoritative, so results stayed correct, but the
geohash prefilter could never use its index: every geo-filtered query fell
back to a full box scan. New writes are fixed by ``Listing.save()`` /
``compute_geohash_draft()``; this module is the one-time (or rerunnable)
pass over rows that were written before that.

Idempotent by construction: the population is defined as "has coordinates,
has no geohash", so a row this backfill stamps leaves the population, a
crash mid-run loses no progress, and a second full run touches nothing.
Graceful the same way ``compute_geohash_draft()`` is: when
``geo.geohash_encode`` is unreachable, a row is left unstamped and counted
as ``unresolved`` rather than raising — the backfill is safe to run before
stapel-geo is deployed and rerun once it is.
"""
from __future__ import annotations

import logging

from stapel_core.comm import call
from stapel_core.comm.exceptions import CommError

from ..models import Listing

logger = logging.getLogger(__name__)

# (lat field, lon field, geohash field) — the two independent populations a
# listing carries (models.py "Plain coordinates next to the geohash (§63)":
# published fields are promoted from the draft twins on publish, they are
# not two views of one column).
_FIELD_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("lat", "lon", "geohash"),
    ("lat_draft", "lon_draft", "geohash_draft"),
)


def _encode(lat, lon) -> str:
    """``geo.geohash_encode(lat, lon)`` -> geohash, or ``""`` on any failure.

    Mirrors ``Listing.compute_geohash_draft()`` exactly — same exception set,
    same "unknown beats wrong" stance — so a row this backfill cannot resolve
    today resolves identically the next time ``save()`` (or a rerun of this
    backfill) touches it.
    """
    try:
        result = call("geo.geohash_encode", {"lat": float(lat), "lon": float(lon)})
    except (CommError, LookupError, KeyError, TypeError, ValueError) as exc:
        logger.debug(
            "geo.geohash_encode unavailable for (%s, %s): %s",
            lat, lon, exc.__class__.__name__,
        )
        return ""
    geohash = result.get("geohash") if isinstance(result, dict) else None
    return geohash or ""


def _backfill_field_pair(
    *, lat_field: str, lon_field: str, geohash_field: str,
    batch_size: int, limit: int | None, dry_run: bool,
) -> dict:
    qs = (
        Listing.all_objects.filter(
            **{
                f"{lat_field}__isnull": False,
                f"{lon_field}__isnull": False,
                geohash_field: "",
            }
        )
        .order_by("pk")
        .only("pk", lat_field, lon_field, geohash_field)
    )
    if limit is not None:
        qs = qs[: max(0, int(limit))]

    stats = {"candidates": 0, "stamped": 0, "unresolved": 0}
    batch: list[Listing] = []
    for listing in qs.iterator():
        stats["candidates"] += 1
        if dry_run:
            continue
        geohash = _encode(getattr(listing, lat_field), getattr(listing, lon_field))
        if not geohash:
            stats["unresolved"] += 1
            continue
        setattr(listing, geohash_field, geohash)
        batch.append(listing)
        stats["stamped"] += 1
        if len(batch) >= batch_size:
            Listing.all_objects.bulk_update(batch, [geohash_field])
            batch = []
    if batch:
        Listing.all_objects.bulk_update(batch, [geohash_field])
    return stats


def backfill_geohashes(
    *, batch_size: int = 500, limit: int | None = None, dry_run: bool = False,
) -> dict:
    """Stamp geohash/geohash_draft on every listing that has coordinates and
    no geohash yet.

    Includes soft-deleted rows (``Listing.all_objects``) — the geohash column
    is a fact about coordinates the row already carries, not a lifecycle
    concern, and leaving deleted rows half-migrated is one more shape a
    future query has to special-case for no benefit.

    ``limit`` bounds each of the two populations (published, draft)
    independently — for running the backfill in slices on a large table, the
    same shape as ``video_backfill_scope --limit``.

    Returns ``{"published": {"candidates", "stamped", "unresolved"},
               "draft": {...}}``.
    """
    result = {}
    for lat_field, lon_field, geohash_field in _FIELD_PAIRS:
        key = "draft" if geohash_field.endswith("_draft") else "published"
        result[key] = _backfill_field_pair(
            lat_field=lat_field,
            lon_field=lon_field,
            geohash_field=geohash_field,
            batch_size=batch_size,
            limit=limit,
            dry_run=dry_run,
        )
    logger.info("listings_backfill_geohash: %r", result)
    return result


__all__ = ["backfill_geohashes"]
