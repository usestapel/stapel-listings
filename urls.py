"""URL patterns — no global prefix here, the host project mounts them:

    path("listings/", include("stapel_listings.urls"))
"""
from rest_framework.routers import DefaultRouter

from .views import ListingViewSet

router = DefaultRouter()
router.register("listings", ListingViewSet, basename="listing")

urlpatterns = router.urls
