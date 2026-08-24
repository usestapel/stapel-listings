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
  (draft→pending→published→{paused,expired,sold,blocked,archived,rejected})
  with guarded transitions, and an independent moderation status
  (pending/approved/rejected/needs_review). `blocked` is the moderation
  **takedown** of a live listing: reachable only from `published`, only
  through `apply_moderation("rejected")`, and — because `published` is the one
  indexed status — entering it emits `listing.removed` and drops the listing
  out of every public read through the single field that already decides
  visibility. A successful appeal moves `blocked → published` again.
- **Re-moderation of a live listing rides the moderation axis alone**
  (post-moderation, 0.5.0): re-publishing something already `published` keeps
  the lifecycle at `published` and moves only `moderation_status → pending`,
  so the owner's edit is live at once and a rejecting verdict removes it
  through the `published → blocked` takedown above. First publication is
  unchanged and stays pre-moderation (`draft → pending`, nothing public until
  the verdict).
- **`features_search` is derived, never stored by hand**: it is re-derived from
  `features` on every write that touches the source and on every entry into an
  indexed status, so a republish from `paused` announces the current
  projection rather than the one built at the last publish.
- **A value-validation pipeline** that fetches the category's feature schema
  over comm (`categories.features`) and delegates every check / DTO→DAO
  conversion to **stapel-attributes** — no attribute engine is re-implemented
  here.
- **Publish service** (draft→pending on a first publication, moderation-axis
  only on a live re-publish; projections built, moderation requested)
  and **favorites** as first-class engagement (the `UserAdLike`/`UserAdView`
  external-stats read-caches are dropped).
- A comm surface (below) that emits index/moderation events and consumes
  category/moderation/GDPR events, plus a `listings.status` Function and a
  GDPR provider.
- **Two authorization rules over the whole HTTP surface, each with one
  implementation** (0.6.2): every *write* to an existing listing —
  `PUT`/`PATCH`, `save-draft`, `publish`, `archive`, `complete`, `DELETE` —
  passes `views.ListingViewSet._get_own` (404 absent, 403 someone else's);
  every *read by id* — the detail route, `favorite`, `my/favorites` —
  resolves through `ListingQuerySet.visible_to(user)`, which is the indexed
  statuses for everyone plus one's own rows in any status. A row a caller may
  not read is filtered at the queryset, so it 404s exactly as an absent one
  does. Staff get no bypass on either rule: moderation acts through the
  `moderation.completed` contract and the admin, not through these views. The
  two exceptions are deliberate and named: `GET /{pk}/status/` is the
  inter-service existence/status read (`AllowAny` over `all_objects`, no
  content — it answers for a soft-deleted listing on purpose), and
  `unfavorite` only deletes the caller's own row.
- **Two owner-scoped reads, one scope** (0.7.0): `GET my/counters` (three
  integers) and `GET my/listings` (the rows behind them) both answer
  `Listing.objects.owned_by(request.user)` under `IsAuthenticated` — every
  status the caller owns, soft-deleted excluded by the default manager,
  narrowable with `?status=` (repeat the parameter or pass one
  comma-separated value; an unknown value is a `400`
  `error.400.listing_invalid_status_filter`, not an empty page) and paginated
  in the module's `IDAnchorPagination` envelope. `list` is the shop window —
  `published()`, narrowable to nobody — so `my/listings` is the **only** route
  by which a person can be shown their own drafts. Its rows use
  `MyListingCardSerializer`: the public card plus `moderation_status` (the
  second axis, which only an owner is owed) and the `title_draft` /
  `price_draft` / `images_draft` twins (the published fields are empty on a
  listing that has never been published, so a drafts tab built on the public
  card would be a column of blank rows). The detail read is unchanged and
  still serializes the published fields only.

## What this module deliberately does NOT do (boundaries)

- **Search / filtering** is a separate module (**stapel-search**). This module
  BUILDS the `features_search` projection, emits `listing.published` /
  `listing.updated` / `listing.removed` and answers
  `listings.search_documents` / `listings.search_export` for the indexer, but
  exposes **no** search or filter endpoints. The events are the *signal*, the
  Functions are the *document*: event payloads carry identity only, so no
  listing text, price or PII rides the durable bus to every subscriber, and an
  indexer never reads this module's database.
- **Moderation** (LLM pipeline, notice-and-action, auto-approve policy) is a
  separate module (**stapel-moderation**). This module emits
  `listing.submitted`, answers `listings.moderation_content` and consumes
  `moderation.completed`; it only applies a verdict, it does not decide one.
  (`AUTO_APPROVE_ON_PUBLISH` is a minimal-deployment escape hatch, not a
  moderation policy.) The verdict topic is **target-generic** — one queue
  moderates listings, reviews, profiles and chat messages — so a verdict
  addresses its target as `{target_type, target_key}` and this module applies
  only the ones whose `target_type` is its `MODERATION_TARGET_TYPE`.
  A **re-publish emits `listing.submitted` again**, after the new content is
  committed — the event carries identity, so the screener reads the *edited*
  content through `listings.moderation_content`. What happens on the other
  side is the moderation module's call, not ours: stapel-moderation dedupes
  by case state (one open case per target), so the intake either opens a fresh
  case (the previous one is resolved) or lands as an audited resubmission on
  the case already open, re-screening only one that was never screened. No
  intake topic in the fleet carries a content-revision token, so a redelivery
  and a genuine edit are indistinguishable on the wire by design (see
  stapel-moderation `MODULE.md`); the explicit "look again" paths are its
  `rescan` endpoint and the `moderation.submit` Function.
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
- **Geocoding/proximity** live in **stapel-geo**; this module never imports
  it. `Listing.save()` stamps `geohash_draft` from `lat_draft`/`lon_draft`
  (`compute_geohash_draft()`) through the `geo.geohash_encode` comm Function
  the same way `price_base` is kept in sync from `price` — one call site, no
  second place that could disagree with it. `publish_listing` promotes
  `geohash_draft` -> `geohash` exactly like the coordinate twins, so this one
  call site fixes both fields. **No hard dependency at import**: when
  `geo.geohash_encode` is unreachable (stapel-geo not deployed, no route
  configured, a bad reply) the geohash is left `""` rather than raising or
  keeping a stale value describing the wrong coordinates — same "unknown
  beats wrong" stance as `price_base`'s `NULL`. An empty geohash never
  produces a wrong answer downstream: stapel-search's postgres backend
  (0.2.2+) treats `geohash = ''` as *unknown*, not *elsewhere*, and falls
  back to its exact lat/lon box — it only costs the geohash prefilter its
  index (a full box scan instead of an indexed prefix lookup). Existing rows
  written before this release backfill via
  `python manage.py listings_backfill_geohash` (idempotent, rerunnable,
  `--dry-run`/`--batch-size`/`--limit`; see `services/geohash_backfill.py`).

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
| `MODERATION_TARGET_TYPE` | `"listing"` | The `target_type` this module answers to in target-generic `moderation.completed` verdicts (match the host's `STAPEL_MODERATION["TARGET_TYPES"]` key). |
| `LISTING_URL_TEMPLATE` | `""` | Public URL template formatted with `listing_id`, returned by `listings.moderation_content`. Empty = unknown; this module serves no site of its own and will not guess one. |
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
| `my/listings` | `my_card_serializer_class` | `MyListingCardSerializer` |
| `create` / `update` / `save-draft` | `draft_serializer_class` | `ListingDraftSerializer` |

### comm surface

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Function (provides) | `listings.status` | `{listing_id}` -> `{listing_id, owner_id, status, moderation_status, is_active, is_deleted}` | `schemas/functions/listings.status.json` |
| Function (provides) | `listings.search_documents` | `{keys:[…]}` -> `{key: {title, description, language, category_id, owner_id, price, currency, price_base, lat, lon, geohash, location_id, location_label, status, moderation_status, features_search, features_title, images, published_at, updated_at}}` — absent key = no document | `schemas/functions/listings.search_documents.json` — **search boundary** |
| Function (provides) | `listings.search_export` | `{cursor?, limit?}` -> `{rows:[{key, seq, …document}], cursor, total}` — snapshot contract verbatim from `stapel_core.comm.projections._iter_snapshot` | `schemas/functions/listings.search_export.json` — **search boundary** |
| Function (provides) | `listings.moderation_content` | `{listing_id}` -> `{listing_id, text, title, language, media, author_id, url, status, moderation_status}` | `schemas/functions/listings.moderation_content.json` — **moderation boundary** |
| Emit (Action) | `listing.submitted` | `{listing_id, owner_id, category_id, title, description, language}` | `schemas/emits/listing.submitted.json` — **moderation boundary** |
| Emit (Action) | `listing.published` | `{listing_id, owner_id, category_id, status, features_search}` | `schemas/emits/listing.published.json` — **search boundary** |
| Emit (Action) | `listing.updated` | same as published | `schemas/emits/listing.updated.json` — **search boundary**; fires when the content of a listing that IS indexed changes (re-publish of a live listing, or any write of an indexed field on it) |
| Emit (Action) | `listing.removed` | `{listing_id, owner_id, category_id, status, reason}` | `schemas/emits/listing.removed.json` — **search boundary** |
| Consume (Action) | `category.changed` | `{category_id, revision}` | `schemas/consumes/category.changed.json` (owned by stapel-categories) |
| Consume (Action) | `moderation.completed` | `{target_type?, target_key, decision, reason_code?, note?, …}`; `{listing_id}` accepted as the pre-0.4 alias | `schemas/consumes/moderation.completed.json` (owned by stapel-moderation) |
| Consume (Action) | `user.deleted` | `{user_id, …}` | `schemas/consumes/user.deleted.json` (owned by stapel-auth/gdpr) |
| Call (depends on) | `categories.features` | `{category_id}` | provided by stapel-categories |
| Call (depends on) | `geo.geohash_encode` | `{lat, lon}` -> `{geohash}` | provided by stapel-geo; graceful when unanswered (`compute_geohash_draft()` above) — no `stapel-geo` dependency in `pyproject.toml` |

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
  don't skip `transition_to` (it emits the index events). Assigning `status`
  directly is how a takedown used to happen silently.
- **Don't write `features_search` by hand** — it is derived from `features`
  (`Listing.rebuild_features_search`). Write the source; the projection
  follows on save.
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
