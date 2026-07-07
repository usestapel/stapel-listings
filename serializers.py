"""Serializers for stapel-listings.

Feature values are polymorphic; their DTO/DAO serializers and OpenAPI schemas
come from stapel-attributes (``get_feature_dto_serializer_class`` /
``get_feature_dao_proxy_serializer``) — this module never re-describes attribute
types. The draft-write serializer replaces the legacy catalog's ~150-line hand-rolled
per-field validation in the ``save-draft`` view with declarative DRF fields.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from rest_framework import serializers

from stapel_attributes import (
    get_feature_dao_proxy_serializer,
    get_feature_dto_proxy_serializer,
    get_feature_dto_serializer_class,
)
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    DeleteResponse,
    FavoriteToggleResponse,
    ListingActionResponse,
    MyCountersResponse,
    PublishResponse,
)
from .models import Favorite, Listing, ListingStatus, validate_countable_stock


# --- Polymorphic feature fields ------------------------------------------


class ListingFeaturesInputField(serializers.DictField):
    """``{slug: FeatureDto}`` — draft attribute values keyed by feature slug."""

    def __init__(self, **kwargs):
        super().__init__(child=get_feature_dto_serializer_class()(), **kwargs)


class ListingFeaturesInputFieldExtension(OpenApiSerializerFieldExtension):
    target_class = ListingFeaturesInputField

    def map_serializer_field(self, auto_schema, direction):
        dto_proxy = get_feature_dto_proxy_serializer()
        auto_schema.resolve_serializer(dto_proxy, direction)
        return {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/FeatureDto"},
        }


class ListingFeaturesOutputField(serializers.JSONField):
    """``List[FeatureDao]`` — the stored, ordered feature projection."""


class ListingFeaturesOutputFieldExtension(OpenApiSerializerFieldExtension):
    target_class = ListingFeaturesOutputField

    def map_serializer_field(self, auto_schema, direction):
        dao_proxy = get_feature_dao_proxy_serializer()
        auto_schema.resolve_serializer(dao_proxy, direction)
        return {"type": "array", "items": {"$ref": "#/components/schemas/FeatureDao"}}


# --- Write (draft) --------------------------------------------------------


class ListingDraftSerializer(serializers.ModelSerializer):
    """Create/update the draft twin fields.

    All user-editable content is a ``*_draft`` field promoted on publish.
    DRF's declarative field validation (max_length, decimal bounds, types)
    replaces the source view's hand-rolled per-field checks.
    """

    features_draft = ListingFeaturesInputField(required=False, allow_null=True)
    images_draft = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )

    class Meta:
        model = Listing
        fields = [
            "id",
            "category_id",
            "currency",
            "language",
            "title_draft",
            "description_draft",
            "price_draft",
            "images_draft",
            "location_id_draft",
            "location_label_draft",
            "geohash_draft",
            "features_draft",
            "auto_republish",
            "countable",
            "stock_quantity",
            "status",
            "moderation_status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "moderation_status",
            "created_at",
            "updated_at",
        ]

    def validate_price_draft(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Price must be >= 0.")
        return value

    def validate_images_draft(self, value):
        if not value:
            return value
        seen, unique = set(), []
        for item in value:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def validate(self, attrs):
        # Cross-field: countable/stock_quantity may arrive independently (or
        # not at all, e.g. on a partial save-draft PATCH), so fall back to the
        # current instance's value for whichever side of the pair is absent
        # from this request. DRF's ModelSerializer does *not* auto-populate a
        # missing field with the Django model field's ``default`` on create
        # (that only happens later, inside ``Model.__init__``) — so on create
        # the fallback must be the model field default explicitly, not
        # ``None``, or a bare ``{"category_id": "7"}`` POST would be rejected.
        if self.instance is not None:
            countable_default = self.instance.countable
            stock_quantity_default = self.instance.stock_quantity
        else:
            countable_default = Listing._meta.get_field("countable").get_default()
            stock_quantity_default = Listing._meta.get_field(
                "stock_quantity"
            ).get_default()

        countable = attrs.get("countable", countable_default)
        stock_quantity = attrs.get("stock_quantity", stock_quantity_default)
        try:
            validate_countable_stock(countable, stock_quantity)
        except DjangoValidationError as exc:
            detail = exc.message_dict if hasattr(exc, "message_dict") else {
                "stock_quantity": exc.messages
            }
            raise serializers.ValidationError(detail) from exc
        return attrs


# --- Read -----------------------------------------------------------------


class ListingCardSerializer(serializers.ModelSerializer):
    """Compact card projection for lists."""

    features_title = ListingFeaturesOutputField(read_only=True)
    features_badges = ListingFeaturesOutputField(read_only=True)
    is_favorited = serializers.BooleanField(read_only=True, allow_null=True)

    class Meta:
        model = Listing
        fields = [
            "id",
            "title",
            "price",
            "price_base",
            "currency",
            "images",
            "features_title",
            "features_badges",
            "location_label",
            "geohash",
            "countable",
            "stock_quantity",
            "status",
            "is_favorited",
        ]


class ListingDetailSerializer(serializers.ModelSerializer):
    """Full listing detail."""

    features = ListingFeaturesOutputField(read_only=True)
    features_title = ListingFeaturesOutputField(read_only=True)
    features_badges = ListingFeaturesOutputField(read_only=True)
    features_search = serializers.JSONField(read_only=True)
    is_favorited = serializers.BooleanField(read_only=True, allow_null=True)

    class Meta:
        model = Listing
        fields = [
            "id",
            "owner",
            "category_id",
            "title",
            "description",
            "language",
            "price",
            "price_base",
            "currency",
            "images",
            "location_id",
            "location_label",
            "geohash",
            "features",
            "features_title",
            "features_badges",
            "features_search",
            "status",
            "moderation_status",
            "auto_republish",
            "countable",
            "stock_quantity",
            "published_at",
            "expires_at",
            "created_at",
            "updated_at",
            "is_favorited",
        ]


class ListingStatusSerializer(serializers.Serializer):
    """Lightweight status view (mirrors the listings.status comm Function)."""

    status = serializers.ChoiceField(choices=ListingStatus.choices)
    moderation_status = serializers.CharField()
    is_deleted = serializers.BooleanField()
    is_expired = serializers.BooleanField()
    is_active = serializers.BooleanField()
    owner_id = serializers.CharField()

    def to_representation(self, instance):
        return {
            "status": instance.status,
            "moderation_status": instance.moderation_status,
            "is_deleted": instance.is_deleted,
            "is_expired": instance.is_expired,
            "is_active": instance.is_active,
            "owner_id": str(instance.owner_id),
        }


class FavoriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Favorite
        fields = ["id", "listing", "created_at"]
        read_only_fields = fields


# --- Dataclass response serializers --------------------------------------


class PublishResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = PublishResponse


class ListingActionResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ListingActionResponse


class DeleteResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = DeleteResponse


class MyCountersResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MyCountersResponse


class FavoriteToggleResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = FavoriteToggleResponse
