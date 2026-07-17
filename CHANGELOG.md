# Changelog

All notable changes to stapel-listings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.3.4] - 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.3.3] - 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules). Suite green as-is.

## [0.3.0] - 2026-07-08

New feature (minor bump): a listing now records whether a quantity applies at
all, and if so, how many units are in stock.

### Added
- **`Listing.countable` (`BooleanField`, default `True`) and
  `Listing.stock_quantity` (`PositiveIntegerField`, nullable, default `0`).**
  Ported gap vs. the legacy catalog's `ads.Ad` (neither has an inventory count) and vs. the
  marketplace domain generally: a listing may be a physical good (countable,
  needs a quantity) or a service (a haircut, a rental hour — "how many"
  doesn't apply).
- **Invariant enforced at three layers**, not just documented:
  - `stapel_listings.models.validate_countable_stock()` / `Listing.clean()` —
    `countable=True` requires a non-negative `stock_quantity`; `countable=False`
    requires it to be `NULL`.
  - DB `CheckConstraint` `listing_stock_invariant_chk` — the storage-level
    backstop for writes that skip `clean()` (bulk operations, raw SQL).
  - `ListingDraftSerializer.validate()` — the API-facing `400`. Cross-field:
    on a partial `save-draft` PATCH, whichever side of the pair is omitted
    falls back to the *current instance's* value, so switching `countable`
    requires sending `stock_quantity` explicitly (`null` to clear it, or a
    non-negative int) in the same request — a bare `{"countable": false}`
    against a listing whose `stock_quantity` is still `0` is rejected, by
    design (no silent guessing about the caller's intent).
  - No new `error.<status>.*` key was registered: both the model-level and
    serializer-level messages are plain strings, matching the existing
    `validate_price_draft` ("Price must be >= 0.") precedent in this same
    file rather than introducing a bespoke code for a single cross-field
    check — `stapel_core`'s exception handler already classifies these under
    the generic `error.400.field.invalid` / `error.400.validation_error`
    fallback.
- `countable` / `stock_quantity` added to `ListingDraftSerializer` (writable —
  no `_draft` twin: unlike title/price/images, stock is operational and
  adjustable any time without a republish/re-moderation cycle, the same
  treatment `auto_republish` already gets), `ListingCardSerializer` and
  `ListingDetailSerializer` (read), and `ListingAdmin.list_display` /
  `list_filter`.

### Migration notes
- **Backfill: existing rows become `countable=True`, `stock_quantity=0`.**
  Neither can be inferred from a category's schema without a live
  `categories.features` call per row (out of scope for a schema migration,
  and category-level "is this a service category" isn't a concept this
  module owns anyway — see MODULE.md). `countable=True` matches every
  listing's implicit prior semantics (a quantity was simply never asked);
  `stock_quantity=0` — rather than inventing a positive count or leaving it
  `NULL` (which would violate the invariant for a `countable=True` row) — is
  the conservative choice: a pre-existing listing looks "out of stock" until
  its owner explicitly sets a count, instead of silently claiming unlimited
  or unknown-but-available stock. Hosts that know some of their existing
  categories are service-only can follow up with an app-layer data migration
  flipping those rows to `countable=False, stock_quantity=NULL`.
- No `AddConstraint` failure risk: the field-add operations (with their
  defaults) run before the `CheckConstraint` is added in the same migration,
  so every pre-existing row already satisfies the invariant by the time the
  constraint is created. Verified against a simulated pre-0.3.0 table with a
  hand-inserted row (no `countable`/`stock_quantity` columns yet).
- **Not part of the `listing.submitted` / `listing.published` / `listing.updated`
  / `listing.removed` event payloads.** Those already omit price/title/images —
  they carry identity + status + the `features_search` projection only,
  intentionally minimal for the future stapel-search indexer. Adding the two
  new fields there would be a scope creep beyond "add the field"; a consumer
  that needs stock can call `listings.status` (once extended) or a future
  dedicated Function.
- **No new filter/search endpoint.** `ListingViewSet` has no query-param
  filtering surface today (not even by price or category) — search/filter is
  explicitly stapel-search's job per MODULE.md. An `in_stock` filter would be
  the first filter added to this module and isn't "consistent with existing
  filters" because there are none; skipped.

### Changed
- **Default currency is now USD, not EUR** — `Listing.currency`'s model
  default and `STAPEL_LISTINGS["BASE_CURRENCY"]` both default to `"USD"`.
  Both remain fully overridable via settings or env, as before; only the
  default value changed. Includes the migration for the field default.

## [0.2.1] — unreleased

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- Pinned `stapel-attributes` to the `>=0.3,<0.4` window (was `>=0.1,<0.2` —
  a stale sibling pin predating attributes 0.3.x; same §7.1 rule).

- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_listings.tests`
  subpackage is no longer listed in `[tool.setuptools] packages`). Added
  `[project.urls]`, completed the trove classifiers (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section (single source shared with the git hooks/CI).


## [0.2.0] — unreleased

Internal code-review fixes to draft validation and the category-schema cache.
Observable behaviour changes (validate-draft now rejects unknown slugs) →
minor bump.

### Fixed
- **`validate-draft` and `publish` now agree on unknown feature slugs (M-7).**
  A draft holding a feature since removed from the category schema used to
  validate clean (structured validation silently ignores unknown slugs) yet
  fail `publish` with an opaque `ERR_400_PUBLISH_VALIDATION_FAILED`.
  `validate_draft` now flags each unknown slug as `VALIDATION_FAILED` with
  per-feature detail (`error.400.listing_feature_not_allowed`), so the publish
  view returns the structured `400` result and the user can see exactly which
  feature to remove.
- **Feature-config cache closes the read-then-set race (M-6).** Configs are now
  stored under a revision-versioned key with a separate pointer key naming the
  current revision, advanced from the `category.changed` event's revision. A
  `category.changed` arriving mid-fetch only advances the pointer, so a
  concurrent fetch can no longer re-cache a stale schema under the live key
  (previously stale until the 300 s TTL). `categories.features`' revision is
  used to key the entry.

### Added
- Error key `error.400.listing_feature_not_allowed` (owned here transitionally;
  see the follow-up note below).
- `category_schema.note_changed(category_id, revision)` — advances the cache
  pointer from a `category.changed` event.

### Migration notes
- `validate-draft`/`publish` now **reject** unknown feature slugs instead of
  ignoring them. Clients that submitted stray slugs (relying on silent drop)
  will now see a `400` with a per-feature `validation_failed` entry. Strip
  removed features from the draft before publishing.

### Follow-up (not done here — out of scope)
- The unknown-slug localizable key lives in stapel-listings because
  stapel-attributes has no `NOT_ALLOWED` `ValidationErrorCode`. A cleaner
  convergence is a new attributes error code owned by the engine (and reused by
  categories' `validate-dto`); tracked separately since stapel-attributes was
  out of scope for this change.

## [0.1.1] — unreleased

### Changed
- **Outbox atomicity now goes through the framework seam.** `transition_to`,
  the soft-delete `delete`, the publish service and the GDPR provider use
  `stapel_core.comm.mutate_and_emit()` (stapel-core >= 0.3.3) instead of raw
  `transaction.atomic()` around mutation+emit — same transaction semantics
  plus core's swallow-proofing (a failed emit marks the transaction
  rollback-only). Core pin bumped to `>=0.3.3,<0.4`.
- **GDPR `delete` erasure is now atomic with its events.** It previously ran
  per-listing `listing.removed` emits and hard-deletes without any shared
  transaction (the L2 bug shape — found by the new `emit-check` gate); a
  crash mid-erasure could leave rows deleted with no event, or events for
  rows that never went away. The whole erasure is now one transaction.
- CI and the git hooks run the `emit-check` static gate
  (`python -m stapel_core.lint.emit_check .`) next to ruff.

## [0.1.0] — unreleased

Initial port of the legacy catalog's `ads` app into a Stapel L2 module — the
marketplace/catalog vertical core.

### Added
- **`Listing`** model (generalizes the legacy catalog's `Ad`): owner (`AUTH_USER_MODEL`),
  opaque `category_id`, opaque `currency` code, title/description, price +
  `price_base`, the four typed-attribute JSON projections (`features`,
  `features_title`, `features_badges`, `features_search`), soft-delete, generic
  optional geo fields, and draft twins promoted on publish.
- **Two state machines**: guarded listing lifecycle
  (draft→pending→published→{paused,expired,sold,archived,rejected}) and an
  independent moderation status; `transition_to` / `apply_moderation`.
- **Value-validation pipeline** delegating to stapel-attributes, fed the
  category's feature configs fetched over the `categories.features` comm
  Function (cached by revision, invalidated by `category.changed`).
- **Publish service**: validate draft → build projections → promote draft
  fields → request moderation.
- **Favorites** as first-class engagement (`Favorite` model + `with_favorited`
  annotation).
- comm surface: emits `listing.submitted` / `listing.published` /
  `listing.updated` / `listing.removed`; consumes `category.changed` /
  `moderation.completed` / `user.deleted`; provides the `listings.status`
  Function — all with JSON schemas in `schemas/`.
- **GDPR** provider (section `listings`) + `user.deleted` consumer.

### Provenance & decoupling (vs. the legacy catalog `ads`)
- **Category is opaque**: stores `category_id`, never FKs stapel-categories;
  gets feature configs via the `categories.features` comm Function; subscribes
  to `category.changed` for cache invalidation.
- **Currency is opaque**: `price_base` computed via the `PRICE_BASE_CONVERTER`
  seam (identity default), not a FK to a currencies module.
- **Search is a separate module** (stapel-search, not built): this module
  builds `features_search` and emits `listing.*` index events but implements no
  search/filter endpoints.
- **Moderation is a separate module** (stapel-moderation, not built): emits
  `listing.submitted`, consumes `moderation.completed`; the LLM pipeline,
  Celery tasks and Kafka publisher are dropped.
- Dropped the `UserAdLike`/`UserAdView` external-stats read-caches (favorites
  are now first-class) and the CDN/geo/agent HTTP clients.

### Fixed (source smells, per docs/catalog-split.md)
- The ~150-line hand-rolled per-field validation in the `save-draft` view is
  replaced by declarative DRF validation (`ListingDraftSerializer`).
- `_get_feature_slug` / `_build_feature_lookup`, duplicated across three source
  files, are imported from stapel-attributes.
- Regex-parsing of `ValidationError` message strings is gone — structured
  machine error codes come from stapel-attributes.
- The "auto-approve after N failed moderation retries" availability-over-safety
  behavior is not ported; moderation policy lives in stapel-moderation.

### Fixed (adversarial review)
- **Atomic status-change + emit.** `transition_to`, `apply_moderation`, the
  soft-delete `delete`, and the publish service now wrap each status mutation
  and its outbox emit in a single `transaction.atomic()` — they commit together
  or roll back together. Previously the save committed before the emit, so a
  crash between them could leave a published-but-unindexed listing (or a PENDING
  listing with no `listing.submitted` event). Added rollback tests asserting a
  failing emit reverts the status change.
- **No silently-wrong `price_base`.** A failing `PRICE_BASE_CONVERTER` now
  stores `NULL` (unknown) and logs a warning, instead of degrading to the raw
  price treated as the base currency — a plausible-but-wrong value that
  corrupted base-price sort/filter. Added a test asserting NULL on failure.

> **Not released.** Opus-authored; per the no-Fable protocol this package must
> not be tagged or published until an independent adversarial review and a PyPI
> pending trusted-publisher registration are in place.

[0.1.0]: https://github.com/usestapel/stapel-listings/releases/tag/v0.1.0
