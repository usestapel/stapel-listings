"""comm event publishers (Actions) of stapel-listings.

Events go through ``stapel_core.comm.emit`` (transport is deployment config:
in-process in a monolith, a bus in microservices) and — with the outbox
enabled — leave iff the surrounding DB transaction commits. Payload contracts
live in ``schemas/emits/*.json`` and are enforced under ``VALIDATE_SCHEMAS``.

Surface (see MODULE.md for the boundary notes):

- ``listing.submitted`` — a listing entered moderation; the moderation
  boundary. A future **stapel-moderation** module consumes it. This module
  does NOT run the moderation pipeline.
- ``listing.published`` / ``listing.updated`` / ``listing.removed`` — index
  lifecycle for a future **stapel-search** indexer. This module BUILDS the
  ``features_search`` projection and emits these events, but implements no
  search/filter endpoints itself.
"""
from __future__ import annotations


def _base_payload(listing) -> dict:
    return {
        "listing_id": listing.pk,
        "owner_id": str(listing.owner_id),
        "category_id": str(listing.category_id),
        "status": listing.status,
    }


def emit_listing_submitted(listing) -> None:
    """Emit ``listing.submitted`` — request moderation of a listing."""
    from stapel_core.comm import emit

    emit(
        "listing.submitted",
        {
            "listing_id": listing.pk,
            "owner_id": str(listing.owner_id),
            "category_id": str(listing.category_id),
            "title": listing.title_draft or listing.title or "",
            "description": listing.description or listing.description_draft or "",
            "language": listing.language or "",
        },
        key=str(listing.pk),
    )


def emit_listing_published(listing) -> None:
    """Emit ``listing.published`` — a listing entered the index."""
    from stapel_core.comm import emit

    payload = _base_payload(listing)
    payload["features_search"] = listing.features_search or {}
    emit("listing.published", payload, key=str(listing.pk))


def emit_listing_updated(listing) -> None:
    """Emit ``listing.updated`` — an indexed listing's content changed."""
    from stapel_core.comm import emit

    payload = _base_payload(listing)
    payload["features_search"] = listing.features_search or {}
    emit("listing.updated", payload, key=str(listing.pk))


def emit_listing_removed(listing, *, reason: str = "") -> None:
    """Emit ``listing.removed`` — a listing left the index."""
    from stapel_core.comm import emit

    payload = _base_payload(listing)
    payload["reason"] = reason or listing.status
    emit("listing.removed", payload, key=str(listing.pk))
