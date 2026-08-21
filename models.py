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
    """Content-moderation state machine (independent of the lifecycle)."""

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
    ListingStatus.ARCHIVED: {ListingStatus.DRAFT},
}

# Statuses in which a listing is part of the public/search index. Entering the
# set emits ``listing.published``; leaving it emits ``listing.removed``.
INDEXED_STATUSES: frozenset[str] = frozenset({ListingStatus.PUBLISHED})

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


class TransitionError(Exception):
    """Raised when a lifecycle transition is not permitted."""


class ListingQuerySet(models.QuerySet):
    """QuerySet helpers for listings."""

    def published(self):
        return self.filter(status=ListingStatus.PUBLISHED, deleted_at__isnull=True)

    def owned_by(self, user):
        return self.filter(owner=user)

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
    # int-like string or a UUID string; validated via the categories.features
    # comm Function, not a DB constraint.
    category_id = models.CharField(max_length=64, db_index=True)

    title = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="", db_index=True)

    # Opaque currency code (e.g. "USD"); no FK to stapel-currencies.
    currency = models.CharField(max_length=8, default="USD")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
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
        default=ModerationStatus.PENDING,
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

        emit_updated = (
            not self._skip_updated_emit
            and not self._state.adding
            and self.status in INDEXED_STATUSES
            and self._indexed_content_changed(update_fields)
        )
        if not emit_updated:
            super().save(*args, **kwargs)
            return

        from . import events

        # Same rule as transition_to/delete: the row and the event a search
        # index reacts to commit together or not at all.
        with mutate_and_emit():
            super().save(*args, **kwargs)
            events.emit_listing_updated(self)

    @staticmethod
    def _touches(update_fields, names: set[str] | frozenset[str]) -> bool:
        """Whether a save with *update_fields* may write any of *names*.

        ``update_fields=None`` is a full-row write — it may write anything.
        """
        return update_fields is None or bool(names & set(update_fields))

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
        super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at", "updated_at"])

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
