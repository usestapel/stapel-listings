# CONFIG.MD — stapel-listings

Config registry for **stapel-listings** (`static-scaffold-and-config.md` §2).
One row per configuration key the module reads, its **source** (`env` = the
process environment / the `STAPEL_LISTINGS` settings dict), what it is for,
whether it is required, and its default.

Every key is read through `listings_settings`
(`stapel_listings.conf.AppSettings`, namespace `STAPEL_LISTINGS`). Resolution
order per key: `settings.STAPEL_LISTINGS` dict → a flat Django setting of the
same name → environment variable → the default below.

stapel-listings hard-depends on **stapel-attributes** (the typed feature engine
for listing values); the scaffolder pulls attributes in automatically as a
headless pip dependency. The category schema is resolved by *comm-by-name*
(`CATEGORY_FEATURES_FUNCTION`), so stapel-categories is NOT a hard import.

## stapel-listings

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| CATEGORY_FEATURES_FUNCTION | env | name of the comm Function that resolves a category's feature schema. | no | categories.features |
| FEATURE_CONFIG_CACHE_TIMEOUT | env | seconds a resolved feature-config list is memoized (early-invalidated by `category.changed`). | no | 300 |
| BASE_CURRENCY | env | base currency code `price_base` is expressed in. | no | USD |
| PRICE_BASE_CONVERTER | env | dotted path to a `(amount, currency, base) -> Decimal` price_base converter (REPLACE). Default is identity. | no | stapel_listings.services.pricing.identity_converter |
| AUTO_APPROVE_ON_PUBLISH | env | when True, a published listing is approved immediately (no moderation.completed wait). | no | false |
| REQUIRE_IMAGE_ON_PUBLISH | env | require at least one image reference to publish. | no | true |
| MODERATION_TARGET_TYPE | env | `target_type` this module answers to in target-generic `moderation.completed` verdicts; must match the host's `STAPEL_MODERATION["TARGET_TYPES"]` key. | no | listing |
| LISTING_URL_TEMPLATE | env | template of a listing's public URL, formatted with `listing_id`, returned by `listings.moderation_content`. Empty = unknown (returns ""). | no | (empty) |
| DESCRIPTION_MIN_LENGTH | env | minimum free-text description length enforced on publish/validate. | no | 4 |
| DESCRIPTION_MAX_LENGTH | env | maximum free-text description length enforced on publish/validate. | no | 500 |
| DEFAULT_LISTING_TTL_DAYS | env | days until a freshly published listing expires (None disables expiry). | no | 30 |
