"""Extension-point tests: seams actually swap behavior (library-standard §4)."""
from decimal import Decimal

import pytest

from stapel_listings.models import Listing, ListingStatus
from stapel_listings.services import category_schema, publish as publish_service

pytestmark = pytest.mark.django_db


def test_price_base_converter_seam(user, settings):
    # A host converter that halves EUR into a "base" currency.
    settings.STAPEL_LISTINGS = {
        "PRICE_BASE_CONVERTER": "stapel_listings.tests.seams.halving_converter",
    }
    listing = Listing.objects.create(owner=user, category_id="7", price=Decimal("100.00"))
    listing.refresh_from_db()
    assert listing.price_base == Decimal("50.00")


def test_default_price_base_is_identity(user):
    listing = Listing.objects.create(owner=user, category_id="7", price=Decimal("100.00"))
    listing.refresh_from_db()
    assert listing.price_base == Decimal("100.00")


def test_failing_price_converter_yields_null_not_wrong_value(user, settings):
    """A broken converter must store NULL (unknown), never the raw price as if
    it were already in the base currency."""
    settings.STAPEL_LISTINGS = {
        "PRICE_BASE_CONVERTER": "stapel_listings.tests.seams.exploding_converter",
    }
    listing = Listing.objects.create(
        owner=user, category_id="7", price=Decimal("10000.00"), currency="USD"
    )
    listing.refresh_from_db()
    assert listing.price_base is None


def test_category_features_function_seam(user, settings):
    """The comm Function name is overridable — point it at a different provider."""
    from stapel_core.comm import register_function
    from stapel_core.comm.registry import function_registry

    def alt_provider(payload):
        return {"category_id": payload["category_id"], "revision": 1, "features": []}

    register_function("catalog.schema", alt_provider)
    settings.STAPEL_LISTINGS = {"CATEGORY_FEATURES_FUNCTION": "catalog.schema"}
    try:
        assert category_schema.get_feature_configs("9") == []
    finally:
        function_registry._providers.pop("catalog.schema", None)


def test_require_image_on_publish_seam_off(draft_listing, settings):
    settings.STAPEL_LISTINGS = {"REQUIRE_IMAGE_ON_PUBLISH": False}
    draft_listing.images_draft = []
    draft_listing.save()
    publish_service.publish_listing(draft_listing)  # no raise
    draft_listing.refresh_from_db()
    assert draft_listing.status == ListingStatus.PENDING


def test_serializer_seam_override():
    """A subclass can swap a view's serializer without rewriting the method."""
    from stapel_listings.serializers import ListingCardSerializer
    from stapel_listings.views import ListingViewSet

    class CustomCard(ListingCardSerializer):
        pass

    class CustomViewSet(ListingViewSet):
        card_serializer_class = CustomCard

    vs = CustomViewSet()
    vs.action = "list"
    assert vs.get_serializer_class() is CustomCard
