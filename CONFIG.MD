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
| MODERATION_GATE | env | which side of moderation a FIRST publication lands on: `pre` holds it in `pending` for a `moderation.completed` verdict; `post` publishes it immediately with `moderation_status` still `pending` (review still happens; a rejecting verdict takes it down). Values outside {`pre`,`post`} are `stapel_listings.E001`. | no | pre |
| AUTO_APPROVE_ON_PUBLISH | env | when True, a published listing is approved immediately (no moderation.completed wait). | no | false |
| REQUIRE_IMAGE_ON_PUBLISH | env | require at least one image reference to publish. | no | true |
| VIEW_DEDUP_WINDOW_SECONDS | env | seconds one viewer's opens of one listing collapse into a single counted view. This is the whole cost control of view counting: every open after the first inside the window touches no database at all. Not a privacy control. | no | 21600 |
| REQUIRE_LOCATION_ON_PUBLISH | env | refuse to publish a listing with no coordinates (Д71). The predicate is `lat_draft`/`lon_draft`, not the label — a listing with only a display string is invisible on every geographic surface. | no | true |
| GEO_REVERSE_FUNCTION | env | name of the comm Function that turns coordinates into address components, so a card's location line is a PLACE derived from the pin rather than the picker's street-address string echoed back from the client (Д76). Empty = do not derive; the client-supplied label publishes verbatim. | no | geo.reverse_geocode |
| ENGAGEMENT_BATCH_LIMIT | env | ids one `GET listings/engagement` call may ask about. The endpoint is AllowAny (`view_count` is public), so the cap is what keeps it from being a cheap way to enumerate the board. | no | 100 |
| MODERATION_TARGET_TYPE | env | `target_type` this module answers to in target-generic `moderation.completed` verdicts; must match the host's `STAPEL_MODERATION["TARGET_TYPES"]` key. | no | listing |
| LISTING_URL_TEMPLATE | env | template of a listing's public URL, formatted with `listing_id`, returned by `listings.moderation_content`. Empty = unknown (returns ""). | no | (empty) |
| FREE_PRICE_CATEGORY_IDS | env | category ids (compared as strings) where an explicit price of 0 («free») is legal. Everywhere else `validate_draft` rejects 0; an absent price is stored/served as NULL («price not stated») and stays legal in every category. | no | () |
| DESCRIPTION_MIN_LENGTH | env | minimum free-text description length enforced on publish/validate. | no | 4 |
| DESCRIPTION_MAX_LENGTH | env | maximum free-text description length enforced on publish/validate. | no | 500 |
| DEFAULT_LISTING_TTL_DAYS | env | days until a freshly published listing expires (None disables expiry). | no | 30 |
| ALLOW_ANONYMOUS_WRITES | env | whether a guest (anonymous account) may author a listing. Gates `create`, `PUT`/`PATCH`, `save-draft` and `publish`; favorites, wind-down actions and every read stay open either way. | no | false |

### ALLOW_ANONYMOUS_WRITES

A storefront may mint an anonymous account silently — press "save this listing"
as a stranger and you get a real user row with `is_anonymous=True` and a valid
session. That session is genuinely authenticated, so `IsAuthenticated` cannot
tell it from a registered one and a "sign up to sell" wall drawn only in the
frontend is decoration. This key is the wall server-side: with it off, a guest
gets `error.403.listing_anonymous_not_allowed` on the authorship writes
(`POST /listings`, `PUT`/`PATCH /listings/{id}`, `save-draft`, `publish`). The
default is CLOSED because that is the cheap answer either way — a deployment
that mints no anonymous users has none to reject, and one that does gets the
wall it already believed it had. Setting it to `true` is the explicit statement
"guests may publish here"; a seller who cannot be reached again is not a
seller, so it should be said out loud.

What it deliberately does NOT gate: `favorite`/`unfavorite`/`my/favorites` —
the saved favorite is the very feature the anonymous session exists for — the
wind-down actions on a listing one already owns (`archive`, `complete`,
`destroy`), so flipping the key closed cannot strand content a guest published
while it was open, and every read.
### MODERATION_GATE

Strict pre-moderation (`pre`, the default) is only a working policy where a
moderator exists to give verdicts: `publish` puts the listing in `pending`,
`listing.submitted` goes out, and **only** a `moderation.completed` verdict
moves it to `published`. On a deployment with no moderator every listing
hangs in `pending` — invisible, unindexed, forever, with nothing in the
system that will ever move it.

`post` is the publish-first model of the big classified boards: the same publish flow
also transitions the listing to `published` — live and indexed at once,
`listing.published` emitted — while `moderation_status` stays `pending` and
`listing.submitted` still opens the case. Review happens on the live
content, and a rejecting verdict removes it from the public surface through
the `published` → `blocked` takedown edge (emitting `listing.removed`).
Unlike `AUTO_APPROVE_ON_PUBLISH`, nothing is approved: the verdict is still
owed, merely after the fact instead of before it.

The other half of this policy lives in the host's moderation registry
(`STAPEL_MODERATION["TARGET_TYPES"][<type>]["gate"]`); the two must agree,
and in the classified composite `stapel_classified.E004` fails the boot when
they do not.
