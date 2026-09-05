"""Settings namespace for stapel-listings.

All configuration is read through ``listings_settings`` (lazily, at call
time) — never via module-level ``os.getenv`` (values would freeze at import).
Resolution order per key: ``settings.STAPEL_LISTINGS`` dict -> flat Django
setting of the same name -> environment variable -> default below.

Dotted-path keys listed in ``import_strings`` are resolved with
``import_string`` — the fork-free escape hatch for swappable behavior.
See MODULE.md for the full settings table and seam semantics.
"""
from stapel_core.conf import AppSettings

listings_settings = AppSettings(
    "STAPEL_LISTINGS",
    defaults={
        # --- Category schema (comm-by-name; NO import of stapel-categories) ---
        # Name of the comm Function that resolves a category's feature schema.
        # Its payload is ``{"category_id": ...}`` and it returns
        # ``{"category_id", "revision", "features": [FeatureDef...]}``.
        "CATEGORY_FEATURES_FUNCTION": "categories.features",
        # Seconds a resolved feature-config list is memoized in the Django
        # cache. Invalidated early by the ``category.changed`` subscription.
        "FEATURE_CONFIG_CACHE_TIMEOUT": 300,
        # Name of the comm Function that lists one rung of the category
        # cascade — ``{"parent_id": id}`` -> ``{"children": [{id,
        # children_count, …}]}``. Read by ``listings.rename_feature_keys``
        # alone, to resolve the SUBTREE a renamed feature is inherited
        # through: a feature defined on a parent is answered by listings in
        # every category under it. Empty disables the walk, and so does an
        # unregistered provider — the rename then applies to the single
        # category it was given and says so (``subtree_resolved``).
        "CATEGORY_CHILDREN_FUNCTION": "categories.children",
        # --- Pricing (currency is an opaque code; conversion is a seam) ---
        # Base currency code price_base is expressed in.
        "BASE_CURRENCY": "USD",
        # Dotted path to a callable ``(amount: Decimal, currency: str,
        # base: str) -> Decimal`` computing price_base (single strategy,
        # REPLACE). Default is identity (price_base == price); a host with
        # stapel-currencies points this at a wrapper over ``currencies.convert``.
        "PRICE_BASE_CONVERTER": "stapel_listings.services.pricing.identity_converter",
        # --- Publish / moderation policy ---
        # Which side of moderation a FIRST publication lands on.
        #
        # "pre" (default): draft -> PENDING, and nothing is public until a
        # moderation.completed verdict arrives — the strict queue. Correct
        # only where a moderator exists to answer: on a stand with none,
        # every listing sits in PENDING forever and nothing will ever move
        # it.
        #
        # "post": the same flow also transitions the listing to PUBLISHED —
        # live at once, indexed at once — while moderation_status stays
        # PENDING and listing.submitted is still emitted, so the case still
        # opens and review still happens; a rejecting verdict takes the
        # listing down through the PUBLISHED -> BLOCKED edge. This is a
        # POLICY, not a verdict — unlike AUTO_APPROVE_ON_PUBLISH nothing is
        # approved here, the review is merely owed after the fact instead of
        # before it.
        #
        # Must agree with the gate the host's moderation policy declares for
        # this target type (STAPEL_MODERATION["TARGET_TYPES"][...]["gate"]);
        # a composite check (stapel_classified.E004) holds the two together.
        # Values outside {"pre", "post"} are stapel_listings.E001.
        "MODERATION_GATE": "pre",
        # When True, a published listing is approved immediately instead of
        # waiting for a moderation.completed event — for minimal deployments
        # with no stapel-moderation module installed.
        "AUTO_APPROVE_ON_PUBLISH": False,
        # Require at least one image reference to publish.
        "REQUIRE_IMAGE_ON_PUBLISH": True,
        # How long one viewer's opens of one listing collapse into a single
        # counted view. This is the whole cost control of view counting: at
        # 6 hours a buyer who opens a listing, leaves and comes back in the
        # evening is two views and a buyer who refreshes twenty times is one,
        # and every open after the first inside the window touches no
        # database at all (services/engagement.py). Lowering it raises both
        # the count and the write rate; raising it does the opposite. It is
        # NOT a privacy control — nothing about a viewer is stored by this
        # window beyond a hashed cache key that expires with it.
        "VIEW_DEDUP_WINDOW_SECONDS": 21600,
        # Д71: refuse to publish a listing with no coordinates. On by
        # default, like REQUIRE_IMAGE_ON_PUBLISH and for the same reason —
        # a listing nobody can find geographically is not a listing, it is a
        # row. A deployment with no geographic dimension at all (a purely
        # digital board) turns it off.
        "REQUIRE_LOCATION_ON_PUBLISH": True,
        # Д76: the comm Function that turns coordinates into address
        # components, so the card's location line is a PLACE derived from
        # the pin rather than the picker's street-address string echoed back
        # from the client (services/location.py). Empty = do not derive; the
        # client-supplied label is then published verbatim, which is what
        # every version before 0.16.0 did.
        "GEO_REVERSE_FUNCTION": "geo.reverse_geocode",
        # Decimal places a coordinate keeps on a PUBLIC read — the width of
        # the area a stranger is handed instead of the seller's pin. Two
        # places is ~1.1km, the same statement stapel-search's card already
        # makes (its CARD_COORD_PRECISION), so the two public surfaces of one
        # listing disclose the same thing.
        #
        # This is a privacy control, and the only one on this axis: the
        # published `geohash` is blanked rather than truncated, because two
        # independently-derived areas around one true point intersect to
        # something smaller than either. Raising this number republishes the
        # pin; ``tests/test_public_read.py`` fails if it goes above a
        # kilometre-wide cell.
        #
        # The exact point is untouched everywhere it is legitimate: the
        # owner's own read, staff, the service transport, and the search feed
        # whose server-side `distance_km` stays computed from the true
        # coordinates.
        "PUBLIC_COORD_PRECISION": 2,
        # Ids one /engagement call may ask about. A page of cards, not a
        # crawl: the endpoint is AllowAny (view_count is public), so the cap
        # is what keeps it from being a cheap way to enumerate the board.
        "ENGAGEMENT_BATCH_LIMIT": 100,
        # The moderation queue is target-generic: its verdicts carry
        # ``{target_type, target_key}``. This is the target_type a composite
        # registered listings under — a moderation.completed for any other
        # target type is not ours and is ignored. Must match the key in the
        # host's ``STAPEL_MODERATION["TARGET_TYPES"]``.
        "MODERATION_TARGET_TYPE": "listing",
        # Template of a listing's public URL, formatted with ``listing_id``
        # (e.g. "https://example.com/listings/{listing_id}"). Empty = unknown,
        # and ``listings.moderation_content`` returns "" rather than a guess:
        # a moderator's card links to the real listing only where the host
        # said what "real" is. This module serves no public site of its own.
        "LISTING_URL_TEMPLATE": "",
        # Category ids (compared as strings) where an EXPLICIT price of 0 is a
        # legal claim — a "free items" / «Отдам даром» section. Everywhere
        # else validate_draft rejects 0: a marketplace card reading «0 ₽» is
        # either a lie or a missed field, and a missed field is spelled NULL
        # («Цена не указана»), which stays legal in every category.
        "FREE_PRICE_CATEGORY_IDS": (),
        # Free-text description length bounds enforced on publish/validate.
        "DESCRIPTION_MIN_LENGTH": 4,
        "DESCRIPTION_MAX_LENGTH": 500,
        # Days until a freshly published listing expires (None disables expiry).
        "DEFAULT_LISTING_TTL_DAYS": 30,
        # --- The guest wall ---------------------------------------------
        # May a GUEST — an anonymous account (``User.is_anonymous``), the kind
        # a storefront mints silently so a stranger can save a favourite —
        # AUTHOR a listing? Such a session is genuinely authenticated, so
        # IsAuthenticated cannot tell it from a registered one and the module
        # has to be asked.
        #
        # CLOSED by default, and the default is the cheap answer either way: a
        # deployment that mints no anonymous users has none to reject, so it
        # costs them nothing; a deployment that does mint them gets the wall it
        # already believed it had. Flipping this to True is the explicit
        # statement "guests may publish here" — a seller who cannot be reached
        # again is not a seller, so it should be said out loud.
        #
        # Gates the AUTHORSHIP writes only: create, PUT/PATCH, save-draft,
        # publish. Favoriting, unfavoriting, the wind-down actions on a listing
        # one already owns (archive/complete/destroy) and every read stay open
        # in both positions — the favorite is the very feature the anonymous
        # session exists for.
        "ALLOW_ANONYMOUS_WRITES": False,
        # --- draft_meta -----------------------------------------------------
        # Max size, in bytes of its UTF-8 JSON serialization, of the
        # `draft_meta` sidecar (0.21.2). It is opaque to this module (the
        # storefront composer's per-field provenance is the first tenant) so
        # there is no per-key limit to enforce — only a ceiling on the whole
        # object, checked against the value that would actually be STORED
        # (after the shallow merge — see ListingDraftSerializer.validate),
        # so accumulating keys across several save-draft calls is capped the
        # same as one large call. 16 KiB is generous for a per-field tag map
        # and small enough that this never becomes a second content channel.
        "DRAFT_META_MAX_BYTES": 16 * 1024,
    },
    import_strings=("PRICE_BASE_CONVERTER",),
)

__all__ = ["listings_settings"]
