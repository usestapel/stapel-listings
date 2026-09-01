"""Documents this module hands to an indexer — the pull half of event→pull.

The ``listing.*`` events carry identity, not content (their payloads are
``additionalProperties: false`` and deliberately minimal). An indexer therefore
uses the event as a *signal* and pulls the document over comm:

- ``listings.search_documents`` — keyed batch, the live read for a signal;
- ``listings.search_export`` — cursor snapshot, the backfill/rebuild read.

Both return the same document shape from the same builder, so a rebuilt index
and a live-updated one cannot disagree about what a listing looks like. Values
are JSON-safe: ``Decimal`` as a string (never a float — a price must not be
rounded on the wire), datetimes as ISO 8601.

Soft-deleted listings are absent from both (``Listing.objects``): a key with no
row is a key with no document, which is exactly what an indexer needs to drop
it.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

from stapel_attributes import dao_visibility, public_daos
from stapel_attributes.visibility import PUBLIC


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _isoformat(value) -> str | None:
    return None if value is None else value.isoformat()


def sequence_of(listing) -> int:
    """Monotonic ordering token for a listing's current state.

    Unix milliseconds of ``updated_at`` — the same unit and origin as
    ``stapel_core.bus.Event.timestamp``, so a snapshot row and a live event
    for the same listing are directly comparable and the indexer's ordering
    guard can reject a stale one either way round.
    """
    return int(listing.updated_at.timestamp() * 1000) if listing.updated_at else 0


def hidden_slugs(listing) -> set[str]:
    """Slugs on this listing whose value no reader outside it may have.

    Read off the stored ``features`` DAOs, which carry their own
    ``visibility`` stamp (stapel-attributes 0.8.0) — no category schema needed.
    """
    return {
        str(dao["slug"])
        for dao in (listing.features or [])
        if isinstance(dao, dict) and dao.get("slug")
        and dao_visibility(dao) != PUBLIC
    }


def build_search_document(listing) -> dict[str, Any]:
    """The indexable document for one listing.

    Every field is either a stored column or a projection this module owns —
    nothing is computed on the fly, so the cost is one row read. ``status`` is
    included raw: index membership is the consumer's predicate over
    ``INDEXED_STATUSES``, not a boolean this module bakes in.

    **A non-public value is filtered again on the way out.**
    ``services.features`` already keeps it out of ``features_search`` and
    ``features_title`` at build time, so on a freshly projected row this costs
    one set difference and removes nothing. It is here for the rows projected
    by an older writer, which are the entire installed base on the day this
    ships: those columns still hold the VIN until
    ``listings_reproject_features`` has run, and an indexer that pulled one in
    the meantime would keep serving it as a filterable term long after the
    re-projection. The document is where this module's data stops being ours,
    so it is the last place that can still say no.
    """
    hidden = hidden_slugs(listing)
    features_search = {
        slug: values
        for slug, values in (listing.features_search or {}).items()
        if slug not in hidden
    }
    return {
        "title": listing.title or "",
        "description": listing.description or "",
        "language": listing.language or "",
        "category_id": str(listing.category_id or ""),
        "owner_id": str(listing.owner_id),
        "price": _decimal(listing.price),
        "currency": listing.currency or "",
        "price_base": _decimal(listing.price_base),
        "lat": _decimal(listing.lat),
        "lon": _decimal(listing.lon),
        "geohash": listing.geohash or "",
        "location_id": listing.location_id or "",
        "location_label": listing.location_label or "",
        "status": listing.status,
        "moderation_status": listing.moderation_status,
        "features_search": features_search,
        # ``features_title`` feeds the index's free-text arm, where a badge
        # attribute's value makes the document findable by `q=`. A hidden value
        # in there is the same oracle as a facet, spelled differently.
        "features_title": public_daos(listing.features_title or []),
        "images": listing.images or [],
        "published_at": _isoformat(listing.published_at),
        "updated_at": _isoformat(listing.updated_at),
    }


def documents_by_keys(keys: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """``{key: document}`` for *keys* — absent keys are simply missing.

    Mirrors ``stapel_core.comm.projections.read()``: keys are stringified and
    a key with no row does not appear in the result.
    """
    from ..models import Listing

    wanted = [str(k) for k in keys]
    if not wanted:
        return {}
    numeric = [k for k in wanted if k.lstrip("-").isdigit()]
    if not numeric:
        return {}
    rows = Listing.objects.filter(pk__in=numeric)
    return {str(row.pk): build_search_document(row) for row in rows}


def export_page(cursor: Any = None, limit: int = 500) -> dict[str, Any]:
    """One page of the full snapshot: ``{rows, cursor, total}``.

    Contract is ``stapel_core.comm.projections._iter_snapshot``'s verbatim:
    called with ``{"cursor": <opaque|None>, "limit": n}``, answers rows each
    carrying their source key and a ``seq``, plus the next cursor (``None``
    when exhausted) and the total.

    The cursor is the last primary key of the previous page — keyset paging,
    so a rebuild of a large corpus does not degrade the way OFFSET does and a
    row inserted mid-rebuild cannot shift a page under the reader.
    """
    from ..models import Listing

    limit = max(1, min(int(limit or 500), 1000))
    qs = Listing.objects.order_by("pk")
    total = qs.count()
    if cursor not in (None, ""):
        qs = qs.filter(pk__gt=int(cursor))

    listings = list(qs[:limit])
    rows = [
        {"key": str(row.pk), "seq": sequence_of(row), **build_search_document(row)}
        for row in listings
    ]
    next_cursor = str(listings[-1].pk) if len(listings) == limit else None
    return {"rows": rows, "cursor": next_cursor, "total": total}
