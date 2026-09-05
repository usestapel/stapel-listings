"""i18n error keys of stapel-listings.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses. Feature-value
validation error keys (below-minimum, mandatory-missing, …) belong to
stapel-attributes and are registered there; this module registers only the
listing-level keys.
"""
from stapel_core.django.api.errors import register_service_errors

# stapel-attributes is an embedded (non-app) library: autodiscovery never
# reaches its errors module, so without this import its 12 feature-validation
# keys enter the registry only as a side effect of serializer imports —
# errors.json emission then depends on whether the schema was built first.
# The embedding app forces the registration deterministically.
import stapel_attributes.errors  # noqa: F401

ERR_404_LISTING_NOT_FOUND = "error.404.listing_not_found"
ERR_403_LISTING_NOT_OWNER = "error.403.listing_not_owner"
# A guest (anonymous account) on an authorship write while
# ALLOW_ANONYMOUS_WRITES is off. Distinct from NOT_OWNER: that one is about
# whose listing this is, this one is about the account itself, and only the
# second is fixed by signing up.
ERR_403_ANONYMOUS_NOT_ALLOWED = "error.403.listing_anonymous_not_allowed"
ERR_409_LISTING_CANNOT_DELETE_ACTIVE = "error.409.listing_cannot_delete_active"
ERR_400_CATEGORY_REQUIRED = "error.400.category_required"
ERR_400_PUBLISH_VALIDATION_FAILED = "error.400.publish_validation_failed"
ERR_400_IMAGE_REQUIRED = "error.400.image_required"
ERR_409_INVALID_TRANSITION = "error.409.invalid_listing_transition"
ERR_409_ALREADY_FAVORITED = "error.409.already_favorited"
# M-7: a draft feature whose slug is not in the category's schema. Owned here
# transitionally — stapel-attributes has no NOT_ALLOWED ValidationErrorCode and
# is out of scope for this change (see the follow-up note in the task result);
# the structured validator uses this as the localizable key so validate-draft
# and publish agree (both reject unknown slugs) instead of diverging.
ERR_400_FEATURE_NOT_ALLOWED = "error.400.listing_feature_not_allowed"
# The ?status= filter of my/listings carries an unknown lifecycle value. A
# 400 rather than a silent empty page: "no listing is in that state" and "that
# state does not exist" are different answers, and only one of them is a
# client bug worth surfacing.
ERR_400_INVALID_STATUS_FILTER = "error.400.listing_invalid_status_filter"
# An explicit price of 0 outside a FREE_PRICE_CATEGORY_IDS category (Д51):
# «0 ₽» on a marketplace card is either a lie or a missed field, and a missed
# field is spelled NULL. The storefront renders NULL as «Цена не указана».
ERR_400_ZERO_PRICE_NOT_ALLOWED = "error.400.listing_zero_price_not_allowed"
# Д71: a draft with no coordinates. «Где находится» was optional, so a
# listing published with no place at all — outside every radius filter and
# every map, findable only by scrolling past it, with nothing anywhere saying
# why. The predicate is coordinates, not the label: the label is a string a
# client sends, and only the coordinates reach the geographic surfaces.
ERR_400_LOCATION_REQUIRED = "error.400.listing_location_required"
# The `draft_meta` sidecar (0.21.2), serialized, over
# DRAFT_META_MAX_BYTES — checked against the value that would actually be
# stored (after the shallow merge with what is already on the row), so a
# caller sees this the moment the STORED object would cross the cap rather
# than only on a single oversized call.
ERR_400_DRAFT_META_TOO_LARGE = "error.400.listing_draft_meta_too_large"
# Д421-follow-up (0.22.3): three ways a `features_draft` write payload can be
# shaped wrong, each naming the shape that WOULD have worked (in `params`,
# key `example` — a compact one-line JSON string) rather than only the type
# DRF's own field validation already names. The classic trigger is a client
# that read a listing's `features` (a list of decorated DAOs —
# `{slug, name, label, presentation, …}`) and posted that same list back
# under `features_draft`, which is written as a DICT keyed by slug. See
# `serializers.normalize_features_draft`, the one function both write
# entry points (create/update, save-draft) run their raw payload through
# before it ever reaches `ListingFeaturesInputField`.
ERR_400_FEATURES_DRAFT_SHAPE = "error.400.listing_features_draft_shape"
ERR_400_FEATURES_DRAFT_VALUE_SHAPE = "error.400.listing_features_draft_value_shape"
ERR_400_FEATURES_DRAFT_UNKNOWN_SLUG = "error.400.listing_features_draft_unknown_slug"

STAPEL_LISTINGS_ERRORS = {
    ERR_404_LISTING_NOT_FOUND: "Listing not found",
    ERR_403_LISTING_NOT_OWNER: "Not your listing",
    ERR_403_ANONYMOUS_NOT_ALLOWED: "A guest account may not publish a listing",
    ERR_409_LISTING_CANNOT_DELETE_ACTIVE: (
        "Cannot delete an active listing. Archive it first."
    ),
    ERR_400_CATEGORY_REQUIRED: "Category is required",
    ERR_400_PUBLISH_VALIDATION_FAILED: "Listing validation failed",
    ERR_400_IMAGE_REQUIRED: "At least one image is required to publish",
    ERR_409_INVALID_TRANSITION: "Invalid status transition for {from_status}",
    ERR_409_ALREADY_FAVORITED: "Listing already favorited",
    ERR_400_FEATURE_NOT_ALLOWED: "Feature '{feature}' is not allowed for this category",
    ERR_400_INVALID_STATUS_FILTER: "Unknown listing status '{status}'",
    ERR_400_LOCATION_REQUIRED: (
        "Choose where the item is before publishing"
    ),
    ERR_400_ZERO_PRICE_NOT_ALLOWED: (
        "A price of 0 is not allowed in this category. "
        "Leave the price empty for \"price not stated\"."
    ),
    ERR_400_DRAFT_META_TOO_LARGE: "draft_meta is too large ({max_bytes} bytes max)",
    ERR_400_FEATURES_DRAFT_SHAPE: (
        "features_draft must be an object keyed by feature slug, or the list "
        "of feature objects a listing read returns (each carrying its own "
        "'slug') — got {got_type}. Example of the accepted object form: "
        "{example}"
    ),
    ERR_400_FEATURES_DRAFT_VALUE_SHAPE: (
        "features_draft['{slug}'] must itself be an object of the form "
        "{{\"type\": <feature type>, \"value\": <feature value>}} — got "
        "{got_type}. Example: {example}"
    ),
    ERR_400_FEATURES_DRAFT_UNKNOWN_SLUG: (
        "Every entry of a features_draft list must carry its own non-empty "
        "'slug' string (the slug a listing read stores on that element) so "
        "it can be filed back under the right feature — entry at index "
        "{index} has none. Example: {example}"
    ),
}

register_service_errors(STAPEL_LISTINGS_ERRORS)

__all__ = [
    "ERR_400_LOCATION_REQUIRED",
    "STAPEL_LISTINGS_ERRORS",
    "ERR_404_LISTING_NOT_FOUND",
    "ERR_403_LISTING_NOT_OWNER",
    "ERR_403_ANONYMOUS_NOT_ALLOWED",
    "ERR_409_LISTING_CANNOT_DELETE_ACTIVE",
    "ERR_400_CATEGORY_REQUIRED",
    "ERR_400_PUBLISH_VALIDATION_FAILED",
    "ERR_400_IMAGE_REQUIRED",
    "ERR_409_INVALID_TRANSITION",
    "ERR_409_ALREADY_FAVORITED",
    "ERR_400_FEATURE_NOT_ALLOWED",
    "ERR_400_INVALID_STATUS_FILTER",
    "ERR_400_ZERO_PRICE_NOT_ALLOWED",
    "ERR_400_DRAFT_META_TOO_LARGE",
    "ERR_400_FEATURES_DRAFT_SHAPE",
    "ERR_400_FEATURES_DRAFT_VALUE_SHAPE",
    "ERR_400_FEATURES_DRAFT_UNKNOWN_SLUG",
]
