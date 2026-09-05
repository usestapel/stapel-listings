# Changelog

All notable changes to stapel-listings are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.22.1] — 2026-09-05

### Fixed — a card badge stopped gluing its name into its value, and stopped printing «20000 км»

The desktop walk (PASS-16, Д421) caught the 0.21.3 card badge contract one
short of the job it set out to do: an apartment card correctly reads «Этаж
3», but a phone card read **«HONOR · Модель 90»** — the feature's own name
(«Модель») and its raw value («90») sitting next to each other with nothing
between them, one glued phrase instead of a name and an answer. And a car's
mileage read **«20000 км»** instead of «20 000 км»: the number reached the
card exactly as it was stored, ungrouped.

Both are fixed in `services/features.py`'s `decorate_card_element`, on the
same `name_value` / `value_unit` path the 0.21.3 contract already decides on:

- **Numbers are grouped per locale.** A caption built from a feature's raw
  stored value (`int` / `float`, not a vocabulary label) now gets RU
  thousands grouping — a non-breaking space every three digits, from five
  digits up: `20000` → `20 000`. Below that a number prints bare, which is
  the whole reason for the threshold: a four-digit number is very often a
  YEAR on a real catalogue (`year`, `built_year` — stored as a plain `int`,
  no vocabulary behind it), and grouping it would split `2019` into `2 019`.
  `GROUPING_THRESHOLD = 10_000` is the one constant that does both jobs.
  `value_unit`'s unit is unaffected, same as `postfix1000` already leaves the
  value alone — only the digits change shape.

- **`name_value` no longer glues.** `«Этаж 3»` reads fine because `«Этаж»`
  is a vocabulary TERM — `floor` / `floors` are `ref_select`/`select`, and a
  term's own name already reads as a natural prefix word. `«Модель 90»` does
  not, because `«90»` is the feature's raw value with no term behind it. So a
  `name_value` element built from a raw value now gets a trailing colon on
  `name` (`"Модель:"`), while a vocabulary-backed one (`floor`, `floors`)
  keeps its bare name exactly as before. The colon lands in the EXISTING
  `name` field rather than a new key: a client already joining `name` and
  `label` with one space — the 0.21.3 contract's own reference renderer —
  now reads «Модель: 90» with **no client-side change required**.

Both fixes are additive to the wire: `value`, `label`, `unit` and
`presentation` keep their 0.21.3 meaning, and the only elements affected are
the ones the bug was in (a raw-value `name_value`, or a `value_unit` /
`name_value` numeric caption of 10 000 or more). The existing badge-contract
tests (vocabulary-backed `floor`/`floors`, `square`'s `42 м²`, the boolean and
multi-select cases) are untouched and still pass unmodified.

Applied **on the way out** (`decorate_card_element`), exactly as the rest of
the card badge contract already is — not at build/publish time, so no
migration and no re-projection is needed for `features_title` /
`features_badges` themselves. **Fleets should still run
`listings_reproject_features` and `search_rebuild --type listing`**: a search
index built downstream of this module (`stapel_classified.cards.card_features`
decorates `features_title`/`features_badges` with this same call before
indexing them for the free-text/facet surface) can be holding pre-fix
rendered strings, and only a rebuild refreshes those cached copies — `stapel-listings`
itself has nothing stored to migrate.

## [0.22.0] — 2026-09-05

### Added — `listings.rename_feature_keys`: the write half of a slug rename

A catalogue import renamed five feature slugs in place on a live fleet
(`make_ref_select` → `make`, `body_type_ref_select` → `body_type`, and three
more; the loader's dry run said `features: updated 62` and nothing about a
rename). The category schema moved. The listings did not: every draft
composed against the old schema kept its answers under the OLD keys in
`features_draft`, so the make facet went empty, the search projection lost
the values, and `listings_reproject_features` — which keys on the CURRENT
slugs — would have DROPPED them rather than repaired them. A feature rename
is not a category-side edit at all; it is a two-sided data migration, and
this library owned a half of it that did not exist.

It exists now, as the comm Function `listings.rename_feature_keys`
(`{category_id, renames: {old: new}, dry_run}`) and the command
`listings_rename_feature_keys --category ID --rename old=new [--dry-run]`.
Across the category subtree — resolved rung by rung over the new
`CATEGORY_CHILDREN_FUNCTION` seam, because a feature defined on a parent is
answered by listings beneath it — it rewrites the KEY, keeps the value and
the key's position, re-projects the categories it touched through the same
`reproject_listings` pass a repair run uses, and lets those writes emit
`listing.updated` so a search index re-pulls. Idempotent: a second run finds
nothing to move and writes nothing.

Three refusals are the point of it:

* a draft that already answers the NEW slug as well as the old one keeps
  **both** keys untouched and is reported as a conflict, per listing, with
  both slugs — nothing here knows which answer the seller meant, and
  overwriting one with the other would be a silent edit of their data on a
  run whose whole purpose is to stop being silent;
* a rename map that cannot mean anything (empty, a slug onto itself, two old
  slugs onto one new one, or a chain `a→b, b→c`) is refused as a whole,
  before a single row is read: every one of those makes the result depend on
  iteration order;
* the memoized category schema is invalidated for every category in scope
  first. `category_schema` caches feature configs under a revision-keyed
  entry, and a re-projection that read a PRE-rename revision would re-derive
  every listing against the schema that just retired — writing back exactly
  the loss the call was made to undo.

What it does not cover, said rather than implied: soft-deleted listings are
outside the pass (they render nowhere and have already told the index they
are gone) and are counted as `deleted_skipped`; with no children provider
wired the call covers the one category it was named and answers
`subtree_resolved: false` instead of implying a subtree it never walked.

Minor, not patch: a new comm Function and a new settings key are features,
and pre-1.0 house semver reads a minor as the breaking edge.

## [0.21.6] — 2026-09-05

### Fixed — the search document dropped `features_badges` on the floor

`build_search_document` served `features_title` but never `features_badges`,
even though both columns exist on `Listing`, are built by the same
`services.features` projection, and the public listing API serializes both
side by side. The search document's free-text arm got the title line; the
badge chips a card draws next to it never left this module.

Downstream this was invisible until a consumer actually rendered the chips:
`stapel-classified` 0.10.9's search source, `cards.card_features`, calls
`stapel_listings.services.features.decorate_card_elements` on whatever the
document hands it under `features_badges` — and since the key was simply
absent, `payload.get(key) or []` always resolved to `[]`. Every published
listing with a badge-flagged feature (a closed-select "condition", say)
indexed correctly, searched correctly, and then rendered its card with no
badge at all — not a wrong badge, a silently missing one.

`build_search_document` now serves `features_badges` next to
`features_title`, filtered through the same `public_daos` belt-and-braces
pass (the projection is already public-only at build time in
`services.features`; this catches a row built by an older writer, before
the visibility axis existed, exactly as `features_title` already does). No
new import, no new column — the source and the decoration are both already
this module's.

`docs/schema.json` is unchanged: the search document is an internal comm
shape (`listings.search_documents` / `listings.search_export`), not an HTTP
response, so it carries no OpenAPI surface to update.

## [0.21.5] — 2026-09-04

### Added — `listings.draft_content`: the owner-scoped read a composer needs

A service that ANALYSES a draft — recognises the photographs, drafts the
text, fills the characteristics — is addressed by the draft id alone. It has
no request body to read the content out of (the composer sends none: the
photos are already uploaded and the row is the truth), it must not read this
module's database, and the only read it could reach was the PUBLIC detail
one, which serves the published columns — empty on everything that has never
been published.

So it analysed nothing. Measured on a live stand: the analysis job hashed two
empty strings, ran every stage over an empty subject, and its screening stage
reported the listing as having no content — while `images_draft` held both
photographs and `title_draft` the seller's own title, the whole time.

`call("listings.draft_content", {"listing_id": ..., "owner_id": ...})` answers
`{listing_id, owner_id, category_id, title, description, images, features,
language, is_empty}`:

* **Draft twin first, published column as the fallback** — the opposite order
  to `listings.moderation_content`, deliberately: screening judges what is
  LIVE, while a composer works on what the seller is writing, so an edit in
  progress is what it must see.
* **`features` is `features_draft`** (the seller's slug→value map) with no
  published fallback: the published `features` column is a display
  projection, a different shape, and answering one where the caller expects
  the other is worse than answering nothing.
* **Owner-scoped by payload.** A mismatched `owner_id` raises `LookupError`
  worded exactly as an unknown listing, so the call is not an existence
  oracle over other people's drafts.
* **`is_empty`** is this module's own answer to "is there anything here to
  work on", so callers do not each reimplement it and disagree about
  whitespace.

Additive: one new Function and its contract file, no model, migration or
endpoint change.

## [0.21.4] — 2026-09-04

### Fixed — a draft may exist before the category does

A composer analyses the seller's **first photo**, and the analysis job is
addressed by the **draft id** — so the row has to exist before anything about
the item is known, several steps before the category is chosen. It could not
be created. `Listing.category_id` was `NOT NULL`, `ListingDraftSerializer`
inherited that as a required field, and the create call was refused. No draft
id came back, the job never started, and the composer hung on the first step.
The storefront was hotfixed to invent a placeholder category; this release
removes the thing it was working around.

**`category_id` is nullable now** (migration `0011`, `AlterField` to
`null=True` — expand-only: widening a column to accept NULL adds no
constraint, rewrites no row, and an older writer keeps working against it
unchanged). `save-draft` accepts a missing category, the owner's draft read
answers `"category_id": null` — an explicit "not chosen yet", never an omitted
key a client has to guess about — and `""` from a client that clears the field
is normalised to NULL so the column has one spelling of "no category".
`features` / `features_draft` are simply empty: with no category there is no
schema, so there is nothing to fill in yet.

**The `NOT NULL` moves to where it was actually load-bearing.** It never
protected the draft; it protected everything downstream of publish. So the
guarantee is kept at the service level, behind one predicate
(`models.has_category`) at the two doors a row uses to leave the draft track:

- `Listing.transition_to` refuses any status outside `CATEGORYLESS_STATUSES`
  (`draft`, `archived`) without a category — nothing category-less reaches
  moderation or the index, whatever route it took;
- `services.publish.publish_listing` — which assigns `PENDING` itself, past
  the FSM — raises the same refusal, so a storefront that skipped
  `validate-draft` cannot publish one anyway. The view maps it to the existing
  **`error.400.publish_validation_failed`**; no new error code.

`archived` is in that set deliberately: 0.20.0's "the seller always has a way
forward" has to keep working on a draft that never got as far as a category,
or the relaxation traps the row it just made possible.

`validate-draft` reports it the way the location gate already does — a
structured `FeatureValidationResult` under the slug `category_id`, carrying
that same `publish_validation_failed` code, so the composer renders it under a
named control and both doors say one word. With no category the feature-value
and unknown-slug checks are skipped (there is no schema to be unknown against)
while the price, location and description checks still run, so the seller gets
the whole list at once.

Three readers that assumed a category got a guard rather than a crash:
`events._base_payload` (`str(category_id or "")`, so no payload can ever carry
the string `"None"` past its schema), `_zero_price_result`, and the
re-projection pass, which now counts a category-less row under the existing
`category_unresolved` skip reason instead of handing NULL to the categories
seam. Search indexing needed no change and is pinned by test: a draft is not
in `INDEXED_STATUSES`, so no `listing.*` event is emitted for it at all, and
the document it would export carries `category_id: ""` with a `draft` status.

**Why a patch and not a minor.** The wire change is a relaxation in both
directions: a write field became optional, and a read field became nullable
for a row that *could not previously exist*. No response that was possible
before 0.21.4 changes shape.

## [0.21.3] — 2026-09-04

### Added — the Card badge contract, so a card stops printing «Кирпичный · 3 · 9»

A live apartment card summarised itself as **«Кирпичный · 3 · 9»**: three
answers with the questions missing. Nothing was wrong with the data — the
stored DAOs said «Тип дома: Кирпичный», «Этаж: 3», «Этажей в доме: 9» — and
nothing was wrong with the client either. `features_title` /
`features_badges` handed it a list of DAOs and the only obvious thing to print
was each one's value, so every client had to invent, per feature, whether that
value stands alone, needs a unit, or needs its question. Three clients, three
inventions.

The decision is now the server's, made once, in
`services/features.py`. **Every element of `features_title` and
`features_badges` carries four more keys on the wire** — nothing is renamed,
nothing stored is dropped, so a client reading `value` / `labels` as before
keeps working:

| key | what it is |
| --- | --- |
| `value` | unchanged — the stored value (term **codes** for a select) |
| `label` | the caption for that value: the write-time label snapshot for a select, the number for a number, the true-caption for a boolean |
| `unit` | the feature's unit when it has one — «м²», «эт.» |
| `name` | the feature's own caption — «Этаж» |
| `presentation` | which of four shapes to render |

**The rule — implement it verbatim.** `presentation` is one of exactly four
values, and a client renders the element by branching on it and nothing else:

| `presentation` | render | example |
| --- | --- | --- |
| `value` | the caption alone | «Кирпичный» |
| `value_unit` | caption, space, unit | «42 м²» |
| `name_value` | name, space, caption | «Этаж 3» |
| `name` | the name alone | «Балкон» |

and the server decides between them by:

- a **boolean** that is true → `name` — the feature's name IS the fact. A
  boolean that is **false is absent from the list entirely**: «Лоджия: нет» is
  noise on a card. This is the one element the contract removes rather than
  annotates, and the only behaviour change on the wire.
- otherwise, is the **caption** a bare number? Not the stored type — the
  **caption**. On a real catalogue `floor`, `floors` and `year` are
  vocabulary-backed, i.e. stored `ref_select` with `labels: ["3"]`, so a rule
  reading the type would file them under "dictionary value, print it alone"
  and reproduce the exact line this contract exists to delete. A caption made
  of several joined labels («1, 2») is never a number, whatever its parts
  look like.
  - numeric **with** a unit → `value_unit`;
  - numeric **without** one → `name_value`;
  - not numeric → `value`.

`label`, `unit` and `name` are translation keys **or** literals, exactly as the
catalogue wrote them — this module does not translate, and a client resolves
them the way it already resolves `name` today. `unit` is **absent**, not null,
when the feature has none. The unit is read from the DAO's own `postfix`
(stapel-attributes stamps it there from the config, so no schema fetch), or,
for a `convertible_unit`, as the `feature.unit.<code>.name` key with the
stored base-unit value converted to that unit. `postfix1000` is deliberately
ignored: it abbreviates the *value* («150 тыс. км» for 150000), and honouring
it would make the card and the detail table print different magnitudes for one
stored number. Headers, blank values and redacted stubs never reach a card.

Derived **on the way out** (`ListingCardFeaturesOutputField`), not at build
time: the contract is a pure function of the stored DAO, so every listing
already in the database gained it the moment this shipped — no re-projection
pass, no migration, and no fourth copy of the projection to keep in step.
`features` (the detail table) is deliberately NOT decorated: it already prints
a name beside every value, and it is also the owner's and moderation's view of
the raw stored DAO.

A gap this exposes rather than fixes lives in the catalogue, not here: a
category whose `floor` carries no `postfix` renders «Этаж 3» — correct and
unambiguous — and turns into «3 эт.» by itself the day the schema sets one.

## [0.21.2] — 2026-09-04

### Added — `draft_meta`, an opaque owner-only sidecar on the draft twin

0.21.1 gave the composer a way to reopen a listing and get its content back;
it did not give it anywhere to keep facts ABOUT that content. The storefront
composer's first tenant is per-field provenance — `{"title": "seller",
"description": "ai"}`, so an edited field can be marked back to "seller"
once a person touches AI-drafted copy — and this module has no opinion on
what the keys mean. `draft_meta` is a JSON object this module stores and
returns unread, the same posture `features_search` takes toward its own
per-slug keys, applied here to whatever the composer wants to keep.

- **Where it shows up**: written by `save-draft` (merged into the existing
  object, see below) and echoed back in its response; read back by owner's
  `GET …/draft/`, byte for byte the same value. Absent from every public and
  semi-public shape — the detail read, the card, the search-facing card, and
  the owner's own `my/listings` row — asserted over the whole key set the
  same way the `*_draft` twins already are (`tests/test_draft_readback.py`).
- **Merge, not replace**: a second `save-draft` call MERGES its `draft_meta`
  into what is already stored, one key at a time — `dict.update()` at the
  top level, not JSON-merge-patch — because the composer's calling pattern
  is one `save-draft` per edited field, and a whole-object replace would
  silently drop every provenance tag a previous call set. Sending
  `draft_meta: null` clears it outright. A composer that wants to drop one
  key sends the whole reduced object; there is no per-key delete verb.
- **Capped at `DRAFT_META_MAX_BYTES`** (default 16 KiB of UTF-8 JSON),
  checked against the value that would actually be STORED — after the merge,
  not just the incoming payload — so the cap cannot be walked past for free
  by spreading a large object across several small calls. Over the cap is a
  400 with `error.400.listing_draft_meta_too_large`, and nothing is written.
- **Kept on publish, not cleared**: unlike the `*_draft` twins,
  `draft_meta` has no published sibling and `publish_listing` /
  `restore_listing` do not touch it — a listing archived and reopened
  («Опубликовать снова», 0.21.1) keeps its provenance tags across the trip,
  which is the only shape a composer reopening a used-to-be-published
  listing can use.

Migration is additive: `blank=True, null=True, default=dict` on a new
column, no backfill — every existing row already satisfies the default.

## [0.21.1] — 2026-09-04

### Added — «опубликовать снова» on an archived listing (Д193)

A seller could file a listing away and then only take it back to DRAFT, which
means walking the whole composer again to sell the same thing twice. The
cabinet offered «Опубликовать снова» on a `sold` row and nothing on an
`archived` one, because that is exactly what the server advertised.

`ARCHIVED -> PUBLISHED` is now an owner edge, so it appears in the row's
`available_transitions` and the cabinet grows the button with **no client
change** — the button set has been derived from that field since 0.20.0.

It is not a lifecycle hop. `POST listings/{id}/transition/ {"to":
"published"}` from ARCHIVED runs the new
`services.publish.restore_listing()`, which is `publish_listing()` under a
name that says why:

- **re-moderation** — `listing.submitted` is emitted and `moderation_status`
  goes back to PENDING, so a fleet whose moderation is wired to publish
  reviews a restored listing exactly as it reviews a fresh one;
- **where it lands is policy's answer, not the route's** — PENDING under the
  default `MODERATION_GATE="pre"`, PUBLISHED under `"post"` or
  `AUTO_APPROVE_ON_PUBLISH`. The response carries the status the listing is
  actually in, which is not necessarily the one that was asked for;
- **no takedown laundering** — ARCHIVED is reachable from BLOCKED, so a bare
  `transition_to(PUBLISHED)` would have handed every taken-down listing a
  two-press route back into the index, around the gate that deliberately
  keeps `BLOCKED -> PUBLISHED` out of `OWNER_TRANSITIONS`;
- **the content is current** — draft twins are promoted, projections rebuilt
  and the TTL restarted, so a listing does not come back from six months in
  the archive already expired. An unpublishable draft is refused with the
  composer's own 400 rather than going back on sale blank.

### Added — `GET listings/{id}/draft/`, the read half of `save-draft`

Every user-editable field is a `*_draft` twin, and until now the only calls
that returned one were the WRITES (`create`, `update`, `save-draft` echo the
bag back). A composer reopening a listing by id therefore had one source, the
detail read, which serves the PUBLISHED fields — empty on anything never
published. The seller reopened a draft and got a blank form while their text
and photos sat in the row.

**The read that carries the draft twins is `GET
/listings/api/v1/listings/{id}/draft/`**, owner-only through `_get_own` (403
for someone else's, 404 for absent or soft-deleted), returning
`ListingDraftSerializer` — byte for byte what `save-draft` returns, so a
client reopens with the mapper it already has for the write's response.

Deliberately a separate route rather than draft columns on the detail read:
the detail read is the public one, and a shape whose key set depends on who
is asking is where a redaction bug hides. The public card and detail carry no
`*_draft` field, asserted over their whole key set; the owner's `my/listings`
card keeps exactly the three twins it had.

## [0.21.0] — 2026-09-03

### Fixed — the public listing card published the seller's front door

`ListingCardSerializer` and `ListingDetailSerializer` shipped, to a reader with
no credentials at all, `geohash` at precision 8 (a cell roughly 38m x 19m) AND
`lat`/`lon` at the column's full six decimal places (~11cm). For a private
person selling a sofa from home that is not "roughly where the sofa is" — it is
their address, printed beside their phone number on the same page, on a surface
whose whole design goal (0.20.x `test_public_read.py`) is that strangers and
crawlers read it without a session. It was live on a stand.

The public statement is now an **area**, and it is deliberately the same
statement the neighbouring search card already makes:

- `lat`/`lon` rounded to `PUBLIC_COORD_PRECISION` decimals — **2**, ~1.1km,
  the same width as stapel-search's `CARD_COORD_PRECISION`, so the two public
  surfaces of one listing disclose the same thing rather than one undoing the
  other.
- `geo_precision_km` on both payloads, so a reader knows what it is holding and
  draws a circle instead of a marker. `0` means an exact point.
- `geohash` **blanked, not truncated**. A truncated prefix is a second,
  differently-aligned area around the same true point, and the intersection of
  two areas is smaller than either of them: a prefix beside a rounded pair, for
  a listing sitting near a cell boundary, still pins it to a sliver tens of
  metres wide. One area, one encoding, nothing to intersect. `""` is a value the
  column already holds and every client already handles, so the public key set
  does not move.

**Nothing that legitimately needs the point lost it.** `FeatureVisibilityMixin`
already resolved anonymous/owner/staff for the VIN-and-IMEI axis; that one
resolver now gates coordinates too and the class is `AudienceRedactionMixin`
(the old name stays as an alias). So the seller reading their own listing, staff,
and the service transport still get the exact pin — which the composer needs,
because `fromDetail` in stapel-react's listings-react loads `lat`/`lon`/`geohash`
off this very read and would otherwise move the listing a kilometre on every
save. `services.search_feed` is untouched: the index keeps the true coordinates
and `distance_km` stays a server-side scalar computed from them, so proximity and
the two-band geo work are exactly as accurate as before. Coarsening the payload
costs a reader no accuracy — only the ability to locate the thing being sold.

- `PUBLIC_COORD_PRECISION` (default 2) — the one knob, documented as a privacy
  control in `CONFIG.MD`.
- `tests/test_public_read.py` — the anonymous payload's **exact key set**, frozen
  for both endpoints in the shape of stapel-categories' `external_id` contract,
  plus the precision of what those keys carry, plus a mixin gate: a serializer
  that lists a coordinate column without `AudienceRedactionMixin` fails a test
  that says why. The next leak will not be called `lat`.

## [0.20.0] — 2026-09-03

### Fixed — «нельзя поменять им статус, только удалить»

The owner's sentence about his own board, and it was true of the HTTP surface
rather than of any one row. `LISTING_TRANSITIONS` describes a lifecycle with
a way out of every state — ARCHIVED back to DRAFT, PAUSED to PUBLISHED,
EXPIRED renewed, SOLD relisted, REJECTED and BLOCKED reworked. The owner API
exposed **two** of those edges, `archive` and `complete`, and both are exits.
So every status a seller could put a listing INTO was a status they could not
get it out of, and `DELETE` was the only call left that still answered. One
live stand held 40 listings in exactly that position.

- `models.OWNER_TRANSITIONS` — the seller's half of the state machine,
  declared as a subset of the machine it belongs to (a test asserts it is
  one) rather than as a second table, because the pair would drift in the one
  direction that hurts: a card advertising a move the route then 409s on.
- `POST listings/{id}/transition/ {"to": ...}` — one route for every edge a
  seller owns, validated against that allowlist. `archive` and `complete`
  keep their URLs and now go through the same gate.
- `available_transitions` on the owner's card. `status` says where the
  listing is and `moderation_status` says what is being waited on; neither
  answers *what can I do about it*, so a cabinet had to re-derive a state
  machine it could not see. It is now reported by the server that owns it —
  the same list the route accepts, not a second opinion about it.

What is deliberately absent is load-bearing: `PENDING -> PUBLISHED` and
`BLOCKED -> PUBLISHED` are moderation's decisions and stay out of the
seller's half, so this is a way forward and not a self-service publish gate.

### Fixed — a draft nobody submitted no longer claims to be under review

`moderation_status` defaulted to `pending`, so every draft announced itself
as awaiting a moderation decision from the instant the row existed. A live
stand held 167 of them with not one moderation case behind any of them: the
cabinet was reading the column correctly and the column was saying the wrong
thing.

`ModerationStatus.NOT_SUBMITTED` is the new default. The distinction lives in
the data rather than in a presenter because every reader asks the same
question and would each otherwise re-derive "...unless it was never
published" from a second column. `publish_listing` sets `PENDING`
unconditionally, so the word is earned the moment anything is submitted.

Migration `0009` carries the existing rows, and its three predicates are each
load-bearing: `status='draft'` (any other status has been through publish),
`published_at IS NULL` (a restored draft HAS been submitted before), and
`moderation_status='pending'` (a real verdict is somebody's decision).
Reversible exactly — `not_submitted` is a value no earlier code could write.

### Upgrading

Consumers that compare `moderation_status` against `"pending"` to mean "this
listing has never been submitted" must read `"not_submitted"` instead;
consumers that meant "a decision is owed" are unaffected and now correct.

## [0.19.0] — 2026-09-03

### Fixed

- **`category_id` can only hold an ID.** Three drafts on one live stand (243,
  244, 245) carry `category_id = "32/149/163"` — a search PATH in the opaque
  category id column. A slash-joined ancestry is what `stapel-search` publishes
  for its `?category=` filter and what the storefront puts in the URL; it is
  the right value *there*. In this column it is a different kind of thing
  wearing the same type, and nothing could tell: the field was a bare
  `CharField` with no validators, `ListingDraftSerializer` had no
  `validate_category_id`, and the categories seam is consulted only at PUBLISH
  — so a draft carrying a path was written, stored and served without one
  reader ever asking whether the id was an id.

  `validate_category_id` is declared **on the model field**, not in the
  serializer. DRF inherits a model field's validators into
  `ListingDraftSerializer` automatically, the admin form runs them, and so does
  any `full_clean()` — and the Django admin, where `category_id` is editable
  and not read-only, was one of the two writers that could not be ruled out for
  the three stand rows. A serializer-only check would have left that door open.

  The pattern is deliberately narrow (`[A-Za-z0-9][A-Za-z0-9_-]*`): int-like
  ids and UUIDs pass, anything with a separator does not. A separator that
  turns out to be legitimate somewhere fails loudly and is a one-line change; a
  path that gets in fails silently and costs a repair run. **If a surface needs
  a path, it needs its own field.**

  Migration `0008_alter_listing_category_id` — validators only, no column
  change. Existing rows are NOT rewritten: a stored path stays until it is
  repaired, and `full_clean()` is where it now fails.

  Breaking for any client that was sending a non-`[A-Za-z0-9_-]` category id.
  Note for the pair: `stapel-react`'s `listings-react` demo and test fixtures
  still assert that `category_id: "tools/power"` round-trips — that assertion
  is now false against a real server (see the follow-up note in the wave
  report).

- **`listings_reproject_features` can now BUILD a missing projection, not only
  refresh a stale one.** The population was `exclude(features=[])` — rows that
  already HAVE projections — so the pass was keyed on its own output. A listing
  carrying a perfectly good `features_draft` and no projection was not merely
  left unrepaired: it was never examined, so no run ever named it in any
  report. Measured on one live stand as **14 listings with empty
  characteristics that survived every repair run**. The population is keyed on
  the INPUT now (`HAS_DRAFT`), and `changed` is split into `built` and
  `refreshed` so the two kinds of work are countable apart.

  Rows carrying a projection and NO draft stay in the population and stay
  reported as `no_draft`: a snapshot with no source is damage, and narrowing to
  `HAS_DRAFT` alone would have traded one silence for another.

### Added

- **`no_attributes` — the third state, counted instead of passed over.** Rows
  in scope with neither a draft nor a projection are reported as a number (one
  aggregate, not a walk), so `examined + no_attributes` is every listing the
  run was responsible for. That invariant is the mechanism, not decoration:
  the original defect survived because rows outside the population produced no
  line in any report, and a population that silently narrows again now shows up
  as a total that stops adding up.

## [0.17.1] — 2026-09-03

Patch. Cap only: `stapel-attributes` admits 0.9.

stapel-attributes 0.9.0 changes one rule semantic — a VALUE predicate (`in` /
`not_in`) no longer matches a controller that reads EMPTY, so a
`require when X not_in […]` rule stops firing before anyone has answered `X`.
Two UX walkers had hit that wall on an imported catalogue: a field starred and
refusing "Next" while its own help line said it was needed only *if* another
field said so, with that field untouched.

This module EVALUATES rules — the publish gate reads `RuleState.required`
through `services/features.py` — so the new semantic reaches it directly: a
draft that used to be refused over a field whose stated precondition had not
happened now publishes. That is the release's whole point. The suite is green
against 0.9.0 with no edit, so this is a cap-only patch.

## [0.17.0] — 2026-09-03

### Added

- **`GET listings/engagement?ids=…`** — the per-viewer overlay for a whole
  page of cards in one call, the HTTP twin of the `listings.engagement` comm
  Function. A storefront's grid is served by the SEARCH index, whose stored
  card can carry neither a flag that differs per reader nor a counter that
  moves faster than a document re-indexed on a listing event; without this
  endpoint the flags shipped in 0.16.0 reach the listings REST card and stop
  short of the one grid a buyer actually looks at. `AllowAny`, because
  `view_count` is public and the two per-viewer flags answer `null` for a
  guest — so the storefront makes the same request signed in or not, and a
  guest's grid is not a second code path. Capped by
  `ENGAGEMENT_BATCH_LIMIT` (100).

## [0.16.1] — 2026-09-03

### Fixed

- Re-cut of 0.16.0. Its tag was created before the pre-push exposure sweep
  and ended up on a commit that is not on `main` — the code is identical, but
  a release whose tag does not build from the branch is not a release. Pin
  0.16.1.

## [0.16.0] — 2026-09-03

### Added

- **View counting.** Opening a listing counts a view — once per (viewer,
  listing) per `VIEW_DEDUP_WINDOW_SECONDS`, never for the listing's own owner,
  and never at all for an open that cannot be attributed to anybody. The
  dedup cache IS the buffer: every repeat open inside the window costs one
  cache read and touches no database
  (`test_a_repeat_open_writes_nothing` counts the queries). Anonymous viewers
  are deduplicated by session key, falling back to a hashed IP+User-Agent
  fingerprint that is declared coarse rather than dressed up — the count is a
  floor, never an invented audience. New: `Listing.view_count`, the
  `ListingView` table (authenticated viewers only — see its docstring for why
  a row per anonymous session would be the legacy `UserAdView` read-cache
  again), `ListingQuerySet.with_viewed`, `services.engagement`.
- **The engagement flags reach the wire.** `view_count`, `viewed` and the
  already-existing `is_favorited` are now on BOTH the card and the detail
  serializer. `viewed` and `is_favorited` are three-state — true / false /
  null-for-anonymous — because unknown is not the same sentence as false.
- **`listings.engagement`**, a keyed batch read of that same overlay. It
  exists because a SERP's cards come out of the search index, which can carry
  neither a per-reader flag nor a counter that moves faster than a document
  re-indexed on a listing event. One call per page, never one per card.
- `stapel_listings.W001`: the default cache is a per-process LocMemCache. View
  deduplication lives in it, so with more than one worker one viewer's single
  open is counted once per worker and the counter is quietly a multiple of the
  truth.

### Changed

- **BREAKING (Д71): a listing with no coordinates no longer publishes.**
  `REQUIRE_LOCATION_ON_PUBLISH` defaults to true, next to
  `REQUIRE_IMAGE_ON_PUBLISH` and for the same reason. «Где находится» was
  optional, so listings went live outside every radius filter and every map,
  findable only by scrolling past them, with nothing anywhere saying why. The
  predicate is the COORDINATES, not the label: the label is a string a client
  sends, and only the coordinates reach the geographic surfaces. The refusal
  is a structured, per-field `ERR_400_LOCATION_REQUIRED` under the slug
  `location` — the composer renders it under the control — and `publish` and
  `validate-draft` read one predicate so they cannot drift apart. Hosts with
  no geographic dimension set the switch to false.
- **BREAKING (Д76): the published `location_label` is derived, not echoed.**
  It was a writable draft field: the composer's map picker posted the
  geocoder's `formatted` line — POI, street, house number, because that is
  what a picker's confirmation line is FOR — and every card on the stand read
  «ул. Тверская, д. 7, корп. 2, Москва, Россия», so no two rows in a grid
  could be told apart by it. It was also a free advertising slot on a public
  card, since nothing stopped a seller from posting a phone number there. On
  publish the label is now resolved from the pin through
  `GEO_REVERSE_FUNCTION` as «City, District» (county backs up city for a
  settlement outside one). Fail-soft: a dark geocoder leaves the supplied
  string in place — publication does not depend on a network call. The draft
  twin stays writable and stays the picker's line.

## [0.15.0] — 2026-09-02

Minor (breaking: `price` is nullable, and any status write announces itself).

### Fixed — a status write that skipped the FSM changed visibility silently

`listing.status = "archived"; listing.save()` — an orchestrator's raw write,
a shell one-liner, a management command — moved a listing out of every
public read while every search index kept serving it. Six listings on a
client stand were archived that way and stayed in the SERP as ghost cards
whose click answered «Объявление снято с публикации».

`transition_to` was never the problem: it emits correctly and remains the
front door (it validates the edge and owns its own emit). The defect was
that it was the ONLY door. `Listing.save()` now carries the index-boundary
detector itself: a save that may write `status` compares the instance
against the stored row and emits `listing.published` on entering
INDEXED_STATUSES, `listing.removed` on leaving it, exactly once, inside the
same `mutate_and_emit()` block as the write. A move entirely inside or
entirely outside the boundary still emits nothing. Entering the index
re-derives `features_search` first, the same as `transition_to`.

`restore()` gained the missing counterpart: `delete()` announced the leave,
so an undelete of a still-published row now announces the way back in.

A queryset `.update(status=...)` bypasses the model layer entirely and
always will; that hole is closed from the other side by stapel-search
0.10.5's `search_reconcile` sweep, not by pretending it cannot happen.

### Fixed — an empty price published as «0 ₽»

`price` was `default=0`, so "the seller skipped the field" and "this item is
free" were the same value, and a marketplace card advertised a free iPhone.
NULL is now the honest spelling of "price not stated": `price` is
`null=True, default=None` (migration 0006), `publish_listing` promotes
`price_draft` unconditionally so a cleared price really clears, and
`validate_draft` REJECTS an explicit 0 — `error.400.listing_zero_price_not_allowed`
— unless the category is named in the new `FREE_PRICE_CATEGORY_IDS` setting.
`price_base` follows (`compute_price_base` already returned None for a NULL
price), and `listings.search_documents` / `listings.search_export` emit both
as JSON `null`, so a storefront can render «Цена не указана» from the DTO
instead of inventing a zero.

The migration deliberately rewrites no data: an existing 0 cannot be told
apart from a genuinely free item by a migration, so the decision stays with
the host.

## [0.14.1] — 2026-09-02

Patch (additive). `python manage.py listings_backfill_cdn_refs` — the
one-time pass 0.14.0 was missing: rows written before claim-on-save are
zero-ref on the CDN side, and a scripted re-save would NOT claim them,
because `Listing.save()` only announces when the claimed set MOVES (it
diffs the stored row against the write; an unchanged listing publishes
nothing — by design, that is what keeps draft autosaves quiet). Without
this pass the orphan sweeper would reap every pre-0.14.0 listing's photos:
exactly the outcome the claim mechanism exists to prevent.

The command publishes one ADDITIVE claim per live listing that references
media — `old_hashes=[]`, so `apply_ref_sync`'s remove set is empty by
construction: idempotent, rerunnable, never releases anything, safe over
rows the save-path already claimed. Soft-deleted rows are skipped on
purpose (a deleted listing claims nothing — the 0.14.0 contract). A failed
bus publish is counted and reported, not raised; the command warns loudly
when nothing at all got through. `--dry-run` counts, `--limit` slices.
Run it before the sweeper (stapel-cdn 0.19.0) deploys.

## [0.14.0] — 2026-09-02

Minor (a new outbound surface). Listings now CLAIM the CDN media they show:
every change to the set of referenced photos is announced on the
`stapel.cdn.ref-sync` bus topic (`stapel_core.django.cdn.ref_sync
.sync_cdn_refs`, the same helper stapel-profiles already uses for avatars),
so stapel-cdn's orphan sweeper (arriving in 0.19.0 there) never reaps a live
listing's photos — and a photo dropped from a draft becomes unclaimed and is
reaped.

### What is claimed

The `<type>/<hash>` strings `images` and `images_draft` store verbatim (they
ARE the CDN ref form `apply_ref_sync` resolves — the upload bag wrote them to
that contract). The claimed set is the **union** of both sides: an edit that
drops a photo from the draft must not release it while the published listing
still shows it; the ref leaves the claim only when a publish promotes the
draft over it (or the draft change is the only place it ever lived). A
deleted listing — soft or hard — claims nothing; `restore()` re-claims.

### Where the sync lives

`Listing.save()` (baseline read of the stored row before the write, one
announce after it) and `hard_delete()` — the model layer, so EVERY writer is
covered: the draft serializer, autosave, `publish_listing`'s promotion, soft
delete, restore, and GDPR erasure all funnel through those two methods; no
second serializer-shaped call site to drift. The entity key is
`listings/listing/<pk>`.

### Graceful, like the geohash stamp (0.7.1)

A bus failure never blocks the listing write: the helper already degrades a
failed publish to `ok=False` + a warning (Kafka replays once CDN catches up),
and anything raised anyway is caught and logged at the call site — media
bookkeeping must never fail a save or a delete.

## [0.13.3] — 2026-09-02

Patch (compatible: the default is exactly the old behavior). Post-moderation
becomes a policy a deployment can actually choose:
`STAPEL_LISTINGS["MODERATION_GATE"] = "pre" | "post"`.

### Pending-forever limbo on moderator-less deployments

Strict pre-moderation is only a working policy where a moderator exists to
answer. `publish` put every first publication in `pending`, emitted
`listing.submitted`, and waited for the `moderation.completed` verdict that
is the ONLY thing that moves a listing to `published`. On a stand with no
moderator that verdict never comes, so every listing ever published hung
invisible — not indexed, not in any public read, and with nothing in the
system that would ever move it. stapel-moderation has declared a per-target
`gate: "pre"|"post"` policy key all along (its `GATE_DEFAULT` is even
`"post"`), but nothing consumed it: the strictest gate was hard-coded here
regardless of what the policy said.

### The `post` gate

Under `MODERATION_GATE="post"` (the big-board model) the same publish flow also
transitions the listing to `published` — inside the same `mutate_and_emit()`
block as the promotion and the `listing.submitted` emit, so the go-live, the
index announcement (`listing.published`, via `transition_to`) and the
moderation request commit together or roll back together. What it is NOT:

- **not a verdict** — unlike `AUTO_APPROVE_ON_PUBLISH`, nothing calls
  `apply_moderation("approved")`. `moderation_status` stays `pending`, the
  case still opens, and review still happens — on the live content;
- **not a change to the takedown** — a rejecting verdict lands on the
  `published` → `blocked` edge that live-edit re-moderation (0.5.0) already
  uses, emitting `listing.removed`; an approving verdict touches only the
  moderation axis (no spurious lifecycle transition, no index churn).

`"pre"` stays the default and is byte-for-byte the old behavior. Values
outside the pair are `stapel_listings.E001` at boot — a misspelling would
silently behave as `"pre"`, which is precisely the limbo this key exists to
end. The composite holds the two halves of the policy together:
stapel-classified 0.8.2 fails the boot (`stapel_classified.E004`) when this
key and the moderation registry's `gate` for the listing target disagree.

## [0.13.2] — 2026-09-02

Patch. Reverts 0.13.1's floor, and corrects what 0.13.1 said.

**0.13.1's changelog was wrong.** It claimed stapel-core 0.51.0–0.53.0 shipped
wheels missing `stapel_core.django.sites` and raised the floor to `>=0.54.1`
to exclude them. The published wheels were checked afterwards and all of them
contain the module. Nothing on PyPI was ever broken, and this floor had no
reason to move; it goes back to `>=0.26.0`.

What really happened: core's main briefly carried a `pyproject.toml` whose
`[tool.setuptools] packages` list had lost the `stapel_core.django.sites`
line to a rebase conflict resolution. It was tagged as core 0.54.0, caught by
core's own CI, never published, and fixed in 0.54.1. Siblings whose CI builds
core from **git main** rather than PyPI failed at `django.setup()` while it
was there. That is a real failure with a real cause, and it is not this one.

The diagnosis jumped from "a wheel is missing a module" to "the published
wheels are missing a module" without checking a published wheel. The check
takes one `pip download`.

## [0.13.1] — 2026-09-02

Patch. Raised `stapel-core` to `>=0.54.1`. **Superseded by 0.13.2 — its stated
reason was incorrect; see that entry.**

## [0.13.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible).
`listings_reproject_features` repairs what it can instead of what it can
prove entirely.

### One bad field cost a listing its whole repair

The pass validated each listing's draft with `validate_dto`, which raises one
error for the whole draft. So a single attribute that had drifted out of its
category's bounds — a `max` tightened under a row published years earlier —
skipped the listing entirely, and every OTHER field on it kept whatever an
older engine had left there: `select` DAOs with no `labels`, cards printing
`Condition: b-u` at people. The two fields have nothing to do with each
other. Measured on a client fleet: **12 listings stuck on a stale
`box_sealed` shape**, none of them stuck for a reason that had anything to do
with the fields that were broken on screen.

### Per-field repair

`validate_dto_structured` answers per field, so the pass now does too:

- every field the current schema accepts is re-projected;
- every field it rejects **keeps its stored DAO** and is named in the report.
  Not dropped — dropping it would delete the attribute from the card, turning
  a stale value into a missing one and making the repair a regression for the
  one field it could not fix. A preserved DAO is stale, and it was already
  stale before the run;
- the merged list is re-sorted by `order`, and `features_title` /
  `features_badges` / `features_search` are derived **from the merged list**
  (`build_projections_from_list`), not from the fresh half — otherwise the
  three would quietly disagree with `features` about which fields exist.

`services.features.build_projections_partial` is the mechanism, beside
`build_projections` so "what the projections are" keeps one definition.

### The report is per listing, per field, with the reason

```
  3 field(s) on 2 listing(s) could not be re-derived and KEPT THEIR STORED VALUES
  (2 of those listings were still repaired in their other fields):
    listing 41 [mileage]: Value must be <= 10
```

A repair that hides what it worked around is how a catalogue rots quietly:
the run goes green, the numbers improve every time, and the fields nobody can
fix are never named to anybody. Every one is also logged individually, so the
bounded in-memory sample never hides one.

### Exit codes

- **0** — anything was repaired, including a run where some fields were
  worked around. That is the whole point of the change; failing over a
  worked-around field would put back the all-or-nothing gate this removes.
- **non-zero** — the pass repaired *nothing* it was asked to repair
  (`repair_failures()`: `category_unresolved`, `draft_invalid`,
  `projection_failed`). `no_draft` is deliberately **not** in that set — a row
  carrying projections with no draft is a row this pass does not apply to, not
  damage, and exiting non-zero over it would make the command red on healthy
  catalogues.
- **`--strict`** — non-zero on any field that could not be re-derived, for CI
  and monitoring that want the stricter reading.

### Also

- `--batch-size` below 1 is now a `CommandError` instead of a silently empty
  run.
- `draft_invalid` narrows to the only case that is still un-repairable per
  field: a draft that is not an object keyed by slug, or a category whose own
  `rules` break the grammar so no field can be judged at all.

## [0.12.1] — 2026-09-02

Patch. A floor that stated less than it needed.

`tests/test_feature_visibility.py` passes `ignore=` to
`stapel_attributes.guard.assert_raw_access_confined`, which arrived in
stapel-attributes **0.8.1**, but the pin said `>=0.8`. CI resolved 0.8.0 and
the reach gate failed with a `TypeError` — the gate itself worked, the
declaration of what it needs did not. Floor raised to `>=0.8.1,<0.9`.

## [0.12.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible). A feature value the
catalogue marked non-public no longer leaves this module to a reader without
the entitlement. Requires `stapel-attributes>=0.8`.

### Fixed

- **An anonymous read no longer carries an identifier attribute's value.**
  On the live stand, `GET /listings/api/v1/listings/287/` answered a stranger
  with the car's VIN in `features`, and the same string sat in
  `features_search`, where `?f.vin=<value>` made it an oracle: the index would
  confirm that this exact listing is that exact vehicle. A VIN and an IMEI
  identify a specific physical unit, so publishing one lets a stranger act as
  its owner.

### Added

- **`serializers.FeatureVisibilityMixin` — the read-time chokepoint.** It
  resolves the audience for the row actually being rendered (service
  transport and staff read as staff, the row's owner as owner, everyone else
  as anonymous) and redacts `features` / `features_title` / `features_badges`
  through `stapel_attributes.visibility.redact_daos`. A stored DAO carries its
  own `visibility` stamp, so this needs no category fetch on the read path —
  which is the only reason it can live here at all.

  It **fails closed**: no request in the serializer context resolves to
  anonymous. `my_listings` and `my_favorites` build their serializers by hand
  and now pass `get_serializer_context()`, without which the mixin would have
  redacted a seller's own VIN out of their own dashboard.

  A hidden row is kept in `features` as a value-free stub — `redacted: true`,
  `present: <bool>`, no `value` — rather than dropped, so the public attribute
  table has the same rows in the same order as the seller's and a buyer can
  see that a VIN exists and was supplied. `present` is a fact this system
  observes; `verification` is a claim about the outside world that nothing in
  the fleet makes today, so a client may render «указан продавцом» and must
  not render «проверен». The wire shape is in `docs/schema.json` as
  `RedactedFeatureDao`.

### Changed

- **Three of the four projections are built without the value at all.**
  `services.features` keeps a non-public DAO out of `features_title`,
  `features_badges` and `features_search` at write time. Those columns are
  read raw — by every card, by `services.search_feed.build_search_document`
  and by the `listing.published` / `listing.updated` bus payloads — and none
  of those readers has a viewer or a schema in hand, so the only way they can
  be safe for everyone is for the value never to enter them. `features` keeps
  everything; it is the column the mixin redacts per viewer.

- **The document builder and the two bus payloads filter again on the way
  out** (`search_feed.hidden_slugs`, `events._public_features_search`). On a
  freshly projected row this removes nothing. It is there for the rows
  projected before the axis existed — which is the entire installed base on
  the day this ships — because an indexer that pulled one in the meantime
  would keep serving the value as a filterable term long after the
  re-projection.

### Migration

**Existing rows keep the value in their public projections until they are
re-projected.** Nothing is fixed by deploying alone:

```
python manage.py listings_reproject_features --category <id>
```

It re-runs the projections against the current category schema, stamps
`visibility` onto the stored DAOs and rebuilds the title/badge/search columns.
It does not touch lifecycle, moderation status or `updated_at`; it does emit
`listing.updated` per indexed row, so an indexer picks up the cleaned document.
After it, the search index still holds the old terms until the consumer
reindexes.

### Tests

`tests/test_feature_visibility.py` — 23 tests in three kinds. Behavioural ones
prove today's payloads are clean. A **structural** one enumerates every
serializer in the module that emits a feature column and fails if one does not
inherit the mixin. A **reach** one (`stapel_attributes.guard`) fails if the raw
columns are read anywhere outside eight named files. The last two are the point:
the original leak was not a wrong redaction rule, it was a plain `JSONField`
that every new serializer inherited the disclosure from.

## [0.11.1] — 2026-09-01

Patch. One test comment — no code, schema or API change.

The comment introducing `RULE_FEATURE_DEFS` in `tests/test_publish_rules.py`
named the external marketplace whose catalogue import produces that schema
shape. It now names the shape instead: a controlling select, a conditionally
required sibling, a conditionally hidden number, and an option forbidden by
the same control.

## [0.11.0] — 2026-08-31

### Fixed — a card printed the storage slug where the copy belonged

`stapel-attributes>=0.6,<0.7` → **`>=0.7,<0.8`**.

The projections this module writes at publish time *are* the DAOs: a card
renders `features_badges` and a detail page renders `features` without ever
fetching the category, which is the entire reason those columns exist. Up to
stapel-attributes 0.6 a stored `select` DAO carried its option values and
nothing else, so every one of those readers printed the storage slug at a
person:

```
Condition: b-u
Sensors: gps
Screen condition: bez-defektov
```

Nothing downstream could repair it. The copy was lost at *write* time, and the
projection is deliberately the only thing a reader has.

stapel-attributes 0.7.0 fixes the engine — `SelectFeatureType.dto_to_dao` now
snapshots the chosen options' `label` into `SelectDao.labels`, positionally
aligned with `value`, exactly the way `RefSelectDao` has carried `labels`
since 0.5 — and this release floors on it. **The floor moves with the cap**,
as it did in 0.10.2 and for the same reason: a deployment able to resolve back
onto 0.6 is a deployment whose listing projections silently lose their display
copy again, on the next publish, with nothing red anywhere to say so. A host
that stays on stapel-attributes 0.6.x stays on stapel-listings 0.10.3.

`docs/schema.json` is regenerated: `FeatureDao`'s `select` variant now names
`labels`.

### Added — `listings_reproject_features`, because a snapshot goes stale

```
python manage.py listings_reproject_features [--category ID[,ID...]]
                                             [--batch-size N] [--dry-run]
```

Fixing the engine does not fix the rows. Every listing published before
attributes 0.7.0 still carries `select` DAOs with no `labels` and keeps
printing slugs until *something* re-projects it — and until now nothing could,
because the four projections have only ever been written by `publish_listing`.
The same staleness has always applied to `ref_select`'s label snapshot and to
any category whose option copy an owner edits after the fact; this is the
general repair, not a one-off backfill.

The command re-derives `features` / `features_title` / `features_badges` /
`features_search` from each listing's stored `features_draft` and the
**current** category schema. It does so through
`services.features.build_projections`, which is new only in the sense that it
is now *named*: `publish_listing`'s projection block moved into it wholesale
and both call it. One definition of what the projections are, or the refreshed
snapshot and the freshly published one are free to disagree.

**It is not a re-publication.** `status`, `moderation_status`,
`moderation_note`, `expires_at`, `published_at`, `created_at` and `updated_at`
are untouched (the write is a `save(update_fields=[…four columns])`), and no
`listing.submitted` is emitted — an owner's listing does not go back through
moderation because we fixed the rendering of a value already approved.

**`listing.updated` is emitted, on purpose.** A search index holding the stale
text is precisely the damage being repaired, so leaving the index alone would
fix the half nobody looks at. That is why the run writes row by row through
`Listing.save()` — which raises the event itself, in its own transaction, for
an indexed row whose content actually moved — instead of the faster
`bulk_update`, which would emit nothing. The count is in the summary
(`events_emitted`) rather than left to be inferred.

Idempotent (a row whose projections would not move is not written, so a second
full run reports zero changes and emits nothing), chunked through
`.iterator(chunk_size=…)`, and it prints numbers instead of the word *done*:
examined / re-projected / already current / skipped, with the skips broken out
by reason and their listing ids named. A row is skipped, never dropped
silently and never fatal to the run, when its category no longer resolves
(`category_unresolved`), its stored draft no longer validates against the
current schema (`draft_invalid` — the same policy `publish_listing` applies),
it has projections but no draft to re-derive them from (`no_draft`; projecting
an empty draft would *erase* the listing's attributes), or the projection
raised anything else (`projection_failed`). Soft-deleted rows are outside the
population: they render nowhere and have already announced their
`listing.removed`.

## [0.10.3] — 2026-08-31

### Fixed — a geocoder's precision is not a client error

`lat_draft`/`lon_draft` are `DecimalField(max_digits=9, decimal_places=6)`, and
DRF refused anything more precise. Every geocoder answers in whatever precision
its source carried — Photon in seven places, a phone's GPS in fourteen — so a
seller who picked an address from the suggestions got

```
400 POST /listings/api/v1/listings/187/save-draft/
    "lat_draft": "55.7505412"
    {"error": "Ensure that there are no more than 6 decimal places.",
     "params": {"field": "lat_draft"}}
```

on every attempt, and the listing could not be filed at all. Deterministic,
and reproducible in two lines on a clean API.

The seventh decimal place of a latitude is **eleven centimetres**, and nothing
downstream can tell the difference: the geohash is computed from the stored
value and search boxes it. So the boundary rounds instead of refusing. The new
`CoordinateField` quantizes to the column's own precision — read off the model
field, so a migration that widens the column widens what the API accepts — and
does it before `validate_precision`, which is the only reason it is a field
subclass rather than a `validate_<field>` method: DRF raises inside
`to_internal_value`, before any per-field validator sees the value.

Bounds are untouched: a longitude of 1000 is still a wrong answer, and money is
still money — `price_draft` refuses a third decimal place exactly as before,
because dropping a digit there changes what somebody is charged.

## [0.10.2] — 2026-08-31

### Fixed

- **`stapel-attributes>=0.5,<1.0` → `>=0.6,<0.7`.** 0.10.1 regenerated the
  schema against stapel-attributes 0.6 — `FeatureDto`/`FeatureDao` now name
  `group` — while still declaring a floor of 0.5, where that type does not
  exist. `test_feature_dto_dao_discriminator_is_slug_keyed` compares the
  committed mapping against the LIVE registry, so on 0.5 that release fails its
  own suite: a contract naming a type the installed engine cannot serve is the
  same defect as a contract missing one, pointing the other way.

  The cap is `<0.7` for the reason the floor moved at all: pre-1.0 house semver
  reads a minor as breaking, and an unbounded cap is what let a sibling's
  release start failing this module's tests with nothing here having changed.
  A host that stays on stapel-attributes 0.5.x stays on stapel-listings 0.10.0.

## [0.10.1] — 2026-08-31

### Changed — the composite `group` joins the polymorphic contract

stapel-attributes 0.6.0 registers a thirteenth builtin type. The published
OpenAPI schema names every one of them: `FeatureDto`/`FeatureDao` are
`oneOf` + `discriminator.mapping`, built from the live type registry, so a
thirteenth slug the committed `docs/schema.json` does not mention is a
contract that has stopped describing what this service accepts —
`test_feature_dto_dao_discriminator_is_slug_keyed` is the gate that says so,
and it went red the moment the floor resolved onto 0.6.

`docs/schema.json` regenerated: `GroupDto` / `GroupDao` and their two mapping
entries. No code changes — the composite rides the same
`coerce_feature_defs` -> `validate_dto` path every other kind does, and its
value never reaches `features_search`, `features_title` or `features_badges`
as anything but the list of rows the engine produced.

The dependency floor stays `stapel-attributes>=0.5,<1.0`: nothing here needs
0.6, and a host on 0.5 simply has twelve types and a schema that names
thirteen.

## [0.10.0] — 2026-08-30

**Minor = breaking** (pre-1.0): publishing behaves differently for a schema
that carries `rules`, and the floor moves to `stapel-attributes>=0.5,<1.0`.

### Changed — requiredness on publish is the rule state, not `mandatory`

stapel-attributes 0.5.0 runs a rule pre-pass over the submitted values before
every per-feature check. Two consequences land squarely on this module's
publish path, and neither is visible in code here, because the rules ride
inside the `categories.features` payload:

- **A field can be required conditionally.** `screen_condition` with
  `mandatory: false` and a `require when condition in ["b-u"]` rule now blocks
  publication exactly as a statically mandatory field does — with the same
  `mandatory_missing` code, on the same slug, through `validate_draft` and
  through `publish_listing` alike. Anything that read `mandatory` as the whole
  answer to "must this be filled" was reading half of it.
- **A field the rules hide is dropped, not merely unvalidated.** A hidden
  answer left in `features_draft` — filled before the control moved, or by a
  client that never filtered — no longer reaches `features`, and therefore no
  longer reaches `features_search`. The draft keeps it; the published listing
  does not show an attribute its own schema says does not apply to it.

A rule violation adds no error vocabulary: `forbid_option` narrows the config
before `parse_config`, so `select` answers `not_in_options` itself, and
`limit` surfaces as `above_maximum` / `below_minimum`.

`get_feature_configs` was already a pass-through — it hands the
`categories.features` dicts to `coerce_feature_defs` untouched — so the six
new `FeatureDef` keys (`rules`, `description`, `example`, `default`, `hints`,
`group`) arrive intact with no change. That is now a test rather than an
accident: a whitelist anywhere on that path would silently disarm every rule.

### Added — vocabulary-backed features in the four projections

`ref_select` / `ref_hierarchical_select` store term **codes** in `value` and a
`labels` snapshot taken at write time. The projections already keep the two
apart, and that split is now pinned:

- `features_title` / `features_badges` are the DAO itself, so a ref value
  travels with both halves and display never re-reads the vocabulary.
- `features_search` takes `value` — the codes. The two types are declared in
  `_LIST_VALUE_TYPES` instead of falling through the unknown-type branch,
  because a label changes with the vocabulary's language and a stored filter
  must not stop matching on translation.

### Fixed
- `test_feature_dto_dao_discriminator_is_slug_keyed` asserted a hard-coded
  count of ten registered attribute types. It broke on an upstream release
  that adds a type — a failure about arithmetic, not about the staleness the
  test exists to catch. It now asserts set equality against the live registry
  (which it already did) and merely that the registry is non-empty.

### Contract
- `docs/schema.json` gains `RefSelectDto`/`RefSelectDao` and the
  `ref_hierarchical_select` pair in the `FeatureDto`/`FeatureDao`
  discriminators; `docs/errors.json` gains
  `error.400.feature_invalid_rules`. No serializer of this module changed.

## [0.9.2] — 2026-08-30

### Fixed — a malformed id in an action payload was a poison pill

`ValidationError` is not a `ValueError`. Django answers a key it cannot coerce
to a column's type — a malformed UUID above all — with
`django.core.exceptions.ValidationError`, which does **not** subclass
`ValueError` or `TypeError`. The `user.deleted` / `user.merged` guards here
caught only `(ValueError, TypeError)`, so a bad id walked straight through
them, the handler raised, `consume_actions` re-raised to the bus, and the
event came back forever: a redelivery loop over a payload no retry can repair,
burning the consumer's retry budget while looking exactly like a downstream
outage.

The consumed contracts do not save anyone from this. They type an id as
`{"type": "string"}` — and where they do say `format: uuid`, `jsonschema`
does not enforce `format` unless a format checker is passed, which the comm
registry does not do. A malformed id is a well-formed payload.

Every guard that turns a payload id into rows now catches `ValidationError`
alongside `ValueError`/`TypeError` and takes the same quiet path it always
took for an id it has never seen: `handle_user_deleted` (previously
unguarded — the erasure call went straight at the GDPR provider),
`handle_user_merged`, and `handle_moderation_completed`'s listing lookup.

`user.merged` had a second door: the *from* id was probed under the guard but
the survivor probe, `user_model.objects.filter(pk=into_user_id)`, sat outside
it, so a malformed *into* id still escaped whenever the guest genuinely owned
rows. That read moved inside the guarded block — still before the first write,
so the "survivor not projected yet" path can no more leave rows half-moved
than it could before.

`MergeTargetNotReady` is untouched: a survivor id that *parses* but has no row
here still raises, because that one is a real ordering lag and redelivery does
fix it.


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
