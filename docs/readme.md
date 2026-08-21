## What this is

`Listing` is the marketplace core: an owner, an opaque category, typed
attribute values, a two-machine lifecycle + moderation status, a publish
pipeline, and first-class favorites. It **consumes** stapel-categories (feature
schema, over comm) and **stapel-attributes** (value validation), and stays
decoupled from search, moderation and currencies (see the boundaries below).

## Quick start

```python
INSTALLED_APPS = [
    # ...
    "stapel_listings",
]

# urls.py — this module's own urls.py bakes in only `v1/`; the host
# contributes `api/`, giving the canonical `/listings/api/v1/...` prefix.
path("listings/api/", include("stapel_listings.urls"))
```

Requires a `categories.features` comm Function provider (stapel-categories) for
value validation against a category's schema.

## Settings

All configuration lives in the `STAPEL_LISTINGS` namespace (dict setting, flat
setting, or env var — resolved lazily). Full table with seam semantics in
[MODULE.md](https://github.com/usestapel/stapel-listings/blob/main/MODULE.md).

| Key | Default | Meaning |
|---|---|---|
| `CATEGORY_FEATURES_FUNCTION` | `"categories.features"` | comm Function resolving a category's feature schema. |
| `PRICE_BASE_CONVERTER` | identity | Dotted-path `(amount, currency, base) -> Decimal`. |
| `AUTO_APPROVE_ON_PUBLISH` | `False` | Publish immediately when no moderation module is installed. |
| `REQUIRE_IMAGE_ON_PUBLISH` | `True` | Require ≥1 image to publish. |
| `MODERATION_TARGET_TYPE` | `"listing"` | `target_type` this module answers to in `moderation.completed`. |
| `LISTING_URL_TEMPLATE` | `""` | Public URL template (`{listing_id}`) for the moderator's card. |
| `DEFAULT_LISTING_TTL_DAYS` | `30` | Days until a published listing expires. |

## comm surface

Emits (Actions): `listing.submitted` (moderation boundary),
`listing.published` / `listing.updated` / `listing.removed` (search boundary).
Consumes: `category.changed`, `moderation.completed`, `user.deleted`.
Provides Functions: `listings.status`, `listings.search_documents`,
`listings.search_export`, `listings.moderation_content`.
Calls: `categories.features`.

**Boundaries:** search/filtering is a separate **stapel-search** module; this
module builds `features_search`, signals with the `listing.*` events and hands
over the document through `listings.search_documents` (keyed batch) and
`listings.search_export` (cursor snapshot), but exposes no search endpoints —
the events carry identity, so no listing content rides the durable bus and no
indexer reads this database. Moderation is a separate **stapel-moderation**
module: this module emits `listing.submitted`, serves the content over
`listings.moderation_content` and applies the target-generic
`moderation.completed` verdict (including the `published → blocked` takedown),
but runs no moderation pipeline. Re-moderating an edit of a **live** listing is
post-moderation: the lifecycle stays `published`, `moderation_status` goes to
`pending`, the edit is visible immediately, and a rejecting verdict removes it
through the takedown edge.

## Contract

`docs/{schema,flows,errors}.json` are emitted from a single-module
`{listings + core}` Django instance mounted at the canonical
`/listings/api/v1` prefix (`make contract` / `make contract-check`; see
`_codegen.py`) — the same mechanism stapel-search, stapel-chat and
stapel-forms already use. `docs/flows.json` is `[]`: no flow is declared via
`@flow` yet, same state as every other contract-complete module today.

**Delta note — one field stays untyped on purpose.** `features_search`
(`ListingDetailSerializer`) is a flattened per-category search index: one
dynamic key per feature slug, shaped by whatever category schema a given
listing happens to carry. There is no fixed property set to declare, so the
schema types it as a bare `object` rather than fake a closed shape that would
go stale the moment any category adds a feature. Every other field that used
to fall back to an untyped blob this way — `images` / `images_draft`, both
lists of opaque `<type>/<hash>` CDN refs (models.py "Opaque list of CDN image
references") — is now typed as `array[string]`, and the ten polymorphic
attribute-value shapes (`FeatureDto`/`FeatureDao`) are a proper
discriminated `oneOf` keyed by `type`, contributed by stapel-attributes.

## Extension points

See [MODULE.md](https://github.com/usestapel/stapel-listings/blob/main/MODULE.md) — the agent-facing map of every fork-free seam
(settings, serializer seams, comm surface, GDPR provider).

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff
./setup-hooks.sh
pytest tests/
```
