from django.urls import include, path

urlpatterns = [
    path("listings/", include("stapel_listings.urls")),
]
