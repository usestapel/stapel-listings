"""Admin registration for stapel-listings.

Listings are operational data; the admin is read-mostly (status/moderation are
driven by the lifecycle and comm, not hand-edited) to avoid bypassing the state
machine and event emission.
"""
from django.contrib import admin

from .models import Favorite, Listing


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "owner_id", "category_id", "status", "moderation_status",
        "price", "countable", "stock_quantity", "created_at",
    )
    list_filter = ("status", "moderation_status", "currency", "countable")
    search_fields = ("id", "title", "category_id")
    readonly_fields = (
        "features", "features_title", "features_badges", "features_search",
        "price_base", "published_at", "created_at", "updated_at", "deleted_at",
    )
    ordering = ("-id",)


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ("id", "user_id", "listing_id", "created_at")
    search_fields = ("user_id", "listing_id")
    ordering = ("-id",)
