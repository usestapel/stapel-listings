"""i18n error keys of stapel-listings.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses. Feature-value
validation error keys (below-minimum, mandatory-missing, …) belong to
stapel-attributes and are registered there; this module registers only the
listing-level keys.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_404_LISTING_NOT_FOUND = "error.404.listing_not_found"
ERR_403_LISTING_NOT_OWNER = "error.403.listing_not_owner"
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

STAPEL_LISTINGS_ERRORS = {
    ERR_404_LISTING_NOT_FOUND: "Listing not found",
    ERR_403_LISTING_NOT_OWNER: "Not your listing",
    ERR_409_LISTING_CANNOT_DELETE_ACTIVE: (
        "Cannot delete an active listing. Archive it first."
    ),
    ERR_400_CATEGORY_REQUIRED: "Category is required",
    ERR_400_PUBLISH_VALIDATION_FAILED: "Listing validation failed",
    ERR_400_IMAGE_REQUIRED: "At least one image is required to publish",
    ERR_409_INVALID_TRANSITION: "Invalid status transition for {from_status}",
    ERR_409_ALREADY_FAVORITED: "Listing already favorited",
    ERR_400_FEATURE_NOT_ALLOWED: "Feature '{feature}' is not allowed for this category",
}

register_service_errors(STAPEL_LISTINGS_ERRORS)

__all__ = [
    "STAPEL_LISTINGS_ERRORS",
    "ERR_404_LISTING_NOT_FOUND",
    "ERR_403_LISTING_NOT_OWNER",
    "ERR_409_LISTING_CANNOT_DELETE_ACTIVE",
    "ERR_400_CATEGORY_REQUIRED",
    "ERR_400_PUBLISH_VALIDATION_FAILED",
    "ERR_400_IMAGE_REQUIRED",
    "ERR_409_INVALID_TRANSITION",
    "ERR_409_ALREADY_FAVORITED",
    "ERR_400_FEATURE_NOT_ALLOWED",
]
