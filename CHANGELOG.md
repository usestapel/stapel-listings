# Changelog

All notable changes to stapel-listings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.1.0] — unreleased

Initial port of legacy-catalog's `ads` app into a Stapel L2 module — the
marketplace/catalog vertical core.

### Added
- **`Listing`** model (generalizes legacy's `Ad`): owner (`AUTH_USER_MODEL`),
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

### Provenance & decoupling (vs legacy-catalog `ads`)
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
