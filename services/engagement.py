"""View counting: what a buyer's attention costs the database.

The naive version of this feature is one UPDATE per page read, and it is
wrong twice over. It puts a write on the hottest read in the product — the
listing page — where a popular row becomes a contention point; and it counts
a reload, an owner's own check, and a crawler as demand, so the number rises
without meaning anything.

So the shape here is a FUNNEL, not a counter:

1. An open with nothing to identify the viewer by is not counted at all. A
   number that rises once per HTTP request is a request log, not a view
   count, and there is no honest way to deduplicate what you cannot name.
2. The owner's own opens are refused. A seller reloading their own listing
   is not demand, and a counter a seller can inflate is worse than none.
3. Everything else is deduplicated in the CACHE for
   ``VIEW_DEDUP_WINDOW_SECONDS``. This is the buffer: every open after the
   first, inside the window, costs one cache read and touches no database at
   all (``test_a_repeat_open_writes_nothing`` counts the queries rather than
   trusting this paragraph).
4. Only what survives all three writes: one increment of
   ``Listing.view_count``, plus — for a signed-in viewer — one upsert of
   ``ListingView`` so «просмотрено» can be answered later.

That leaves at most ONE write per (viewer, listing, window). It is not a
write per read, which is the property the design owes; making it a periodic
flush instead would buy a smaller constant at the price of a counter that
silently stops moving when the flush is not scheduled, and this fleet has no
beat schedule wired for this service (``listings.W002`` says so where it
matters — a per-process cache, which would let each worker count the same
viewer once).

The dedup key is the thing to read carefully, because it is where a view
counter usually starts lying:

- signed in: the user id. Exact.
- anonymous with a session: the session key. Exact per browser profile.
- anonymous without one: a hash of whatever the caller passes as
  ``client_key`` (the HTTP layer passes IP + User-Agent). COARSE, and
  declared so: two people behind one NAT running the same browser are one
  viewer here. The count is therefore a floor. That is the honest direction
  to be wrong in — it under-counts rather than inventing an audience — and
  it is why no attempt is made to dress the fingerprint up with more
  entropy, which would only make a shakier number look firmer.
"""
from __future__ import annotations

import hashlib

_CACHE_PREFIX = "stapel_listings:viewed:"


def viewer_key(*, user=None, session_key: str = "", client_key: str = "") -> str:
    """Stable identity for the deduplication window, or ``""`` for nobody.

    Ordered strongest first. The empty string is a real answer — it means
    "this open cannot be attributed", and :func:`record_view` refuses to
    count it rather than counting it as its own new viewer.
    """
    if user is not None and getattr(user, "is_authenticated", False):
        return f"u:{user.pk}"
    if session_key:
        return f"s:{session_key}"
    if client_key:
        # Hashed, not stored raw: the key lives in a shared cache, and an
        # IP plus User-Agent is personal data that this module has no reason
        # to be able to read back.
        digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()
        return f"a:{digest[:32]}"
    return ""


def _dedup_key(listing_id, viewer: str) -> str:
    return f"{_CACHE_PREFIX}{listing_id}:{viewer}"


def record_view(listing, *, user=None, session_key: str = "", client_key: str = "") -> bool:
    """Count one open of *listing*. Returns whether it counted.

    Idempotent inside the window by construction: ``cache.add`` is the
    atomic test-and-set, so two concurrent opens by one viewer produce one
    increment and not two.
    """
    from django.core.cache import cache
    from django.db.models import F
    from django.utils import timezone

    from ..conf import listings_settings
    from ..models import INDEXED_STATUSES, Listing, ListingView

    if listing.status not in INDEXED_STATUSES:
        # An unpublished listing has no audience to count. Its owner is
        # usually the only one who can open it at all, and a draft that
        # accumulates views would be measuring the composer.
        return False

    viewer = viewer_key(user=user, session_key=session_key, client_key=client_key)
    if not viewer:
        return False
    if user is not None and getattr(user, "is_authenticated", False):
        if str(user.pk) == str(listing.owner_id):
            return False

    window = int(listings_settings.VIEW_DEDUP_WINDOW_SECONDS)
    if not cache.add(_dedup_key(listing.pk, viewer), 1, window):
        return False

    Listing.all_objects.filter(pk=listing.pk).update(view_count=F("view_count") + 1)
    if user is not None and getattr(user, "is_authenticated", False):
        # `update_or_create` rather than `get_or_create`: `last_seen_at` is
        # what "recently viewed" reads, so a return visit in a later window
        # has to move it. `first_seen_at` is auto_now_add and stays put.
        ListingView.objects.update_or_create(
            user_id=user.pk,
            listing_id=listing.pk,
            defaults={"last_seen_at": timezone.now()},
        )
    return True


def engagement_for(keys, *, user_id: str = "") -> dict[str, dict]:
    """``{listing key: {view_count, viewed, is_favorited}}`` for a batch.

    The read behind ``listings.engagement``. It exists because a SERP's cards
    come out of the search index, which cannot hold either per-viewer flag —
    ``viewed`` and ``is_favorited`` are different for every reader, and
    ``view_count`` moves far faster than a document that is re-indexed on a
    listing event. Overlaying them afterwards, in ONE query for the whole
    page, is the only shape that is both correct and not N+1.

    ``viewed`` / ``is_favorited`` are ``None`` without a *user_id* — the same
    three-state contract the annotations carry, for the same reason: unknown
    is not false.
    """
    from ..models import Listing

    wanted = [str(key) for key in (keys or []) if str(key)]
    numeric = [key for key in wanted if key.isdigit()]
    if not numeric:
        return {}

    rows = Listing.objects.filter(pk__in=numeric)
    if user_id:
        # A user id that no longer resolves annotates False, not an error:
        # the flags describe a reader, and a reader who is gone has read
        # nothing. Nothing here trusts the id beyond an equality test.
        from django.db.models import BooleanField, Exists, OuterRef, Value

        from ..models import Favorite, ListingView

        rows = rows.annotate(
            viewed=Exists(
                ListingView.objects.filter(user_id=user_id, listing_id=OuterRef("pk"))
            ),
            is_favorited=Exists(
                Favorite.objects.filter(user_id=user_id, listing_id=OuterRef("pk"))
            ),
        )
    else:
        from django.db.models import BooleanField, Value

        rows = rows.annotate(
            viewed=Value(None, output_field=BooleanField()),
            is_favorited=Value(None, output_field=BooleanField()),
        )

    return {
        str(row.pk): {
            "view_count": int(row.view_count),
            "viewed": row.viewed,
            "is_favorited": row.is_favorited,
        }
        for row in rows.only("pk", "view_count")
    }


__all__ = ["engagement_for", "record_view", "viewer_key"]
