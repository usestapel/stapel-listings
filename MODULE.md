# stapel-listings — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- **`Listing`** — the marketplace/catalog vertical core (generalizes the legacy
  catalog's `Ad`): an owner (`AUTH_USER_MODEL`), an **opaque `category_id`** (no FK), an
  **opaque `currency`** code, title/description, price + `price_base`, four
  typed-attribute JSON projections (`features` / `features_title` /
  `features_badges` / `features_search`), soft-delete, generic optional geo
  fields, an inventory pair (`countable` / `stock_quantity`, below), and draft
  twins promoted on publish.
- **Inventory (`countable` / `stock_quantity`)**: a listing may be a physical
  good (`countable=True`, the default — needs a non-negative
  `stock_quantity`) or a service (`countable=False` — a haircut, a rental
  hour; a quantity doesn't apply, so `stock_quantity` must be `NULL`). The
  invariant is enforced three times — `Listing.clean()` /
  `validate_countable_stock()` (the source of truth), the DB
  `listing_stock_invariant_chk` `CheckConstraint` (backstop for writes that
  skip `clean()`), and `ListingDraftSerializer.validate()` (the API's `400`) —
  and is unit-editable directly (no `_draft` twin, same treatment as
  `auto_republish`): stock changes don't need a republish/re-moderation cycle.
  Existing rows from before this field pair backfill as `countable=True,
  stock_quantity=0` (see CHANGELOG `[0.3.0]` migration notes for the
  rationale). **Not** part of the `listing.*` event payloads or any filter —
  see the boundaries below.
- **Two state machines**: the listing lifecycle
  (draft→pending→published→{paused,expired,sold,archived,rejected}) with
  guarded transitions, and an independent moderation status
  (pending/approved/rejected/needs_review).
- **A value-validation pipeline** that fetches the category's feature schema
  over comm (`categories.features`) and delegates every check / DTO→DAO
  conversion to **stapel-attributes** — no attribute engine is re-implemented
  here.
- **Publish service** (draft→pending, projections built, moderation requested)
  and **favorites** as first-class engagement (the `UserAdLike`/`UserAdView`
  external-stats read-caches are dropped).
- A comm surface (below) that emits index/moderation events and consumes
  category/moderation/GDPR events, plus a `listings.status` Function and a
  GDPR provider.

## What this module deliberately does NOT do (boundaries)

- **Search / filtering** is a separate module (**stapel-search**, not built).
  This module BUILDS the `features_search` projection and emits
  `listing.published` / `listing.updated` / `listing.removed` for a future
  indexer, but exposes **no** search or filter endpoints.
- **Moderation** (LLM pipeline, notice-and-action, auto-approve policy) is a
  separate module (**stapel-moderation**, not built). This module emits
  `listing.submitted` and consumes `moderation.completed`; it only applies a
  verdict, it does not decide one. (`AUTO_APPROVE_ON_PUBLISH` is a
  minimal-deployment escape hatch, not a moderation policy.)
- **Category schema** lives in **stapel-categories**; this module never imports
  it — it calls the `categories.features` comm Function and caches by revision.
  The cache uses a revision-versioned data key plus a pointer key advanced from
  the `category.changed` event's revision (`category_schema.note_changed`), so a
  `category.changed` arriving mid-fetch can't re-cache a stale schema under the
  live key. **Unknown-slug policy**: `validate_draft` rejects any draft feature
  whose slug is not in the category schema (per-feature `validation_failed`,
  key `error.400.listing_feature_not_allowed`), so `validate-draft` and
  `publish` agree — a draft carrying a feature removed from the category after
  it was written fails validation with actionable detail rather than an opaque
  publish `400`.
- **Currency conversion** lives in a currencies module; `price_base` is
  computed through the `PRICE_BASE_CONVERTER` seam (identity by default).

## Extension points (fork-free)

### Settings — `STAPEL_LISTINGS` namespace (`conf.py`)

Resolution order per key: `settings.STAPEL_LISTINGS[key]` -> flat Django setting
of the same name -> environment variable -> default. Read lazily at call time.

| Key | Default | What it customizes |
|---|---|---|
| `CATEGORY_FEATURES_FUNCTION` | `"categories.features"` | Name of the comm Function resolving a category's feature schema (REPLACE — single provider). |
| `FEATURE_CONFIG_CACHE_TIMEOUT` | `300` | Seconds a resolved feature-config list is memoized. |
| `BASE_CURRENCY` | `"USD"` | Currency code `price_base` is expressed in. |
| `PRICE_BASE_CONVERTER` | `stapel_listings.services.pricing.identity_converter` | Dotted path `(amount, currency, base) -> Decimal` (REPLACE — single strategy). Default is identity; wire to a currencies backend. |
| `AUTO_APPROVE_ON_PUBLISH` | `False` | Approve+publish immediately instead of waiting for `moderation.completed` (deployments with no moderation module). |
| `REQUIRE_IMAGE_ON_PUBLISH` | `True` | Whether ≥1 image is required to publish. |
| `DESCRIPTION_MIN_LENGTH` / `DESCRIPTION_MAX_LENGTH` | `4` / `500` | Description length bounds enforced on validate/publish. |
| `DEFAULT_LISTING_TTL_DAYS` | `30` | Days until a freshly published listing expires (`None` disables). |

`PRICE_BASE_CONVERTER` and `CATEGORY_FEATURES_FUNCTION` are **single-strategy
REPLACE** keys. This module ships no open (merge-semantics) registry of its own;
the interchangeable set it depends on — attribute *types* — is an open,
merge-over-builtins registry owned by **stapel-attributes** (`register_feature_type`).

### Serializer seams (`views.py`)

`ListingViewSet` resolves its serializer per action from overridable class
attributes; subclass and remount the router to swap any of them.

| Action(s) | Attribute | Default |
|---|---|---|
| `retrieve` / detail | `detail_serializer_class` | `ListingDetailSerializer` |
| `list` | `card_serializer_class` | `ListingCardSerializer` |
| `create` / `update` / `save-draft` | `draft_serializer_class` | `ListingDraftSerializer` |

### comm surface

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Function (provides) | `listings.status` | `{listing_id}` -> `{listing_id, owner_id, status, moderation_status, is_active, is_deleted}` | `schemas/functions/listings.status.json` |
| Emit (Action) | `listing.submitted` | `{listing_id, owner_id, category_id, title, description, language}` | `schemas/emits/listing.submitted.json` — **moderation boundary** |
| Emit (Action) | `listing.published` | `{listing_id, owner_id, category_id, status, features_search}` | `schemas/emits/listing.published.json` — **search boundary** |
| Emit (Action) | `listing.updated` | same as published | `schemas/emits/listing.updated.json` — **search boundary** |
| Emit (Action) | `listing.removed` | `{listing_id, owner_id, category_id, status, reason}` | `schemas/emits/listing.removed.json` — **search boundary** |
| Consume (Action) | `category.changed` | `{category_id, revision}` | `schemas/consumes/category.changed.json` (owned by stapel-categories) |
| Consume (Action) | `moderation.completed` | `{listing_id, decision, note?}` | `schemas/consumes/moderation.completed.json` (owned by stapel-moderation) |
| Consume (Action) | `user.deleted` | `{user_id, …}` | `schemas/consumes/user.deleted.json` (owned by stapel-auth/gdpr) |
| Call (depends on) | `categories.features` | `{category_id}` | provided by stapel-categories |

### GDPR

`ListingsGDPRProvider` (section `listings`) is registered in `apps.ready()` and
also driven by the `user.deleted` subscription. `export` returns the user's
listings + favorites; `delete`/`anonymize` erase them (emitting
`listing.removed` for indexed rows so a search backend drops them too).

### Admin categories — `@access` declarations (admin-suite AS-5)

Every model in `models.py` carries (or implicitly defaults to) a
`stapel_core.access.access` category — one declaration, consumed by admin
visibility, default staff rights, and the audit report (admin-suite §0).
Undecorated = `business` (visible, staff-manageable) and is the correct,
zero-effort default for domain tables.

Both models here are `business` and stay undecorated — neither fits `ops`
(outbox/dedup/audit-log/TTL-junk machinery) or `secret` (token/key/credential
carriers):

- `Listing` — the module's core domain table; the admin-suite doc's own
  verbatim `business` example. It holds no secrets (`category_id`, `currency`,
  `images`, geo fields are opaque references, not credentials) and no
  service-machinery fields (the state machines are first-class business
  state staff moderate through, not an internal delivery/audit log).
- `Favorite` — a first-class user engagement record (the module's own docs
  frame it as replacing the old `UserAdLike`/`UserAdView` stats caches), not a
  dedup/idempotency record or TTL-expiring junk: it is durable, user-visible
  state ("my favorites") staff may need to inspect for abuse/dispute
  handling, unbounded by any TTL/expiry field.

No decorator changes were made and `admin.py` (`ListingAdmin`,
`FavoriteAdmin`) is untouched — there is no ops/secret model here to route
through `StapelModelAdmin`.

## Anti-patterns

- **Don't fork to change behavior** — every knob above is a seam; a change
  impossible without editing this package is an upstream bug.
- **Don't import other stapel modules** — no `import stapel_categories`,
  `stapel_moderation`, `stapel_search`, `stapel_currencies`. Talk over comm by
  string name.
- **Don't FK across a service boundary** — `category_id`, `currency`, image
  refs and geo ids are opaque; keep them that way.
- **Don't add search/filter endpoints here** — that is stapel-search; feed it
  via the `listing.*` events.
- **Don't run moderation logic here** — emit `listing.submitted`, consume
  `moderation.completed`.
- **Don't bypass the settings namespace** with import-time `os.getenv`, and
  don't skip `transition_to` (it emits the index events).
- **Don't emit outside the mutation's transaction, and never swallow an emit
  failure** — every `listing.*` event must commit atomically with the row it
  describes. Wrap mutation+emit in `stapel_core.comm.mutate_and_emit()` (used
  by `transition_to`, `delete`, the publish service and the GDPR provider);
  CI gates this with `python -m stapel_core.lint.emit_check .`.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam above: a
settings key, a `PRICE_BASE_CONVERTER`, a serializer subclass + router remount,
a comm subscriber, or registering a custom attribute *type* upstream in
stapel-attributes.

**Upstream contribution** if it needs new `Listing` fields/migrations, new
endpoints, a new settings key or seam, or changes a committed schema.

Litmus test: if you'd have to monkeypatch or edit code inside
`stapel_listings/` — it's upstream. If a setting, subclass, receiver or comm
call gets you there — it's app-layer.
