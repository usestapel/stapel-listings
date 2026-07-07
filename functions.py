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
