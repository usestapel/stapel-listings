"""Claim CDN media for listings written before claim-on-save (0.14.0).

``Listing.save()`` announces a claim only when the claimed union MOVES
(it diffs the stored row against the write), so a scripted re-save of an
unchanged listing publishes nothing — rows that predate 0.14.0 stay
zero-ref on the CDN side and its orphan sweeper would reap their photos.
This module is the one-time (or rerunnable) pass over those rows.

Additive by construction: every event carries ``old_hashes=[]``, so
``apply_ref_sync``'s ``to_remove`` set is empty and a ref already claimed is
left exactly as it is — a rerun, or a run over rows the save-path already
claimed, adds nothing and releases nothing. Graceful the same way the
save-path sync is: a failed bus publish is counted as ``failed`` and the
pass keeps going; rerun once the bus is reachable.

Soft-deleted rows are skipped on purpose (unlike the geohash backfill's
``all_objects``): a deleted listing claims nothing — that IS the 0.14.0
contract (``Listing.cdn_image_refs()``) — and claiming for it would keep
media alive that the mandate says gets reaped.
"""
from __future__ import annotations

import logging

from ..models import Listing

logger = logging.getLogger(__name__)


def backfill_cdn_refs(*, limit: int | None = None, dry_run: bool = False) -> dict:
    """Publish an additive claim for every live listing that references media.

    ``limit`` bounds the number of candidate rows (rows that actually carry
    refs) — for running the pass in slices on a large table.

    Returns ``{"candidates": int, "published": int, "failed": int}``.
    """
    from stapel_core.django.cdn.ref_sync import sync_cdn_refs

    qs = (
        Listing.objects.order_by("pk")
        .only("pk", "images", "images_draft", "deleted_at")
    )

    stats = {"candidates": 0, "published": 0, "failed": 0}
    for listing in qs.iterator(chunk_size=500):
        refs = listing.cdn_image_refs()
        if not refs:
            continue
        stats["candidates"] += 1
        if not dry_run:
            result = sync_cdn_refs("listings", "listing", listing.pk, [], sorted(refs))
            if result is None or getattr(result, "ok", False):
                stats["published"] += 1
            else:
                stats["failed"] += 1
        if limit is not None and stats["candidates"] >= limit:
            break
    return stats
