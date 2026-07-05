"""GDPR provider for stapel-listings (a data-holding module — required).

Registered in ``apps.ready()``; also driven by the ``user.deleted``
subscription (see ``actions.py``). Listings and favorites are erased on account
deletion; indexed listings emit ``listing.removed`` first so a search backend
drops them too.
"""
from stapel_core.gdpr import GDPRProvider


class ListingsGDPRProvider(GDPRProvider):
    section = "listings"

    def export(self, user_id: int) -> dict:
        from .models import Favorite, Listing

        listings = [
            {
                "id": listing.pk,
                "category_id": listing.category_id,
                "title": listing.title,
                "description": listing.description,
                "status": listing.status,
                "price": str(listing.price),
                "currency": listing.currency,
                "created_at": listing.created_at.isoformat(),
                "updated_at": listing.updated_at.isoformat(),
            }
            for listing in Listing.all_objects.filter(owner_id=user_id)
        ]
        favorites = list(
            Favorite.objects.filter(user_id=user_id).values_list(
                "listing_id", flat=True
            )
        )
        return {"listings": listings, "favorites": favorites}

    def delete(self, user_id: int) -> None:
        from stapel_core.comm import mutate_and_emit

        from . import events
        from .models import INDEXED_STATUSES, Favorite, Listing

        # Erasure and its listing.removed events commit as one unit: a crash
        # mid-erasure must not leave rows deleted with no event (a search
        # index would keep serving dead listings) nor events for rows that
        # rolled back.
        with mutate_and_emit():
            # This user's favorites of any listing, plus others' favorites of
            # the user's listings, both go away with the rows.
            Favorite.objects.filter(user_id=user_id).delete()

            for listing in Listing.all_objects.filter(owner_id=user_id):
                if listing.status in INDEXED_STATUSES and not listing.is_deleted:
                    events.emit_listing_removed(listing, reason="user_deleted")
                listing.hard_delete()

    def anonymize(self, user_id: int) -> None:
        # Listings are owned content erased wholesale on deletion; there is no
        # partial-anonymization mode for them.
        self.delete(user_id)
