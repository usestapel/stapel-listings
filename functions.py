"""comm Functions provided by stapel-listings.

Every Function carries a JSON schema in ``schemas/functions/`` — tests run with
``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails loudly.
Registration happens on import from ``apps.py:ready()``; re-imports are no-ops.

    from stapel_core.comm import call
    call("listings.status", {"listing_id": 42})
    # -> {"listing_id", "owner_id", "status", "moderation_status",
    #     "is_active", "is_deleted"}

``listings.status`` is the inter-service status probe (replacing the legacy
catalog's ``AdStatusSerializer`` "inter-service validation" endpoint): moderation,
reviews and search can check a listing's state without an HTTP round-trip or a
cross-module import. Raises ``LookupError`` for an unknown listing.

The other three answer the two modules that consume listings but must not read
its database (MODULE.md: "talk over comm by string name"):

- ``listings.search_documents`` / ``listings.search_export`` — the search seam.
  Events are the signal, these are the document: a keyed batch for live
  re-indexing and a cursor snapshot for backfill/rebuild/drift-check. Nothing
  about a search engine leaks in here — this module only says what a listing
  IS.
- ``listings.engagement`` — the storefront seam: the per-viewer overlay
  (view count, viewed, favorited) a card built from the search index cannot
  carry, batched for a whole page.
- ``listings.moderation_content`` — the moderation seam. The verdict bus
  carries identifiers only, so the screener reads content through this call at
  the moment it screens, not from a six-hour-old event payload.
- ``listings.rename_feature_keys`` — the catalogue seam, and the only WRITE
  among these. A feature-slug rename is a two-sided migration: the schema moves
  in stapel-categories, the stored answers move here. The loader performing the
  first half calls this by name to perform the second.
- ``listings.draft_content`` — the composer seam, and the only read here that
  answers the DRAFT twins first. A service that analyses a draft is addressed
  by the draft id and has no body to read the content from; the public detail
  read serves the published columns, which are empty on a listing that has
  never been published. Owner-scoped by payload.
"""
import json
from pathlib import Path

from stapel_core.comm import function

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"


def _schema(name: str) -> dict:
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@function("listings.status", schema=_schema("listings.status"))
def status_function(payload: dict) -> dict:
    """Resolve a listing's lifecycle/moderation state for another service."""
    from .models import Listing

    listing_id = payload["listing_id"]
    try:
        listing = Listing.all_objects.get(pk=listing_id)
    except Listing.DoesNotExist:
        raise LookupError(f"listing {listing_id} not found") from None

    return {
        "listing_id": listing.pk,
        "owner_id": str(listing.owner_id),
        "status": listing.status,
        "moderation_status": listing.moderation_status,
        "is_active": listing.is_active,
        "is_deleted": listing.is_deleted,
    }


@function("listings.search_documents", schema=_schema("listings.search_documents"))
def search_documents_function(payload: dict) -> dict:
    """Keyed batch of indexable documents: ``{key: {...}}``, absent = no row."""
    from .services.search_feed import documents_by_keys

    return documents_by_keys(payload.get("keys") or [])


@function("listings.engagement", schema=_schema("listings.engagement"))
def engagement_function(payload: dict) -> dict:
    """Per-viewer engagement overlay for a page of listings.

    The read a SERP needs and the search index cannot serve: ``view_count``
    changes far faster than a document re-indexed on a listing event, and
    ``viewed`` / ``is_favorited`` are a property of the READER, not of the
    listing. One call per page, never one per card.
    """
    from .services.engagement import engagement_for

    return engagement_for(
        payload.get("keys") or [], user_id=str(payload.get("user_id") or "")
    )


@function("listings.search_export", schema=_schema("listings.search_export"))
def search_export_function(payload: dict) -> dict:
    """One snapshot page: ``{rows, cursor, total}`` (rebuild/backfill read)."""
    from .services.search_feed import export_page

    return export_page(payload.get("cursor"), payload.get("limit") or 500)


@function("listings.moderation_content", schema=_schema("listings.moderation_content"))
def moderation_content_function(payload: dict) -> dict:
    """Content of one listing for a screener or a moderator's card.

    Published fields first, draft twins as the fallback: what is live is what
    is moderated, and a listing still on its way to publication is moderated on
    the draft that is about to become live.
    """
    from .conf import listings_settings
    from .models import Listing

    listing_id = payload["listing_id"]
    try:
        listing = Listing.all_objects.get(pk=listing_id)
    except (Listing.DoesNotExist, ValueError, TypeError):
        raise LookupError(f"listing {listing_id} not found") from None

    url_template = listings_settings.LISTING_URL_TEMPLATE or ""
    return {
        "listing_id": listing.pk,
        "title": listing.title or listing.title_draft or "",
        "text": listing.description or listing.description_draft or "",
        "language": listing.language or "",
        "media": list(listing.images or listing.images_draft or []),
        "author_id": str(listing.owner_id),
        "url": url_template.format(listing_id=listing.pk) if url_template else "",
        "status": listing.status,
        "moderation_status": listing.moderation_status,
    }


@function("listings.draft_content", schema=_schema("listings.draft_content"))
def draft_content_function(payload: dict) -> dict:
    """Owner-scoped read of one listing's DRAFT twins, for another service.

    The read this exists for: a service that ANALYSES a draft — recognises
    the photographs, drafts the text, fills the characteristics — and is
    addressed by the draft id alone. It has no request body to read the
    content out of (the composer sends none: the photos are already
    uploaded and the row is the truth), it must not read this module's
    database, and the only HTTP read it could reach is the PUBLIC detail
    one, which serves the published columns — empty on a listing that has
    never been published. Measured on a live stand: the analysis job hashed
    two empty strings, ran every stage over nothing, and the screening
    stage reported the listing as empty content, while ``images_draft``
    held both photographs the whole time.

    So: **draft twin first, the published column only as a fallback**. That
    order is the opposite of :func:`moderation_content_function`, and
    deliberately so — screening judges what is LIVE, while a composer works
    on what the seller is writing. A published listing being edited answers
    with the edit in progress, which is what a composer must see.

    Owner-scoped by payload: ``owner_id`` is required and a mismatch raises
    ``LookupError`` — the same error, and the same words, as a listing that
    does not exist. A caller that could tell "not yours" from "not there"
    would be an existence oracle over every draft on the marketplace.

    Payload: ``{"listing_id": <id>, "owner_id": "<uuid>"}``. Returns::

        {listing_id, owner_id, category_id, title, description, images,
         features, language, is_empty}

    ``features`` is the seller's own ``features_draft`` map
    (``{slug: value}``). It has no published fallback on purpose: the
    published ``features`` column is a rendering PROJECTION (a list of
    display rows), not a slug→value map, and answering one shape where the
    caller expects the other is worse than answering nothing.

    ``is_empty`` is the module's own answer to "is there anything here to
    work on" — no photographs and no words — so every caller does not
    reimplement it and disagree about whitespace.
    """
    from .models import Listing

    listing_id = payload["listing_id"]
    owner_id = str(payload.get("owner_id") or "")
    try:
        listing = Listing.all_objects.get(pk=listing_id)
    except (Listing.DoesNotExist, ValueError, TypeError):
        raise LookupError(f"listing {listing_id} not found") from None
    if not owner_id or str(listing.owner_id) != owner_id:
        raise LookupError(f"listing {listing_id} not found")

    title = listing.title_draft or listing.title or ""
    description = listing.description_draft or listing.description or ""
    images = list(listing.images_draft or listing.images or [])
    features = listing.features_draft or {}
    return {
        "listing_id": listing.pk,
        "owner_id": str(listing.owner_id),
        # The module's own spelling: an id, as a string, or an explicit
        # null for a draft that has not been placed yet (0.21.4).
        "category_id": str(listing.category_id) if listing.category_id else None,
        "title": title,
        "description": description,
        "images": images,
        "features": features if isinstance(features, dict) else {},
        "language": listing.language or "",
        "is_empty": not images and not title.strip() and not description.strip(),
    }


@function("listings.rename_feature_keys", schema=_schema("listings.rename_feature_keys"))
def rename_feature_keys_function(payload: dict) -> dict:
    """Move stored draft KEYS to follow a category that renamed a feature slug.

    The WRITE half of a two-sided migration. A catalogue import renames a slug
    in place (``make_ref_select`` → ``make``); the schema moves and the stored
    answers do not, so the facet empties, the search projection loses the
    values, and ``listings_reproject_features`` — which keys on the CURRENT
    slugs — would have dropped them rather than repaired them. Measured on a
    live fleet on 2026-09-05 over five car features at once.

    So the loader that renames the slug calls this by name in the same run
    (stapel-categories ``load_catalog --rename-features``, whose hook this is
    the default of), and a person can call it by hand through
    ``manage.py listings_rename_feature_keys``.

    Payload: ``{"category_id": <id>, "renames": {old: new}, "dry_run": bool}``.
    The subtree under ``category_id`` is included — a feature defined on a
    parent is answered by listings in every category beneath it.

    Answers ``{listings_scanned, listings_changed, keys_renamed, conflicts,
    …}``. A draft already answering the NEW key as well as the old one is a
    ``conflict``: both keys are kept exactly as they are, because nothing here
    knows which answer the seller meant, and overwriting one with the other
    would be a silent edit of their data on a run whose point is to stop being
    silent. Raises ``ValueError`` for a rename map that cannot mean anything
    (empty, onto itself, two olds onto one new, or a chain).
    """
    from .services.rename_features import rename_feature_keys

    return rename_feature_keys(
        category_id=payload["category_id"],
        renames=payload["renames"],
        dry_run=bool(payload.get("dry_run", False)),
    )
