"""Django system checks for stapel-listings configuration.

Policy (docs/library-standard.md §3.7): E-level for configuration the
service cannot run with; W-level for entries that degrade lazily (a broken
*unused* dotted path must not block deploys).
"""
from __future__ import annotations

from django.core import checks


@checks.register(checks.Tags.compatibility)
def check_moderation_gate(app_configs, **kwargs):
    """E001 — ``MODERATION_GATE`` outside {"pre", "post"} decides nothing.

    An ERROR rather than a warning because the failure mode is silent and
    directional: ``publish_listing`` tests ``== "post"``, so any misspelling
    behaves as "pre" — the operator who believes they turned post-moderation
    on still has every first publication stuck in PENDING, which on a
    moderator-less stand is exactly the invisible-forever limbo the key
    exists to end. The same wording rule as stapel_moderation.E003, which
    guards the other half of this policy (the per-target ``gate`` in the
    moderation registry).
    """
    from .conf import listings_settings

    gate = listings_settings.MODERATION_GATE
    if gate in ("pre", "post"):
        return []
    return [
        checks.Error(
            f"STAPEL_LISTINGS['MODERATION_GATE'] is {gate!r}; only 'pre' and "
            f"'post' are meaningful. Anything else silently behaves as 'pre' "
            f"— first publications wait in PENDING for a verdict that a "
            f"deployment without moderators will never produce.",
            hint="Set it to 'pre' (hold first publications for a verdict) or "
                 "'post' (publish first, review after; a rejecting verdict "
                 "takes the listing down), or delete the key for the 'pre' "
                 "default.",
            id="stapel_listings.E001",
        )
    ]



@checks.register("stapel_listings")
def check_view_dedup_cache(app_configs, **kwargs):
    """W001: a per-process cache makes the view counter count workers.

    View deduplication IS the cache (``services/engagement.py``): one
    ``cache.add`` per (viewer, listing, window) is what keeps a reload from
    becoming a write, and what keeps the number a count of PEOPLE rather
    than of requests. Under ``LocMemCache`` each gunicorn worker holds its
    own window, so one buyer refreshing a page is counted once per worker —
    the counter still rises, the endpoint still answers 200, and the number
    is quietly a multiple of the truth.

    A warning rather than an error, and deliberately: a single-process dev
    server is a legitimate place to run under LocMem, and a library that
    refuses to boot without Redis is a library nobody can try out.
    """
    from django.conf import settings

    backend = str(
        (getattr(settings, "CACHES", {}) or {}).get("default", {}).get("BACKEND", "")
    )
    if "locmem" not in backend.lower():
        return []
    return [
        checks.Warning(
            "The default cache is a per-process LocMemCache, and view "
            "deduplication is held in it: with more than one worker process, "
            "one viewer's single open is counted once per worker and "
            "Listing.view_count is a multiple of the real number.",
            hint="Point CACHES['default'] at a shared backend (Redis, "
                 "Memcached) in any deployment that runs more than one "
                 "process. Harmless on a single-process dev server.",
            id="stapel_listings.W001",
        )
    ]


__all__ = ["check_moderation_gate", "check_view_dedup_cache"]
