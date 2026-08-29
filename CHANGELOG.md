# Changelog

All notable changes to stapel-listings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.9.1] — 2026-08-30

### The public read was true but unowned

Anonymous browsing works today because `ListingViewSet` carries
`IsAuthenticatedOrReadOnly` and the detail read resolves through
`visible_to`. Nothing asserted either from the outside, so it was a property
of the code rather than a promise of the surface: change that one line to
`IsAuthenticated` and the whole suite stays green while every catalogue page
on the internet answers 401. For a classified — where most traffic arrives
from a search engine with no session and will never get one — that is the
single most expensive regression this module can ship, and it was the one
regression no test would have caught.

`tests/test_public_read.py` makes it a contract. A client with **no
credentials at all** (not a guest account — a stranger) gets 200 on the list
and on a published listing, another user's draft is absent from the list and
404s by id exactly as an absent row does, and none of those responses carries
a `Set-Cookie` — a read must stay cacheable at the edge and must not start a
session per crawler. The permission class is asserted by name, so a
regression fails a test that says what broke instead of fifty that say
`401 != 200`. The write door is checked from the same anonymous side: refused,
and 401 rather than 403 wherever the deployment's authenticator offers a
challenge, which is what a fleet on `JWTCookieAuthentication` returns.

Tests only. No behaviour, no schema, no settings change.

## [0.9.0] — 2026-08-28

### A merge deleted the guest and transferred nothing

`user.merged` arrives when an anonymous visitor signs into a real account.
This package now consumes it and moves the guest's rows onto the survivor.

Before, nothing consumed it: the guest row was deleted and every CASCADE
foreign key went with it, so a visitor who favourited three listings and then
signed in lost all three. The comment promising a transfer had never been kept,
and the loss was silent — the right number of rows simply stopped existing.

The consumer opens a transaction and decides inside it, before any write:
a guest that owns nothing here returns quietly (which also covers the second
delivery of an already-completed merge, so idempotency stays silent), a
survivor with no local user row yet raises so the outbox redelivers, and only
then does the transfer run. Ordering is deliberate — the owns-nothing check
runs first, so an unknown survivor plus an empty guest never starts a retry
loop.

### `ALLOW_ANONYMOUS_WRITES` — the wall was in the interface only

A silently minted guest could `POST` a listing and publish it: 200, live.
The refusal existed in the frontend and nowhere else. The axis defaults to
**False**; favouriting and unfavouriting stay open on purpose, and are tested
as such, because saving something you are looking at is the act auto-anonymous
exists to allow.

## [0.8.0] — 2026-08-28

### The status probe was an enumeration oracle

`GET /listings/{pk}/status/` was `AllowAny` and returned `owner_id`,
`moderation_status` and the full lifecycle for any id — reading `all_objects`,
so soft-deleted and unpublished rows answered too. Listing ids are sequential.
Walking them anonymously harvested, for every listing in a deployment
including other people's **drafts, rejected and deleted** listings, who owns it
and what a moderator decided about it. Found by doing exactly that against a
live stand:

    GET /listings/api/v1/listings/10/status/
    → 200 {"status":"draft","moderation_status":"pending","is_deleted":true,
           "owner_id":"720a67e2-…"}

### The first fix was wrong, which is worth recording

The obvious repair — lock the action to `IsServiceRequest`, since it is
documented as inter-service and carries the `listings.status` function — would
have **broken a real browser client**. `@stapel/listings-react`'s
`ListingDetail` calls this endpoint in parallel with the detail read for a
reason its own docstring states: `GET /{pk}/` excludes soft-deleted rows, so a
removed listing 404s and a made-up id 404s identically. The probe is what turns
those into two different sentences, and "this listing was removed" is the one a
person following a stale link actually needs.

"The only caller is another service" was a search result, not a fact. A grep of
the consuming pair found the browser caller in one command.

### So the capability stays and the disclosure goes

The action is `AllowAny` again. A fleet service (X-API-KEY) and the listing's
**own owner** get the full `ListingStatusSerializer`. Everyone else — including
a signed-in stranger, because otherwise the oracle costs an attacker one free
account — gets `ListingPresenceSerializer`: a single `is_deleted` boolean,
which is the only field the browser client reads.

### Notes

The suite asserts the stranger's shape, that `owner_id`/`moderation_status`/
`status` are absent by name (so re-adding one fails loudly rather than
silently widening the response), that a service and the owner still get the
full view, that a signed-in non-owner does not, and that a soft-deleted row
still answers at all — which is the whole reason the probe reads
`all_objects`. Those assertions go red against the pre-fix code.

## [0.7.1] - 2026-08-24

Patch (pre-1.0 semver: minor = breaking, patch = compatible). Bug fix — a
live geo defect: stapel-geo's own `MODULE.md` documented listings as the
consumer of `geo.geohash_encode` ("this is how consumers stamp geohashes
onto their own rows"), but nothing in this module ever called it. Every
listing carrying `lat`/`lon` also carried `geohash=""`. stapel-search 0.2.2
made the lat/lon box authoritative, so results stayed *correct* — but the
geohash prefilter could never use its index, so every geo-filtered query
fell back to a full box scan.

### Fixed

- **`Listing.save()` now stamps `geohash_draft`** from `lat_draft`/
  `lon_draft` via the `geo.geohash_encode` comm Function
  (`compute_geohash_draft()`), the same shape as `price_base` being kept in
  sync from `price` — recomputed whenever a save touches `lat_draft`,
  `lon_draft` or `geohash_draft` (or is a full save), one call site, no
  second place that could disagree with it. `publish_listing` already
  promoted `geohash_draft` -> `geohash` on publish, so this one call site
  fixes both the draft and the published field.
- **No hard dependency on stapel-geo**: consumed by comm name only (no new
  `pyproject.toml` dependency, no import). When `geo.geohash_encode` is
  unreachable — not deployed, no route configured, a bad reply — the
  geohash is left `""` rather than raising or keeping a stale value tied to
  the *previous* coordinates; an empty geohash never produces a wrong
  answer downstream (stapel-search 0.2.2 treats it as "unknown", not
  "elsewhere", and falls back to the exact box), it only costs the
  prefilter its index.
- **`ListingDraftSerializer.geohash_draft` is now read-only.** It was a
  plain writable `ModelSerializer` field — the module expected a *caller* to
  compute and send a correct geohash by hand, with no validation that it
  actually matched the coordinates, which is exactly backwards from
  stapel-geo's documented contract above. A value sent in the request body
  is now silently ignored in favor of the server-computed one.
  `docs/schema.json` regenerated (`make contract`): `geohash_draft` carries
  `readOnly: true` in both request schemas that include it.
- **`python manage.py listings_backfill_geohash`** (new management command,
  `services/geohash_backfill.py`) — stamps `geohash`/`geohash_draft` on
  listings written before this fix: any row with coordinates and an empty
  geohash. Idempotent and resumable (only ever touches `geohash* = ''`
  rows, so a crash mid-run loses no progress and a second full run is a
  no-op), `--dry-run`/`--batch-size`/`--limit` for staging on a large
  table, graceful the same way `compute_geohash_draft()` is when geo is
  unreachable (rows left `unresolved`, reported, not an error).
- `tests/test_geohash_stamp.py` (12 tests): stamp on create, stamp on a
  coordinate update (and only on one — an unrelated-field save leaves the
  existing geohash alone), a changed coordinate recomputes rather than
  keeping the old geohash, no crash and no stamp when `geo.geohash_encode`
  has no provider, a stale geohash is cleared (not kept) when geo becomes
  unreachable after a coordinate change, and a client-supplied
  `geohash_draft` is ignored end-to-end through the serializer.
  `tests/test_geohash_backfill.py` (10 tests): both populations
  (published/draft) stamped independently, idempotent re-run, `--dry-run`
  writes nothing, geo-unanswered leaves rows `unresolved` without raising,
  soft-deleted listings included, `--limit` bounds each population
  independently, and the management command's stdout report / flags.
  `tests/test_publish.py::test_publish_promotes_lat_lon_with_geohash`
  updated for the new server-computed value instead of a hand-set one.

## [0.7.0] - 2026-08-24

**Feature.** Minor (pre-1.0 semver: minor = breaking, patch = compatible) —
nothing existing changed shape, but a new route joins the surface.

The one gap `@stapel/listings-react` recorded rather than papered over
(`packages/listings-react/MODULE.md` §3 ask 1, `src/model/mineSource.ts`; the
storefront spec §13.9 note 1): **this module could not list a person their own
listings.** `GET /listings/` answers `published()` and takes no owner
parameter, so a seller's own DRAFTS were unreachable by any call the contract
offered — the pair shipped an injected `MyListingsSource` and a NAMED failure
when a host had not wired one, because "we cannot ask" and "you have no
listings" are different sentences.

### Added

- **`GET /listings/api/v1/my/listings/`** — the caller's own listings, in
  every status. The counterpart of `my/counters`: the same owner scope
  (`Listing.objects.owned_by(request.user)` under `IsAuthenticated`), the
  same status grouping, but the rows behind the three numbers. All nine
  lifecycle statuses are visible to their owner, `blocked` included — the one
  status `my/counters` counts in no tab at all, and the one whose owner most
  needs to know. Soft-deleted rows are excluded by the default manager. The
  envelope is the module's `IDAnchorPagination`, byte-identical to
  `my/favorites`.
- **`?status=` on it**, in both spellings a client may reach for: a repeated
  parameter (`?status=draft&status=rejected`) and one comma-separated value
  (`?status=draft,rejected`) — a dashboard tab is a *set* of statuses (the
  groupings are `my/counters`', so a tab's rows and its count cannot describe
  different sets), and which spelling an HTTP client produces is not the
  caller's choice to make. Omitted means every status.
- **`MyListingCardSerializer`** — the public card plus exactly two owner-only
  additions and no more: `moderation_status`, because since 0.5.0 a
  *published* listing can be under re-review and its owner is the one person
  who has to be told (a sentence no client can derive from `status`), and the
  `title_draft` / `price_draft` / `images_draft` twins, because the published
  fields are empty on a listing that has never been published and a drafts tab
  built on the public card would render a column of blank rows. That is the
  **list half of the pair's ask 2**; the detail read is unchanged and still
  serializes the published fields only. Plus `created_at` / `updated_at` for
  the row's "when". Owner-scoped by construction — one route uses it, and
  that route's queryset is `owned_by`.
- `views.parse_status_filter` — the one place the two spellings collapse.
- `my_card_serializer_class` on `ListingViewSet`, joining the per-action
  serializer seam (MODULE.md, "Serializer seams").
- `error.400.listing_invalid_status_filter` — an unknown value in `?status=`
  is a `400` naming it, not a silent empty page: "no listing is in that
  state" and "that state does not exist" are different answers, and only one
  of them is a client bug worth surfacing.
- `tests/test_my_listings.py` (46 tests): every status visible to its owner
  and no status of a stranger's visible at all (both parametrized over all
  nine), anonymous refused rather than given an empty page, soft-deleted
  excluded, both filter spellings, the three counter tabs asserted to agree
  with `my/counters` row-for-count, the `400`, newest-first ordering, the row
  shape (both axes, the draft twins, what is deliberately absent), and the
  anchor walk with the filter surviving a page turn. 239 tests green (193
  before).

### Unchanged, deliberately

- `GET /listings/` is still the shop window and still takes no owner
  parameter. An `?owner=` filter on a public list would put the same owner
  scope behind a predicate a caller supplies; a separate owner route cannot
  be pointed at anyone else by construction.
- The detail read, the favorites reads and every write keep the 0.6.2
  authorization rules — `visible_to` for reads by id, `_get_own` for writes.
  `my/listings` adds a third owner-scoped *collection* read, not a third rule.

## [0.6.2] - 2026-08-22

**Security.** Patch (pre-1.0 semver: minor = breaking, patch = compatible).
No route, component or error-key change — two authorization holes closed on
routes that already existed.

Filed by @stapel/listings-react (`packages/listings-react/MODULE.md` §3
asks 3 and 4; the storefront spec §13.9 notes 4 and 5). The
pair had already declined to call `PUT`/`PATCH` for this reason, but the
endpoints were reachable by anything else that speaks the contract.

### Security

- **`PUT`/`PATCH /listings/{id}/` skipped the ownership check.** Both were
  the plain `ModelViewSet` writes, resolving their object off
  `Listing.objects.all()` under `IsAuthenticatedOrReadOnly` — so **any
  authenticated caller could write any listing's draft fields** (title,
  description, price, images, features, category, stock). They now pass
  `views.ListingViewSet._get_own`, the module's one ownership gate, with the
  shapes every other owner operation returns: `404`
  (`error.404.listing_not_found`) for an absent or soft-deleted listing,
  `403` (`error.403.listing_not_owner`) for someone else's.
  `partial_update` routes through `update`, so one check covers both verbs.
- **`GET /listings/{id}/` served unpublished listings to anyone holding the
  id** — a draft, a rejected, a paused and a *blocked* (moderation takedown)
  listing all answered `200`. The detail read now resolves through the new
  `ListingQuerySet.visible_to(user)`: the indexed statuses for everyone,
  plus the caller's own rows in any status. The filter is applied at the
  queryset, so a listing a caller may not read `404`s from the same code
  path an absent id does — indistinguishable, per the fleet's uniform-404
  canon. Staff get no bypass (moderation acts over `moderation.completed`
  and the admin, not these views).
- **The same missing filter on the two favorites reads**, found by auditing
  the rest of the viewset: `POST /{id}/favorite/` resolved over
  `Listing.objects` and so confirmed a stranger's draft exists (now `404`,
  while favoriting one's *own* unpublished listing still works), and
  `GET my/favorites/` kept serving the card of a favorited listing after it
  left the index (now filtered the same way).
- Audited and deliberately unchanged: `GET /{id}/status/` stays `AllowAny`
  over `all_objects` — it is the inter-service existence/status read that
  carries no content and answers for a soft-deleted listing on purpose
  (design §13.9 note 6); `unfavorite` only deletes the caller's own row;
  `list` was already `published()`-only; `destroy`, `save-draft`,
  `validate-draft`, `publish`, `archive`, `complete` already went through
  `_get_own`; `my/counters` is owner-scoped.

### Added

- `ListingQuerySet.visible_to(user)` — the read counterpart of `_get_own`,
  and the one place the read rule lives.
- `tests/test_authz.py` (42 tests): owner can / non-owner cannot / anonymous
  cannot for `PUT` and `PATCH`; `draft`, `pending`, `blocked` and the other
  five unindexed statuses each pinned on the detail read from a stranger's,
  an anonymous and the owner's viewpoint; staff pinned as non-special on
  both rules; hidden-vs-absent responses asserted byte-identical; the
  favorites and list routes pinned too.

### Changed

- `docs/schema.json` regenerated (`make contract`): the `PUT` operation
  description now states the ownership rule.

## [0.6.1] - 2026-08-22

Patch (pre-1.0 semver: minor = breaking, patch = compatible). Bug fix
inherited from upstream — no route/component/error-key change of its own.

Filed by @stapel/categories-react (the storefront spec §13.7
note 5): `docs/schema.json`'s `FeatureDto`/`FeatureDao` discriminator
mapping had a single bogus `"null"` entry instead of the ten feature-type
slug entries, because stapel-attributes' `PolymorphicProxySerializer` was
built from a bare list of serializer classes and drf-spectacular's
resource-type inference collapsed every sub-serializer to `None` (fixed
upstream in stapel-attributes 0.4.7 — see its CHANGELOG for the full
root-cause writeup).

### Fixed
- Floor bumped to `stapel-attributes>=0.4.7,<0.5` and `docs/schema.json`
  regenerated (`make contract`): `FeatureDto`/`FeatureDao`'s
  `discriminator.mapping` now carries all ten slug-keyed entries
  (`int`, `float`, `string`, `bool`, `hex_color`, `select`, `date`,
  `header`, `hierarchical_select`, `convertible_unit`), fixing the
  openapi-typescript codegen that previously stripped `type` from call
  sites and re-added a synthetic wrong one.
- Added a contract test asserting the committed `docs/schema.json` mapping
  is slug-keyed, matches the registered type slugs 1:1, and never contains
  `"null"`.

## [0.6.0] - 2026-08-22

**This module now emits its own contract triad.** `docs/schema.json`,
`docs/flows.json` and `docs/errors.json` did not exist before this release —
the Makefile said so out loud (`Makefile:12`, superseded below) — which
blocked the react codegen pipeline (`gen:api`/`gen:errors`/`gen:manifest`)
for any `-react` pair generated against this module
(the storefront spec §1.8, §3.10, A1).

### Added

- `_codegen.py` + `_codegen_settings.py` + `codegen_urls.py`: a
  single-module `{listings + core}` Django harness that emits
  `docs/{schema,flows,errors}.json` at the canonical `/listings/api/v1`
  prefix, the same mechanism stapel-search/-chat/-forms already use.
  `make contract` / `make contract-check` now cover the triad, not just
  `docs/llms.txt` + README.md.
- `docs/schema.json` (12 paths), `docs/flows.json` (`[]` — no `@flow` is
  declared yet, same state as every other contract-complete module today),
  `docs/errors.json` (63 keys: 9 owned by this module, the rest inherited
  from stapel-core/stapel-attributes).
- `tests/test_contract.py`: every mounted route is described in
  `docs/schema.json`; every `STAPEL_LISTINGS_ERRORS` code and every
  stapel-attributes validation code the publish path can raise is declared
  with the correct `owner`.

### Changed

- `ListingCardSerializer.images` / `ListingDetailSerializer.images` are now
  declared as `serializers.ListField(child=CharField())` instead of falling
  back to `ModelSerializer`'s untyped JSONField mapping — the schema now
  says `array[string]` (opaque `<type>/<hash>` CDN refs, same shape as the
  already-typed `images_draft`) instead of an unstructured blob. Response
  bytes are unchanged; only the declared OpenAPI type is.
- `docs/readme.md`: the quick-start mount snippet corrected to
  `path("listings/api/", include("stapel_listings.urls"))` — this module's
  own `urls.py` bakes in only `v1/`, the host contributes `api/`, exactly
  the recipe stapel-example-monolith already uses for its siblings. Plus a
  documented, deliberate gap: `features_search` stays a bare `object` in the
  schema (a per-category flattened index with no fixed key set — see the
  README "Contract" section).

## [0.5.0] - 2026-08-21

**Behaviour change — editing a published listing no longer hides it.**
Re-moderation of a live listing rides the moderation axis alone
(`tasks/classified-v2-architecture.md`, addendum of 2026-08-21). Hosts that
relied on a re-publish yanking the listing out of the public reads must read
`moderation_status`, not `status`, for "an edit is under review".

### Changed

- **Re-publishing a `published` listing keeps it `published`.** The owner's
  edit is promoted and live immediately; only `moderation_status` moves back to
  `pending`. Previously `publish_listing()` assigned `status = pending` **by
  direct assignment, past the FSM** — no transition, no event, and the listing
  silently vanished from `Listing.objects.published()`, from `is_active`, and
  from every search index for as long as re-moderation took. That was a
  takedown in all but name, applied *before* anyone had looked at the content,
  and it was unreachable from a host without a fork. The model is now
  post-moderation, consistent with `listing.updated` already firing on live
  edits: content goes live, a verdict can remove it.
- **A rejecting verdict on a re-moderated edit takes the listing down through
  the existing `published → blocked` edge** (`apply_moderation("rejected")`,
  0.4.0), so the removal is expressed by the one field that decides visibility
  and announces itself with `listing.removed`. An approving verdict returns
  `moderation_status` to `approved` and touches the lifecycle not at all — no
  transition, no index churn, no re-emitted `listing.published`.
- **A re-publish still requests moderation**: `listing.submitted` is emitted
  after the new content is committed, in the same `mutate_and_emit()` block, so
  the screener that pulls `listings.moderation_content` reads the *edited*
  content. stapel-moderation dedupes intake by case state (one open case per
  target): the event opens a fresh case when the previous one is resolved, or
  lands as an audited resubmission on a case already open. No intake topic in
  the fleet carries a content-revision token, so a bus redelivery and a genuine
  edit stay indistinguishable on the wire by design — the explicit "look again"
  paths are moderation's `rescan` endpoint and `moderation.submit`.
- **`listing.updated` for a live re-publish now comes from `Listing.save()`'s
  own detector** instead of a second, unconditional emit inside
  `publish_listing()`. One detector owns the fact, it compares the promoted
  fields against the stored row, and its payload carries `status: "published"`
  — where the pre-0.5 emit announced `status: "pending"` for a listing the
  indexer was supposed to keep. A re-publish that moves no indexed field now
  correctly announces nothing.
- **`POST /listings/{id}/publish/`** answers `status: "published"` for a live
  re-publish (it answered `"pending"` before). First publication still answers
  `"pending"`.

### Unchanged

- **First publication** is still pre-moderation: `draft → pending`, nothing
  public until the verdict. So is a re-publish from any non-indexed status
  (`paused`, `expired`, `rejected`, …) — that listing is invisible either way,
  so it is a first publication again.
- The 0.4.0 `features_search` freshness rules: the projection is re-derived on
  the same write that promotes `features`, so the document an indexer pulls
  right after a re-publish carries the new attribute projection (pinned by
  tests on both the keyed batch and the export row).

## [0.4.0] - 2026-08-21

The upstream release that unblocks **stapel-search** and **stapel-moderation**
(`tasks/stapel-search-design.md` §16.1/§18, `tasks/stapel-moderation-design.md`
§16.1/§20). Both specs named the same four defects in this module; none of them
was reachable from a host project without a fork, so all four are fixed here.

### Added

- **`listings.search_documents`** `{keys:[…]}` → `{key: document}` — the keyed
  batch read of the search seam. The `listing.*` events carry identity only
  (their payloads are `additionalProperties: false` and deliberately minimal),
  so an indexer uses the event as a signal and pulls the document over comm
  rather than reading this module's database — the same shape in a monolith
  (in-process `call()`) and in a split (over the bus). Decimals travel as
  strings, datetimes as ISO 8601. A key with no listing — unknown or
  soft-deleted — is simply absent from the answer, which is how an indexer
  learns to drop it.
- **`listings.search_export`** `{cursor, limit}` → `{rows, cursor, total}` —
  the snapshot read for backfill, rebuild and drift-check, contract verbatim
  from `stapel_core.comm.projections._iter_snapshot`. Rows carry their source
  `key`, a `seq` in unix milliseconds (the same unit as
  `stapel_core.bus.Event.timestamp`, so a snapshot row and a live event for
  one listing are directly comparable) and the same document body
  `search_documents` returns. Keyset paging on the primary key.
- **`listings.moderation_content`** `{listing_id}` → `{listing_id, text,
  title, language, media, author_id, url, status, moderation_status}` — the
  moderation seam. The verdict bus carries identifiers only, so a screener
  reads content through this authorized call at the moment it screens instead
  of from an event payload that went stale hours ago. Published fields first,
  draft twins as the fallback.
- **`blocked` lifecycle state** and the **`published → blocked`** edge —
  moderation takedown of a live listing. Reachable only from `published` and
  only through `apply_moderation("rejected")`; the owner API has no route to
  it. Since `published` is the single indexed status, entering `blocked`
  removes the listing from every public read through the one field that
  already decides visibility (no second predicate, no
  visibility-reads-`moderation_status` coupling) and emits `listing.removed`.
  `blocked → published` reinstates on a successful appeal;
  `blocked → {draft, archived}` lets the owner rework or file it away.
- Settings `MODERATION_TARGET_TYPE` (default `"listing"`) and
  `LISTING_URL_TEMPLATE` (default `""`).

### Fixed

- **`listing.updated` had zero call sites** — the event was declared, schema'd,
  documented and emitted by nothing, so every edit of a live listing reached no
  index at all. It now fires from two places: re-publishing a listing that is
  currently indexed (the owner-edits-a-live-listing path through
  `publish_listing`), and any save that actually moves an indexed field on a
  listing in an indexed status (admin, a script, a data migration). The
  detection compares against the stored row rather than trusting
  `update_fields`, because a save-draft on a live listing is a full save that
  changes no published content — announcing that would be a lie the indexer
  pays for. Emitted inside `mutate_and_emit()` like every other `listing.*`
  event: the edit and its announcement commit together or not at all.
- **`features_search` was rebuilt at exactly one call site** (`publish_listing`),
  so a `paused → published` republish re-announced the projection built at the
  last publish, and any other write of `features` left the two disagreeing
  forever. It is now a *derived* value with one derivation
  (`build_features_search_from_list`, keyed off the stored `features` DAO
  list): re-derived on every save that writes `features`, and again on entry
  into an indexed status, before `listing.published` is emitted.
- **`apply_moderation("rejected")` bypassed `transition_to`** — it assigned
  `status` directly, so a rejection emitted no lifecycle event, and a verdict
  against an already-published listing changed nothing at all: the listing
  stayed live and indexed with `moderation_status=rejected`. Every lifecycle
  move now goes through `transition_to`, which owns the index events.

### Changed

- **`schemas/consumes/moderation.completed.json` widened to the target-generic
  shape.** The moderation queue is one queue over listings, reviews, profiles
  and chat messages, so a verdict addresses its target as
  `{target_type, target_key}`; the old schema was `required: ["listing_id"]`
  with `additionalProperties: false`, which made the integration physically
  impossible without this release. Now: `required: ["decision"]`,
  `target_type`/`target_key` understood, **`listing_id` still accepted** as the
  pre-0.4 alias (a payload with no `target_type` is a listing verdict by
  construction), emitter-owned extras (`case_id`, `source`, `decided_at`, …)
  accepted and ignored — a consumer must not reject a payload because the
  producer's contract grew. `decision` gains `dismissed` (the report needed no
  action; the target is untouched). Verdicts whose `target_type` is not this
  module's `MODERATION_TARGET_TYPE` are ignored. `reason_code` is recorded in
  `moderation_note` when no `note` is sent.
- `features_search` is no longer writable by hand in any meaningful sense: a
  save re-derives it from `features`. A row that carried a `features_search`
  with no corresponding `features` (an impossible state the projection
  contract never allowed) now normalises to `{}` on its next write.

### Migration

`0005_alter_listing_status` — choices-only `AlterField` adding `blocked`. No
column change, no data rewrite, no existing row affected.

## [0.3.8] - 2026-08-15

### Fixed

- `stapel-attributes` floor raised from `>=0.3,<0.4` to `>=0.4,<0.5`. The old
  attributes line caps `stapel-core<0.12`, so stapel-listings could not be
  installed next to any modern core — `ResolutionImpossible` against every
  sibling app that requires `core>=0.16`. stapel-categories 0.5.4 fixed the
  same defect; listings was left behind.

### Changed

- `stapel-core` floor raised from `>=0.10` to `>=0.26.0`, naming the core this
  app is actually developed and tested against — 0.26.0 is the core whose
  error registry runs the registry-catalog pairing gate that the deterministic
  `stapel_attributes` registration in this release exists for.

## [0.3.7] - 2026-08-02

### Fixed - `tests/test_contract.py` (added in 0.3.6) needs `stapel-tools` on the release track too

`ci.yml`'s test job only stayed green by accident: the `migration-lint`
step runs `pip install stapel-tools` before the test step, so
`stapel_tools.llms_txt` (imported by `tests/test_contract.py`) happened to
already be on the path. `publish.yml`'s test job has no migration-lint
step, so the same import failed there with `ModuleNotFoundError` — the
0.3.6 tag's publish run never got past `test` (no wheel was built,
nothing reached PyPI). Both workflows now install
`"stapel-tools>=0.9.1,<1"` explicitly in the "Install test dependencies"
step, matching the convention already used in `stapel-notifications`/
`stapel-profiles`/`stapel-shop`.

## [0.3.6] - 2026-08-02

Packaging/CI only, no runtime change.

### Changed
- CI now tests Python 3.14 (the version production runs) alongside 3.11-3.13.
- Badge canon; migration-lint step uncommented now that `stapel-tools` is on PyPI.
- Contract documents (`docs/capabilities.json`, `docs/flows.json`,
  `docs/errors.json`, `CONFIG.MD`) ship inside the wheel via `package-data` (#184).
- `docs/llms.txt` — the fifth contract artifact (badge-canon §3), rendered
  from the hand-authored `docs/capabilities.json`, wired into new
  `make contract`/`contract-check` targets plus `tests/test_contract.py`,
  and now packaged into the wheel.
- `docs/capabilities.json`'s hand-maintained `version` field brought back
  in line with `pyproject.toml` (it had drifted to 0.3.4 while the package
  moved to 0.3.5).

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
