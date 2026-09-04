"""Models for stapel-listings.

Ported from the legacy catalog's ``ads`` app (the ``Ad`` model), generalized to a
framework-neutral ``Listing`` and decoupled from its sibling services:

- **category is opaque**: ``category_id`` is a plain string, never a FK to
  stapel-categories. The feature schema used to validate attribute values is
  fetched through the ``categories.features`` comm Function
  (``services.category_schema``); a ``category.changed`` subscription
  invalidates the cache.
- **currency is opaque**: ``currency`` is a bare ISO code; ``price_base`` is
  computed through the ``PRICE_BASE_CONVERTER`` seam (identity by default),
  not a FK to stapel-currencies.
- the ``UserAdLike`` / ``UserAdView`` external-stats read-caches are dropped —
  engagement is a first-class :class:`Favorite`.

House rules (docs/library-standard.md §3.8): cross-service references are
opaque id fields (no FK across a service boundary); the user is only
``settings.AUTH_USER_MODEL``; index names must be <= 30 chars.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.utils import timezone

from stapel_core.comm import mutate_and_emit

from .conf import listings_settings

logger = logging.getLogger(__name__)


def validate_countable_stock(countable: bool, stock_quantity: int | None) -> None:
    """Enforce the ``countable`` / ``stock_quantity`` invariant.

    ``countable=True`` (a physical good — the default, matching every listing
    that existed before this field pair) requires a non-negative
    ``stock_quantity``; ``countable=False`` (a service — "how many" doesn't
    apply) requires it to be ``NULL``. Mirrors the DB
    ``listing_stock_invariant_chk`` constraint on :class:`Listing.Meta`, which
    is the storage-level backstop for writes that bypass this function
    (bulk operations, raw SQL, a future admin bulk-action).

    Deliberately **not** wired into ``Listing.save()`` — the lifecycle methods
    (``transition_to``, ``apply_moderation``) intentionally save a narrow
    ``update_fields`` list that never touches these two fields, and forcing a
    full ``full_clean()`` there would validate unrelated fields these methods
    have no business checking. Called explicitly from ``Listing.clean()`` (so
    admin/``full_clean()`` callers get it) and from
    ``ListingDraftSerializer.validate()`` (so the API does).
    """
    if countable:
        if stock_quantity is None:
            raise ValidationError(
                {"stock_quantity": "stock_quantity is required when countable is True."}
            )
        if stock_quantity < 0:
            raise ValidationError({"stock_quantity": "stock_quantity must be >= 0."})
    elif stock_quantity is not None:
        raise ValidationError(
            {
                "stock_quantity": (
                    "stock_quantity must be empty when countable is False "
                    "(the listing is a service — a quantity doesn't apply)."
                )
            }
        )


class ListingStatus(models.TextChoices):
    """Lifecycle state machine of a listing."""

    DRAFT = "draft", "Draft"
    PENDING = "pending", "Pending Moderation"
    PUBLISHED = "published", "Published"
    PAUSED = "paused", "Paused"
    EXPIRED = "expired", "Expired"
    SOLD = "sold", "Sold"
    REJECTED = "rejected", "Rejected"
    BLOCKED = "blocked", "Blocked (moderation takedown)"
    ARCHIVED = "archived", "Archived"


class ModerationStatus(models.TextChoices):
    """Content-moderation state machine (independent of the lifecycle).

    ``NOT_SUBMITTED`` is the default, and it exists because ``PENDING`` was.
    ``pending`` is a claim about a QUEUE — someone is waiting on a decision —
    and it was this field's default, so every draft ever created announced
    itself as awaiting moderation from the moment the row existed. A live
    stand carried 167 drafts saying so with not one moderation case behind
    any of them; a cabinet that renders this field verbatim told each of
    those sellers their empty draft was under review, and offered them
    nothing to do about it.

    The distinction has to live in the data, not in a presenter, because
    every reader of this field asks the same question and would otherwise
    each have to re-derive "...unless it was never published" from a second
    column. ``publish_listing`` sets ``PENDING`` unconditionally, so the
    moment anything IS submitted the word is earned.
    """

    NOT_SUBMITTED = "not_submitted", "Not submitted for review"
    PENDING = "pending", "Pending Review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    NEEDS_REVIEW = "needs_review", "Needs Manual Review"


# Allowed lifecycle transitions. The value set is the whitelist of statuses a
# listing may move to *from* the key status. Enforced by ``transition_to``.
LISTING_TRANSITIONS: dict[str, set[str]] = {
    ListingStatus.DRAFT: {ListingStatus.PENDING, ListingStatus.ARCHIVED},
    ListingStatus.PENDING: {
        ListingStatus.PUBLISHED,
        ListingStatus.REJECTED,
        ListingStatus.DRAFT,
        ListingStatus.ARCHIVED,
    },
    ListingStatus.PUBLISHED: {
        ListingStatus.PAUSED,
        ListingStatus.EXPIRED,
        ListingStatus.SOLD,
        ListingStatus.BLOCKED,
        ListingStatus.ARCHIVED,
    },
    # Takedown of a live listing. Reachable only from PUBLISHED and only
    # through ``apply_moderation("rejected")`` — the owner API has no route
    # here. PUBLISHED is the sole indexed status, so entering BLOCKED emits
    # ``listing.removed`` and the listing leaves every public read by the one
    # field that already decides visibility (no second predicate, no
    # visibility-reads-moderation_status coupling).
    ListingStatus.BLOCKED: {
        # Reinstatement after a successful appeal (moderation re-emits
        # ``moderation.completed`` with decision "approved").
        ListingStatus.PUBLISHED,
        # The owner may rework it into a draft or file it away.
        ListingStatus.DRAFT,
        ListingStatus.ARCHIVED,
    },
    ListingStatus.PAUSED: {
        ListingStatus.PUBLISHED,
        ListingStatus.ARCHIVED,
        ListingStatus.EXPIRED,
    },
    ListingStatus.EXPIRED: {
        ListingStatus.PENDING,
        ListingStatus.PUBLISHED,
        ListingStatus.ARCHIVED,
    },
    ListingStatus.SOLD: {ListingStatus.ARCHIVED, ListingStatus.PUBLISHED},
    ListingStatus.REJECTED: {ListingStatus.DRAFT, ListingStatus.ARCHIVED},
    # Restoring an archived listing is a PUBLICATION, not an unpause: it goes
    # through ``services.publish.restore_listing`` and therefore lands on
    # whichever status a fresh publish lands on — PENDING under the default
    # pre-moderation gate, PUBLISHED under ``MODERATION_GATE="post"`` or
    # ``AUTO_APPROVE_ON_PUBLISH``. Both edges are named here because both are
    # really driven.
    ListingStatus.ARCHIVED: {
        ListingStatus.DRAFT,
        ListingStatus.PENDING,
        ListingStatus.PUBLISHED,
    },
}

# The seller's half of the machine above: the edges an OWNER may drive from
# their own cabinet, keyed the same way.
#
# It is written as a subset of ``LISTING_TRANSITIONS`` (and a test asserts
# that it is one) rather than as a second table, because the two would drift
# in the one direction that hurts: a card advertising a move the route then
# refuses with a 409.
#
# The gap this closes was the whole of the owner's report. The API exposed
# ``archive`` (-> ARCHIVED) and ``complete`` (-> SOLD) and nothing else, so
# every state a seller could put a listing INTO was a state they could not
# get it out of, and ``DELETE`` was the only call left that still answered.
# Forty listings sat in exactly that position on one live stand.
#
# What is deliberately NOT here is as load-bearing as what is:
#
# * ``PENDING -> PUBLISHED`` and ``BLOCKED -> PUBLISHED`` belong to
#   moderation. They are in ``LISTING_TRANSITIONS`` because
#   ``apply_moderation`` drives them; putting them here would be a
#   self-service publish gate.
# * ``PUBLISHED -> BLOCKED`` is a takedown, which is not a thing one does to
#   oneself.
#
# EXPIRED -> PENDING is the renewal edge: it re-enters moderation rather than
# going straight back to the shop window, because a listing that has been
# sitting for a TTL is content nobody has looked at recently.
#
# ARCHIVED -> PUBLISHED is «опубликовать снова» (Д193): a seller who filed a
# listing away could only take it back to DRAFT and walk the whole composer
# again. It is routed through ``services.publish.restore_listing`` rather than
# through a bare ``transition_to``, and that is the load-bearing part —
# ARCHIVED is reachable from EVERY other status, BLOCKED and REJECTED
# included, so an edge that put the row straight back in the window would be a
# takedown-laundering path (block -> archive -> publish) around the very gate
# that keeps BLOCKED -> PUBLISHED out of this table. Going through the publish
# service means a restore is validated, promoted and re-submitted for review
# exactly like a first publication, and lands where the fleet's moderation
# policy says a publication lands.
OWNER_TRANSITIONS: dict[str, set[str]] = {
    ListingStatus.DRAFT: {ListingStatus.PENDING, ListingStatus.ARCHIVED},
    ListingStatus.PENDING: {ListingStatus.DRAFT, ListingStatus.ARCHIVED},
    ListingStatus.PUBLISHED: {
        ListingStatus.PAUSED,
        ListingStatus.SOLD,
        ListingStatus.ARCHIVED,
    },
    ListingStatus.PAUSED: {ListingStatus.PUBLISHED, ListingStatus.ARCHIVED},
    ListingStatus.EXPIRED: {ListingStatus.PENDING, ListingStatus.ARCHIVED},
    ListingStatus.SOLD: {ListingStatus.PUBLISHED, ListingStatus.ARCHIVED},
    ListingStatus.REJECTED: {ListingStatus.DRAFT, ListingStatus.ARCHIVED},
    ListingStatus.BLOCKED: {ListingStatus.DRAFT, ListingStatus.ARCHIVED},
    ListingStatus.ARCHIVED: {ListingStatus.DRAFT, ListingStatus.PUBLISHED},
}


def owner_transitions_for(status: str) -> list[str]:
    """The moves this listing's OWNER may make from *status*, sorted.

    The single answer to "what can I do with this row", read by the route
    that accepts a move and by the serializer that advertises one — so a
    storefront never has to re-derive it and can never derive it differently.
    """
    return sorted(OWNER_TRANSITIONS.get(status, set()))


# Statuses in which a listing is part of the public/search index. Entering the
# set emits ``listing.published``; leaving it emits ``listing.removed``.
INDEXED_STATUSES: frozenset[str] = frozenset({ListingStatus.PUBLISHED})

#: A category id is an opaque TOKEN — an int-like string (stapel-categories'
#: ``AutoField``) or a UUID on a deployment keyed that way. It is never a
#: PATH.
#:
#: ``stapel-search`` publishes a slash-joined ancestry for its ``?category=``
#: filter (``suggest.py``: ``"category": "/".join(path_ids)``) and the storefront
#: puts it in the URL. That is the right value in that field. Dropped into
#: ``Listing.category_id`` it is a different kind of thing wearing the same
#: type, and nothing here could tell: the column was a bare ``CharField`` with
#: no validators, the draft serializer had no ``validate_category_id``, and the
#: categories seam is only consulted at PUBLISH — so a draft carrying a path
#: was written, stored and served without one reader ever asking. Three drafts
#: on one live stand (243, 244, 245) hold ``"32/149/163"`` because of it.
#:
#: The rule is declared HERE, on the field, and not in the serializer, because
#: the serializer is one door of three: DRF inherits a model field's validators
#: into ``ListingDraftSerializer`` automatically, the admin form runs them, and
#: so does any ``full_clean()``. A serializer-only check would have left the
#: Django admin — where ``category_id`` is editable and not read-only — able to
#: type a path straight in.
#:
#: Deliberately narrow: alphanumerics, ``_`` and ``-``, starting on an
#: alphanumeric. A separator that turns out to be legitimate somewhere fails
#: loudly and is a one-line change; a path that gets in fails silently and
#: costs a repair run. If a surface ever needs a path, it needs its OWN field.
CATEGORY_ID_PATTERN = r"\A[A-Za-z0-9][A-Za-z0-9_-]*\Z"

validate_category_id = RegexValidator(
    regex=CATEGORY_ID_PATTERN,
    message=(
        "A category id is an opaque id, not a path. Got a value containing a "
        "separator — a slash-joined category PATH belongs in the search "
        "?category= filter, not in category_id."
    ),
    code="invalid_category_id",
)


#: The statuses a listing may hold with NO category (``category_id`` NULL).
#:
#: The column is nullable so the composer can open the draft row on the first
#: photo, before the category step. That is the whole of the relaxation: a
#: category-less row is a DRAFT, or an ARCHIVED draft the seller put away —
#: 0.20.0's "the seller always has a way forward" must keep working on a row
#: that never got as far as a category. Every other status still carries one,
#: and that is enforced at the two doors a row uses to leave DRAFT:
#: ``Listing.transition_to`` and ``services.publish.publish_listing`` (which
#: assigns PENDING itself). One predicate — ``has_category`` — behind both, so
#: the structured refusal the composer renders and the write path's refusal
#: cannot drift apart.
CATEGORYLESS_STATUSES: frozenset[str] = frozenset(
    {ListingStatus.DRAFT, ListingStatus.ARCHIVED}
)


def has_category(listing) -> bool:
    """Whether *listing* carries a category. NULL and ``""`` both mean no."""
    return bool(listing.category_id)


# Fields that are part of the document an indexer holds. A save that writes any
# of them on a listing that IS in an indexed status emits ``listing.updated``
# (``Listing.save``) — the event exists so a search index can re-pull, and it
# has to fire wherever the content actually moves, not only where someone
# remembered to call the emitter.
INDEXED_CONTENT_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "language",
        "category_id",
        "price",
        "currency",
        "price_base",
        "images",
        "location_id",
        "location_label",
        "geohash",
        "lat",
        "lon",
        "features",
        "features_title",
        "features_badges",
        "features_search",
    }
)


# Fields whose write can move the set of CDN media this listing claims. The
# claimed set is the UNION of ``images`` and ``images_draft`` — a photo still
# on the published side stays claimed while an edit drops it from the draft —
# and it is empty once the listing is (soft- or hard-) deleted. Changes are
# announced on the ``stapel.cdn.ref-sync`` topic (``sync_cdn_refs``) so
# stapel-cdn's orphan sweeper never reaps a claimed photo and does reap a
# dropped one.
CDN_REF_FIELDS: frozenset[str] = frozenset({"images", "images_draft", "deleted_at"})


class TransitionError(Exception):
    """Raised when a lifecycle transition is not permitted."""


class ListingQuerySet(models.QuerySet):
    """QuerySet helpers for listings."""

    def published(self):
        return self.filter(status=ListingStatus.PUBLISHED, deleted_at__isnull=True)

    def owned_by(self, user):
        return self.filter(owner=user)

    def visible_to(self, user):
        """Rows *user* may read by id.

        The public half is exactly ``INDEXED_STATUSES`` — what a search index
        holds is what a stranger may pull by id. An authenticated caller also
        sees every listing they own, whatever its status. Everything else is
        filtered out at the queryset, so a hidden row is indistinguishable
        from an absent one (the same 404, from the same code path).
        """
        visible = models.Q(status__in=INDEXED_STATUSES)
        if user is not None and getattr(user, "is_authenticated", False):
            visible |= models.Q(owner_id=user.id)
        return self.filter(visible)

    def with_favorited(self, user):
        """Annotate ``is_favorited`` for *user* (None for anonymous)."""
        from django.db.models import BooleanField, Exists, OuterRef, Value

        if not user or not getattr(user, "is_authenticated", False):
            return self.annotate(
                is_favorited=Value(None, output_field=BooleanField())
            )
        return self.annotate(
            is_favorited=Exists(
                Favorite.objects.filter(user_id=user.id, listing_id=OuterRef("pk"))
            )
        )

    def with_viewed(self, user):
        """Annotate ``viewed`` for *user* (None for anonymous).

        The same three-state shape as ``with_favorited`` and for the same
        reason: ``False`` is the claim "this viewer has not opened it", and
        an anonymous caller supports no such claim. Collapsing unknown into
        false would grey out nothing for a stranger while looking exactly
        like an answer.
        """
        from django.db.models import BooleanField, Exists, OuterRef, Value

        if not user or not getattr(user, "is_authenticated", False):
            return self.annotate(viewed=Value(None, output_field=BooleanField()))
        return self.annotate(
            viewed=Exists(
                ListingView.objects.filter(user_id=user.id, listing_id=OuterRef("pk"))
            )
        )


class ListingManager(models.Manager.from_queryset(ListingQuerySet)):
    """Manager that hides soft-deleted listings by default.

    Built from ``ListingQuerySet`` so its helpers (``published``, ``owned_by``,
    ``with_favorited``) are reachable straight off ``Listing.objects``.
    """

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)

    def with_deleted(self):
        return ListingQuerySet(self.model, using=self._db)

    def only_deleted(self):
        return self.with_deleted().filter(deleted_at__isnull=False)


class Listing(models.Model):
    """A marketplace listing with polymorphic, typed attribute values.

    Attribute values live in JSON projections built by the value-validation
    pipeline (see ``services.publish`` / ``services.features``):

    - ``features``: ordered list of DAOs (display metadata included);
    - ``features_title`` / ``features_badges``: DAOs flagged for title / badge;
    - ``features_search``: ``{slug: [values]}`` document a future
      stapel-search indexer consumes (built here, queried there).

    User-editable content lives in ``*_draft`` twins promoted to the published
    fields by :func:`stapel_listings.services.publish.publish_listing`.
    """

    objects = ListingManager()
    all_objects = models.Manager()  # includes soft-deleted

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="listings",
    )
    # Opaque category reference — NEVER a FK to stapel-categories. May hold an
    # int-like string or a UUID string; EXISTENCE is validated via the
    # categories.features comm Function, not a DB constraint. SHAPE is
    # validated here — see ``validate_category_id``.
    #
    # NULL while the seller has not chosen one yet. A composer opens the draft
    # row on the FIRST PHOTO — before any category is known — because the
    # photo analysis job is addressed by the draft id and cannot start without
    # one. A NOT NULL column made that row uncreatable, so the whole first
    # step had nowhere to persist. The category becomes mandatory at PUBLISH
    # (``services.publish``), not at CREATE — see ``CATEGORYLESS_STATUSES``.
    category_id = models.CharField(
        max_length=64,
        db_index=True,
        blank=True,
        null=True,
        default=None,
        validators=[validate_category_id],
    )

    title = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="", db_index=True)

    # Opaque currency code (e.g. "USD"); no FK to stapel-currencies.
    currency = models.CharField(max_length=8, default="USD")
    # NULL means "price not stated" — a legal, honest state for a classified
    # («Цена не указана»). The old ``default=0`` published every skipped price
    # as a public «0 ₽» (Д51/Д60). An explicit 0 is a different claim ("free")
    # and is gated by FREE_PRICE_CATEGORY_IDS in validate_draft.
    price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, default=None
    )
    price_base = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )

    # Inventory: whether "how many" applies at all — False for services
    # (a haircut, a rental hour) where a quantity is meaningless — and, when it
    # does, how many units are in stock. Defaults (True / 0) are chosen so a
    # bare ``Listing(...)`` — every call site that predates this field pair —
    # lands in the valid "countable good, zero known stock" state rather than
    # silently reclassifying as a service or inventing a positive count; see
    # ``validate_countable_stock`` and the migration for the full rationale.
    countable = models.BooleanField(default=True)
    stock_quantity = models.PositiveIntegerField(null=True, blank=True, default=0)

    # How many distinct viewers have opened this listing. NOT a request
    # counter: ``services.engagement.record_view`` collapses every open by
    # one viewer inside ``VIEW_DEDUP_WINDOW_SECONDS`` into a single
    # increment, and refuses the owner's own opens outright. So a reload
    # costs no write and a seller cannot inflate their own number — the two
    # ways this column would otherwise become a lie that still looks like a
    # metric. Denormalized rather than ``COUNT(*)`` over ``ListingView``,
    # because anonymous viewers leave no row and must still be counted.
    view_count = models.PositiveIntegerField(default=0, editable=False)

    # Opaque list of CDN image references (validated/synced by stapel-cdn).
    images = models.JSONField(blank=True, null=True, default=list)

    # Generic, optional geo fields (geo is an app-layer concern; no hard dep).
    location_id = models.CharField(max_length=64, blank=True, default="")
    location_label = models.CharField(max_length=255, blank=True, default="")
    geohash = models.CharField(max_length=12, blank=True, default="", db_index=True)
    # Plain coordinates next to the geohash (§63): nullable, promoted from the
    # draft twins on publish exactly like geohash/geohash_draft.
    lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lon = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=ListingStatus.choices,
        default=ListingStatus.DRAFT,
        db_index=True,
    )
    moderation_status = models.CharField(
        max_length=20,
        choices=ModerationStatus.choices,
        default=ModerationStatus.NOT_SUBMITTED,
    )
    moderation_note = models.TextField(blank=True, default="")

    auto_republish = models.BooleanField(default=True)

    published_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expiry_notification_sent = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # Published attribute projections.
    features = models.JSONField(blank=True, null=True, default=list)
    features_title = models.JSONField(blank=True, null=True, default=list)
    features_badges = models.JSONField(blank=True, null=True, default=list)
    features_search = models.JSONField(blank=True, null=True, default=dict)

    # Draft twins (promoted on publish).
    features_draft = models.JSONField(blank=True, null=True, default=dict)
    title_draft = models.CharField(max_length=255, blank=True, default="")
    description_draft = models.TextField(blank=True, default="")
    price_draft = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    images_draft = models.JSONField(blank=True, null=True, default=list)
    location_id_draft = models.CharField(max_length=64, blank=True, default="")
    location_label_draft = models.CharField(max_length=255, blank=True, default="")
    geohash_draft = models.CharField(max_length=12, blank=True, default="")
    lat_draft = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    lon_draft = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Opaque, owner-only metadata about the draft — NOT a `*_draft` twin: it
    # carries no listing content, has no published sibling, and is never
    # promoted or cleared by publish_listing/restore_listing (0.21.2). The
    # storefront composer's first tenant is per-field provenance
    # (`{"title": "seller", "description": "ai"}`), but this module never
    # reads or interprets a key — it stores whatever JSON object the client
    # sends and hands the same object back to the owner. Size-capped at write
    # time (see ListingDraftSerializer) so it stays a small sidecar, not an
    # alternate payload channel.
    draft_meta = models.JSONField(blank=True, null=True, default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["owner", "status"], name="listing_owner_status_idx"),
            models.Index(fields=["category_id", "status"], name="listing_cat_status_idx"),
        ]
        constraints = [
            # Backstop for validate_countable_stock() — catches bulk_create,
            # bulk_update, raw SQL and any other write that skips clean()/the
            # serializer.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        countable=True,
                        stock_quantity__isnull=False,
                        stock_quantity__gte=0,
                    )
                    | models.Q(countable=False, stock_quantity__isnull=True)
                ),
                name="listing_stock_invariant_chk",
            ),
        ]

    def __str__(self) -> str:
        return f"Listing #{self.pk} ({self.status})"

    def clean(self):
        super().clean()
        validate_countable_stock(self.countable, self.stock_quantity)

    # -- price_base ---------------------------------------------------------

    def compute_price_base(self) -> Decimal | None:
        """Compute ``price_base`` via the PRICE_BASE_CONVERTER seam.

        On converter failure store ``None`` (unknown) — never the raw price in
        the listing's own currency, which would be a plausible-but-wrong base
        value that silently corrupts base-price sort/filter. A NULL sorts
        predictably; a wrong number lies. The failure is logged.
        """
        if self.price is None:
            return None
        converter = listings_settings.PRICE_BASE_CONVERTER
        base = listings_settings.BASE_CURRENCY
        try:
            return converter(Decimal(str(self.price)), self.currency or base, base)
        except Exception:
            logger.warning(
                "price_base conversion failed for listing %s (price=%s %s); "
                "storing NULL rather than a wrong base value",
                self.pk, self.price, self.currency, exc_info=True,
            )
            return None

    # -- geohash_draft --------------------------------------------------------

    def compute_geohash_draft(self) -> str:
        """Stamp ``geohash_draft`` via the ``geo.geohash_encode`` comm Function.

        Mirrors ``compute_price_base``: a listing carrying ``lat_draft``/
        ``lon_draft`` gets a geohash computed server-side in ``save()``,
        rather than relying on a client to compute and send one (the prior
        state of this module — MODULE.md's own words: "this is how consumers
        stamp geohashes onto their own rows"; stapel-listings never actually
        called it). ``publish_listing`` promotes ``geohash_draft`` ->
        ``geohash`` exactly like ``lat_draft``/``lon_draft`` -> ``lat``/
        ``lon``, so this single call site is enough to fix both.

        No coordinates -> ``""`` (nothing to encode). stapel-geo is consumed
        by comm name only (MODULE.md "Do not import stapel_geo" — no hard
        dependency at import), so any failure to reach it — not deployed, no
        route configured, a bad reply — degrades to ``""`` rather than
        raising. stapel-search 0.2.2 made the lat/lon box authoritative for
        correctness; an empty geohash only costs the prefilter its index (a
        full box scan), never a wrong answer — so, like ``price_base``, a
        stale/wrong geohash left over from an earlier coordinate is worse
        than a blank one and is never kept on failure.
        """
        if self.lat_draft is None or self.lon_draft is None:
            return ""
        from stapel_core.comm import call
        from stapel_core.comm.exceptions import CommError

        try:
            result = call(
                "geo.geohash_encode",
                {"lat": float(self.lat_draft), "lon": float(self.lon_draft)},
            )
        except (CommError, LookupError, KeyError, TypeError, ValueError) as exc:
            logger.debug(
                "geo.geohash_encode unavailable for listing %s (lat=%s, lon=%s): %s",
                self.pk, self.lat_draft, self.lon_draft, exc.__class__.__name__,
            )
            return ""
        geohash = result.get("geohash") if isinstance(result, dict) else None
        return geohash or ""

    # Set by the paths that own their own index event (``transition_to``
    # emits published/removed itself) so one write never produces two.
    _skip_updated_emit = False

    def save(self, *args, **kwargs):
        # Keep price_base in sync unless the caller manages update_fields
        # without touching price.
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "price" in update_fields or "price_base" in update_fields:
            self.price_base = self.compute_price_base()
            if update_fields is not None and "price_base" not in update_fields:
                update_fields = list(update_fields) + ["price_base"]
                kwargs["update_fields"] = update_fields
        # Keep geohash_draft in sync unless the caller manages update_fields
        # without touching the draft coordinates (same shape as price_base
        # above).
        if self._touches(update_fields, {"lat_draft", "lon_draft", "geohash_draft"}):
            self.geohash_draft = self.compute_geohash_draft()
            if update_fields is not None and "geohash_draft" not in update_fields:
                update_fields = list(update_fields) + ["geohash_draft"]
                kwargs["update_fields"] = update_fields
        if self.status == ListingStatus.PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()

        # ``features_search`` is DERIVED from ``features`` — re-derive it on
        # every write that touches the source, not only in publish_listing()
        # (a projection rebuilt at exactly one call site is a projection that
        # goes stale everywhere else).
        if not self._skip_updated_emit and self._touches(update_fields, {"features"}):
            if self.rebuild_features_search() and update_fields is not None:
                update_fields = list(update_fields) + ["features_search"]
                kwargs["update_fields"] = update_fields

        # CDN media claims: capture the claimed set the stored row holds
        # BEFORE the write, announce the difference after it succeeds.
        touches_refs = self._touches(update_fields, CDN_REF_FIELDS)
        old_refs = (
            self._stored_cdn_image_refs()
            if touches_refs and not self._state.adding
            else set()
        )

        # Index-boundary detector (the Д50 lesson): a status write that crosses
        # INDEXED_STATUSES must emit published/removed WHEREVER it happens, not
        # only inside transition_to. `Listing.status = "archived"; save()` — an
        # orchestrator's raw write, a shell one-liner — used to change public
        # visibility while every search index kept serving the ghost. The FSM
        # remains the front door (it validates the edge and owns its emit via
        # _skip_updated_emit); this is the model refusing to let ANY save
        # route move a listing across the boundary silently. Queryset
        # ``.update(status=...)`` still bypasses the model — that hole is
        # covered by stapel-search's reconcile sweep, not by pretending it
        # cannot happen.
        boundary = None
        if (
            not self._skip_updated_emit
            and not self._state.adding
            and self._touches(update_fields, {"status"})
        ):
            boundary = self._status_boundary_crossing()
        if boundary == "published":
            # Parity with transition_to: entering the index re-derives the
            # projection so the announced document is not stale by construction.
            if self.rebuild_features_search() and update_fields is not None:
                if "features_search" not in update_fields:
                    update_fields = list(update_fields) + ["features_search"]
                    kwargs["update_fields"] = update_fields

        emit_updated = (
            not self._skip_updated_emit
            and not self._state.adding
            and self.status in INDEXED_STATUSES
            and self._indexed_content_changed(update_fields)
        )
        if not emit_updated and boundary is None:
            super().save(*args, **kwargs)
        else:
            from . import events

            # Same rule as transition_to/delete: the row and the event a
            # search index reacts to commit together or not at all.
            with mutate_and_emit():
                super().save(*args, **kwargs)
                if boundary == "published":
                    # The published payload carries the full document signal;
                    # a same-write listing.updated would be a duplicate.
                    events.emit_listing_published(self)
                elif boundary == "removed":
                    events.emit_listing_removed(self, reason=self.status)
                else:
                    events.emit_listing_updated(self)

        if touches_refs:
            self._sync_cdn_image_refs(self.pk, old_refs, self.cdn_image_refs())

    @staticmethod
    def _touches(update_fields, names: set[str] | frozenset[str]) -> bool:
        """Whether a save with *update_fields* may write any of *names*.

        ``update_fields=None`` is a full-row write — it may write anything.
        """
        return update_fields is None or bool(names & set(update_fields))

    def _status_boundary_crossing(self) -> str | None:
        """Whether this save moves ``status`` across INDEXED_STATUSES.

        Compares the instance against the stored row (one narrow SELECT, only
        on saves that may write ``status``): ``"published"`` when the write
        enters the indexed set, ``"removed"`` when it leaves it, ``None`` for
        everything else — including moves entirely inside or entirely outside
        the boundary, which an index does not care about.
        """
        stored = (
            type(self)
            .all_objects.filter(pk=self.pk)
            .values_list("status", flat=True)
            .first()
        )
        if stored is None or stored == self.status:
            return None
        was_indexed = stored in INDEXED_STATUSES
        now_indexed = self.status in INDEXED_STATUSES
        if was_indexed and not now_indexed:
            return "removed"
        if now_indexed and not was_indexed:
            return "published"
        return None

    def _indexed_content_changed(self, update_fields) -> bool:
        """Whether this save actually moves a field an index holds.

        Compares against the stored row rather than trusting ``update_fields``:
        a full save (``update_fields=None``) is the normal shape of a
        *draft* write — the API's save-draft on a live listing goes through it
        — and announcing a content change for a draft keystroke would be a
        lie the indexer pays for. Costs one extra SELECT, and only on a save
        that could plausibly change the document: an indexed listing whose
        write reaches at least one indexed field.

        Values are normalised through each field's ``to_python`` before the
        comparison so a ``Decimal`` from the database and the equivalent
        string an API write leaves on the instance are not read as a change.
        """
        candidates = INDEXED_CONTENT_FIELDS
        if update_fields is not None:
            candidates = INDEXED_CONTENT_FIELDS & set(update_fields)
        if not candidates:
            return False

        stored = (
            type(self)
            .all_objects.filter(pk=self.pk)
            .values(*sorted(candidates))
            .first()
        )
        if stored is None:  # first write of this row — nothing to diverge from
            return False

        for name in candidates:
            field = self._meta.get_field(name)
            if field.to_python(stored[name]) != field.to_python(getattr(self, name)):
                return True
        return False

    # -- CDN media claims ---------------------------------------------------

    @staticmethod
    def _cdn_refs_of(images, images_draft, deleted_at) -> set[str]:
        """The CDN references a row with these values claims.

        ``images`` / ``images_draft`` hold opaque ``<type>/<hash>`` strings —
        exactly the ref form stapel-cdn's ``apply_ref_sync`` resolves — stored
        verbatim from ``@stapel/cdn-react``'s upload bag, so the stored value
        IS the ref: no re-derivation, only non-string/empty junk is skipped.
        The claim is the union of both sides (a photo dropped from the draft
        but still published stays claimed), and a deleted listing claims
        nothing.
        """
        if deleted_at is not None:
            return set()
        return {
            item
            for source in (images or [], images_draft or [])
            for item in source
            if isinstance(item, str) and item
        }

    def cdn_image_refs(self) -> set[str]:
        """The ``<type>/<hash>`` references this listing currently claims."""
        return self._cdn_refs_of(self.images, self.images_draft, self.deleted_at)

    def _stored_cdn_image_refs(self) -> set[str]:
        """The claimed set as the DATABASE row stands (pre-write baseline)."""
        stored = (
            type(self)
            .all_objects.filter(pk=self.pk)
            .values("images", "images_draft", "deleted_at")
            .first()
        )
        if stored is None:
            return set()
        return self._cdn_refs_of(
            stored["images"], stored["images_draft"], stored["deleted_at"]
        )

    @staticmethod
    def _sync_cdn_image_refs(entity_id, old_refs: set[str], new_refs: set[str]) -> None:
        """Announce a claim change to stapel-cdn; never blocks the write.

        Same graceful discipline as ``compute_geohash_draft`` (0.7.1): the
        helper already degrades a failed bus publish to ``ok=False`` and a
        warning (Kafka replays when CDN catches up); anything it raises anyway
        is logged here, because a listing write must never fail over media
        bookkeeping. Fired after the row write, mirroring stapel-profiles'
        avatar sync.
        """
        if old_refs == new_refs:
            return
        try:
            from stapel_core.django.cdn.ref_sync import sync_cdn_refs

            sync_cdn_refs(
                "listings", "listing", entity_id, sorted(old_refs), sorted(new_refs)
            )
        except Exception:
            logger.warning(
                "CDN ref sync failed for listing %s", entity_id, exc_info=True
            )

    def rebuild_features_search(self) -> bool:
        """Re-derive ``features_search`` from ``features``; True if it moved.

        Does not save — callers fold the field into their own write.
        """
        from .services.features import build_features_search_from_list

        rebuilt = build_features_search_from_list(self.features)
        if rebuilt == (self.features_search or {}):
            return False
        self.features_search = rebuilt
        return True

    # -- lifecycle state machine -------------------------------------------

    def can_transition_to(self, new_status: str) -> bool:
        if new_status == self.status:
            return True
        return new_status in LISTING_TRANSITIONS.get(self.status, set())

    def transition_to(self, new_status: str, *, save: bool = True) -> None:
        """Move the lifecycle to *new_status*, emitting search events.

        Emits ``listing.published`` when entering an indexed status and
        ``listing.removed`` when leaving one, so a future stapel-search
        indexer stays in sync without this module knowing it exists.

        Entering an indexed status re-derives ``features_search`` first: a
        PAUSED -> PUBLISHED republish used to re-announce the projection built
        at the last publish, so an index fed by that event was stale by
        construction.

        The status write and the outbox emit share one
        ``stapel_core.comm.mutate_and_emit()`` block: they commit together or
        roll back together. Without it a crash (or emit failure) between the
        save and the emit would leave a published-but-unindexed listing
        forever — the whole point of the transactional outbox is that the row
        and its event never disagree.
        """
        if new_status == self.status:
            return
        if not self.can_transition_to(new_status):
            raise TransitionError(
                f"cannot move listing {self.pk} from {self.status} to {new_status}"
            )
        # The NOT NULL the column no longer carries, kept where it is actually
        # needed: a row may be category-less only while it is a draft (or an
        # archived one). Nothing reaches moderation or the index without a
        # category, whatever route it took to get here.
        if new_status not in CATEGORYLESS_STATUSES and not has_category(self):
            raise TransitionError(
                f"cannot move listing {self.pk} to {new_status} without a category"
            )
        from . import events

        old_status = self.status
        was_indexed = old_status in INDEXED_STATUSES
        now_indexed = new_status in INDEXED_STATUSES

        with mutate_and_emit():
            self.status = new_status
            if new_status == ListingStatus.PUBLISHED and self.published_at is None:
                self.published_at = timezone.now()
            fields = ["status", "published_at", "updated_at"]
            if now_indexed:
                self.rebuild_features_search()
                fields.append("features_search")
            if save:
                # This method owns the index event for this write; suppress
                # save()'s own listing.updated so one transition is one event.
                self._skip_updated_emit = True
                try:
                    self.save(update_fields=fields)
                finally:
                    self._skip_updated_emit = False
            if now_indexed and not was_indexed:
                events.emit_listing_published(self)
            elif was_indexed and not now_indexed:
                events.emit_listing_removed(self, reason=new_status)

    def apply_moderation(
        self, decision: str, *, note: str = "", auto_publish: bool = True
    ) -> None:
        """Apply a moderation *decision* to this listing.

        ``approved`` -> moderation APPROVED and (if ``auto_publish``) the
        lifecycle moves PENDING->PUBLISHED, or BLOCKED->PUBLISHED when a
        takedown is reversed on appeal; ``rejected`` -> moderation REJECTED
        plus the lifecycle move that expresses the verdict — PENDING->REJECTED
        before publication, PUBLISHED->BLOCKED for a takedown of a live
        listing; ``needs_review`` -> moderation NEEDS_REVIEW, lifecycle
        unchanged; ``dismissed`` -> nothing changes (the verdict is about a
        report, not about this content).

        Every lifecycle move goes through :meth:`transition_to`, so the index
        events are emitted by the one place that owns them. A takedown that
        assigned ``status`` directly (as this method used to) left a listing
        out of the public reads with the search index still serving it.
        """
        if decision not in ("approved", "rejected", "needs_review", "dismissed"):
            raise ValueError(f"unknown moderation decision: {decision!r}")

        # The moderation write and any resulting lifecycle transition (which
        # itself saves + emits) commit as one unit — an approval must not leave
        # moderation_status APPROVED without the listing.published event that a
        # search indexer needs, nor vice versa.
        with transaction.atomic():
            if decision == "dismissed":
                return
            if decision == "approved":
                self.moderation_status = ModerationStatus.APPROVED
                self.moderation_note = note or ""
                self.save(
                    update_fields=["moderation_status", "moderation_note", "updated_at"]
                )
                if auto_publish and self.status in (
                    ListingStatus.PENDING,
                    ListingStatus.BLOCKED,
                ):
                    self.transition_to(ListingStatus.PUBLISHED)
            elif decision == "rejected":
                self.moderation_status = ModerationStatus.REJECTED
                self.moderation_note = note or "Content policy violation"
                self.save(
                    update_fields=["moderation_status", "moderation_note", "updated_at"]
                )
                if self.status == ListingStatus.PENDING:
                    self.transition_to(ListingStatus.REJECTED)
                elif self.status in INDEXED_STATUSES:
                    # Takedown of a live listing: leaves the indexed set, so
                    # transition_to emits listing.removed.
                    self.transition_to(ListingStatus.BLOCKED)
            else:  # needs_review
                self.moderation_status = ModerationStatus.NEEDS_REVIEW
                self.moderation_note = note or "Flagged for manual review"
                self.save(
                    update_fields=["moderation_status", "moderation_note", "updated_at"]
                )

    # -- soft delete --------------------------------------------------------

    def delete(self, using=None, keep_parents=False):
        """Soft delete; emits ``listing.removed`` if it was indexed.

        Soft-delete write and the removal emit share one
        ``mutate_and_emit()`` transaction (see ``transition_to``) so a deleted
        listing is never left in a search index.
        """
        from . import events

        was_indexed = self.status in INDEXED_STATUSES
        with mutate_and_emit():
            self.deleted_at = timezone.now()
            self.save(update_fields=["deleted_at", "updated_at"])
            if was_indexed:
                events.emit_listing_removed(self, reason="deleted")

    def hard_delete(self, using=None, keep_parents=False):
        """Physically delete the row, releasing every CDN media claim.

        Bypasses ``save()``, so it announces the release itself. A row that
        was already soft-deleted claims nothing (``cdn_image_refs`` is empty),
        so this is a no-op for it — the soft delete released the refs.
        """
        entity_id = self.pk
        old_refs = self.cdn_image_refs()
        super().delete(using=using, keep_parents=keep_parents)
        self._sync_cdn_image_refs(entity_id, old_refs, set())

    def restore(self):
        """Undo a soft delete; emits ``listing.published`` if the row is in an
        indexed status — delete() announced the leave, the way back in must be
        announced too or the index stays a ghost in the other direction."""
        from . import events

        with mutate_and_emit():
            self.deleted_at = None
            self.save(update_fields=["deleted_at", "updated_at"])
            if self.status in INDEXED_STATUSES:
                events.emit_listing_published(self)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and timezone.now() > self.expires_at

    @property
    def is_active(self) -> bool:
        return (
            not self.is_deleted
            and self.status == ListingStatus.PUBLISHED
            and not self.is_expired
        )


class Favorite(models.Model):
    """A user's favorite (first-class engagement, replacing the stats caches)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="listing_favorites",
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="favorites"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "listing"], name="uniq_user_listing_fav"
            )
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="fav_user_created_idx"),
        ]

    def __str__(self) -> str:
        return f"User {self.user_id} ♥ Listing {self.listing_id}"


class ListingView(models.Model):
    """One (viewer, listing) pair an AUTHENTICATED viewer has opened.

    Two jobs, and it refuses a third.

    It answers «have I seen this» — the grey «просмотрено» on a card — which
    is a question only a signed-in viewer can have asked. And it carries
    ``last_seen_at``, so «recently viewed» is a read of this table rather
    than a second one.

    What it is NOT is the view COUNTER. Anonymous viewers are the majority of
    a classified's traffic and leave no row here on purpose: a row per
    (session, listing) grows with traffic and answers nothing, which is
    exactly the shape of the legacy ``UserAdView`` read-cache this module
    dropped. The count lives on ``Listing.view_count`` and covers everyone.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="listing_views",
    )
    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="viewers"
    )
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "listing"], name="uniq_user_listing_view"
            )
        ]
        indexes = [
            models.Index(fields=["user", "-last_seen_at"], name="view_user_seen_idx"),
        ]

    def __str__(self) -> str:
        return f"User {self.user_id} saw Listing {self.listing_id}"
