# stapel-listings

[![CI](https://github.com/usestapel/stapel-listings/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-listings/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-listings/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-listings)
[![PyPI](https://img.shields.io/pypi/v/stapel-listings.svg)](https://pypi.org/project/stapel-listings/)

Listings and catalog vertical for the [Stapel framework](https://github.com/usestapel) —
composable Django apps that deploy as a monolith or as microservices without
changing module code.

`Listing` is the marketplace core: an owner, an opaque category, typed
attribute values, a two-machine lifecycle + moderation status, a publish
pipeline, and first-class favorites. It **consumes** stapel-categories (feature
schema, over comm) and **stapel-attributes** (value validation), and stays
decoupled from search, moderation and currencies (see the boundaries below).

## Install

```bash
pip install stapel-listings
```

```python
INSTALLED_APPS = [
    # ...
    "stapel_listings",
]

# urls.py
path("listings/", include("stapel_listings.urls"))
```

Requires a `categories.features` comm Function provider (stapel-categories) for
value validation against a category's schema.

## Settings

All configuration lives in the `STAPEL_LISTINGS` namespace (dict setting, flat
setting, or env var — resolved lazily). Full table with seam semantics in
[MODULE.md](MODULE.md).

| Key | Default | Meaning |
|---|---|---|
| `CATEGORY_FEATURES_FUNCTION` | `"categories.features"` | comm Function resolving a category's feature schema. |
| `PRICE_BASE_CONVERTER` | identity | Dotted-path `(amount, currency, base) -> Decimal`. |
| `AUTO_APPROVE_ON_PUBLISH` | `False` | Publish immediately when no moderation module is installed. |
| `REQUIRE_IMAGE_ON_PUBLISH` | `True` | Require ≥1 image to publish. |
| `DEFAULT_LISTING_TTL_DAYS` | `30` | Days until a published listing expires. |

## comm surface

Emits (Actions): `listing.submitted` (moderation boundary),
`listing.published` / `listing.updated` / `listing.removed` (search boundary).
Consumes: `category.changed`, `moderation.completed`, `user.deleted`.
Provides Function: `listings.status`. Calls: `categories.features`.

**Boundaries:** search/filtering is a separate **stapel-search** module fed by
the `listing.*` events (this module builds `features_search` but exposes no
search endpoints); moderation is a separate **stapel-moderation** module
(this module emits `listing.submitted` and applies `moderation.completed`, it
runs no moderation pipeline).

## Extension points

See [MODULE.md](MODULE.md) — the agent-facing map of every fork-free seam
(settings, serializer seams, comm surface, GDPR provider).

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff
./setup-hooks.sh
pytest tests/
```

## License

MIT
