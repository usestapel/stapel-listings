"""URL patterns — no global prefix here, the host project mounts them:

    path("listings/", include("stapel_listings.urls"))
"""
from rest_framework.routers import SimpleRouter

from .views import ListingViewSet

router = SimpleRouter()
router.register("listings", ListingViewSet, basename="listing")

urlpatterns = router.urls
