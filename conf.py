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
        # --- Pricing (currency is an opaque code; conversion is a seam) ---
        # Base currency code price_base is expressed in.
        "BASE_CURRENCY": "EUR",
        # Dotted path to a callable ``(amount: Decimal, currency: str,
        # base: str) -> Decimal`` computing price_base (single strategy,
        # REPLACE). Default is identity (price_base == price); a host with
        # stapel-currencies points this at a wrapper over ``currencies.convert``.
        "PRICE_BASE_CONVERTER": "stapel_listings.services.pricing.identity_converter",
        # --- Publish / moderation policy ---
        # When True, a published listing is approved immediately instead of
        # waiting for a moderation.completed event — for minimal deployments
        # with no stapel-moderation module installed.
        "AUTO_APPROVE_ON_PUBLISH": False,
        # Require at least one image reference to publish.
        "REQUIRE_IMAGE_ON_PUBLISH": True,
        # Free-text description length bounds enforced on publish/validate.
        "DESCRIPTION_MIN_LENGTH": 4,
        "DESCRIPTION_MAX_LENGTH": 500,
        # Days until a freshly published listing expires (None disables expiry).
        "DEFAULT_LISTING_TTL_DAYS": 30,
    },
    import_strings=("PRICE_BASE_CONVERTER",),
)

__all__ = ["listings_settings"]
