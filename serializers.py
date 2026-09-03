"""Serializers for stapel-listings.

Feature values are polymorphic; their DTO/DAO serializers and OpenAPI schemas
come from stapel-attributes (``get_feature_dto_serializer_class`` /
``get_feature_dao_proxy_serializer``) — this module never re-describes attribute
types. The draft-write serializer replaces the legacy catalog's ~150-line hand-rolled
per-field validation in the ``save-draft`` view with declarative DRF fields.
"""
import decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.fields import DecimalField

from stapel_attributes import (
    get_feature_dao_proxy_serializer,
    get_feature_dto_proxy_serializer,
    get_feature_dto_serializer_class,
)
from stapel_attributes import visibility
from stapel_core.django.api.permissions import IsServiceRequest
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
    """``List[FeatureDao]`` — the stored, ordered feature projection.

    Emits the column verbatim. Redaction is NOT here: a DRF field is handed a
    value, not the row it came off, so it cannot tell whether the person asking
    owns the listing. :class:`FeatureVisibilityMixin` does it one level up,
    where the instance and the request are both in hand.
    """


class ListingFeaturesOutputFieldExtension(OpenApiSerializerFieldExtension):
    target_class = ListingFeaturesOutputField

    #: What a reader gets instead of a value they are not entitled to. Declared
    #: here rather than on ``FeatureDao`` because it is never *stored*: it only
    #: exists on the wire, produced by :class:`FeatureVisibilityMixin`. A client
    #: branches on ``redacted``, renders the field's presence from ``present``,
    #: and may claim a check was run only if ``verification`` is there.
    REDACTED_SCHEMA = {
        "type": "object",
        "title": "RedactedFeatureDao",
        "description": (
            "A feature the catalogue marked non-public (visibility 'owner' or "
            "'staff') read by someone without that entitlement — an identifier "
            "such as a VIN or an IMEI. Carries no value. `present` says whether "
            "the seller filled it in, which is all this system observes; "
            "`verification` is absent unless an outside check was actually run, "
            "so a UI may say the value was supplied and must not say it was "
            "verified."
        ),
        "required": ["redacted", "present"],
        "properties": {
            "slug": {"type": "string"},
            "type": {"type": "string"},
            "name": {"type": "string", "nullable": True},
            "order": {"type": "integer", "nullable": True},
            "translate": {"type": "string", "nullable": True},
            "visibility": {"enum": ["owner", "staff"]},
            "verification": {"type": "object", "additionalProperties": True},
            "redacted": {"const": True},
            "present": {"type": "boolean"},
        },
    }

    def map_serializer_field(self, auto_schema, direction):
        dao_proxy = get_feature_dao_proxy_serializer()
        auto_schema.resolve_serializer(dao_proxy, direction)
        return {
            "type": "array",
            "items": {
                "oneOf": [
                    {"$ref": "#/components/schemas/FeatureDao"},
                    self.REDACTED_SCHEMA,
                ]
            },
        }


# --- Who may read a stored feature value ----------------------------------


class FeatureVisibilityMixin:
    """Redacts non-public feature values for the person actually asking.

    Some attributes identify a specific physical unit instead of describing it:
    a VIN, an IMEI, a serial number. The catalogue marks them
    ``visibility: "owner"`` (or ``"staff"``) and stapel-attributes stamps that
    onto every stored DAO, so this mixin needs no category schema — the value
    says for itself who may read it.

    **It is applied to every serializer in this module that emits a feature
    column, and ``tests/test_feature_visibility.py`` fails if a new one is
    added without it.** That gate is the point: the leak this fixes existed
    because ``features`` was a plain ``JSONField``, so each serializer that
    listed the field inherited the disclosure for free and nothing anywhere
    said "wait".

    It fails closed. No request in the serializer context — a
    ``many=True`` instantiation, a comm caller, a management command rendering
    a payload — resolves to ``anonymous`` and redacts, because the only safe
    answer to "who is this?" when nobody said is "a stranger".
    """

    #: Columns carrying ``List[FeatureDao]``. ``features_search`` is absent on
    #: purpose: it is a ``{slug: [value]}`` map with no DAO to read a stamp
    #: off, and it is built already-clean (``services.features``), which is the
    #: only correct place for a column that is also read raw by the indexer and
    #: by two bus payloads.
    FEATURE_DAO_FIELDS = ("features", "features_title", "features_badges")

    def resolve_feature_audience(self, instance) -> str:
        """``anonymous`` / ``owner`` / ``staff`` for this request and row.

        Mirrors ``views._may_see_full_status``: a fleet service (X-API-KEY) and
        a staff user read as staff, the row's owner reads as owner. Note that
        moderation does NOT come through this viewset at all — a moderator
        reads a listing through stapel-moderation and the
        ``listings.moderation_content`` function — so the staff branch here is
        for the service transport and the admin, not for the console.
        """
        request = self.context.get("request")
        if request is None:
            return visibility.ANONYMOUS
        if IsServiceRequest().has_permission(request, None):
            return visibility.AUDIENCE_STAFF
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return visibility.ANONYMOUS
        if getattr(user, "is_staff", False):
            return visibility.AUDIENCE_STAFF
        owner_id = getattr(instance, "owner_id", None)
        if owner_id is not None and str(getattr(user, "pk", "")) == str(owner_id):
            return visibility.AUDIENCE_OWNER
        return visibility.ANONYMOUS

    def to_representation(self, instance):
        data = super().to_representation(instance)
        audience = self.resolve_feature_audience(instance)
        for field in self.FEATURE_DAO_FIELDS:
            rows = data.get(field)
            if rows:
                # `features` keeps the hidden row as a value-free stub so the
                # public attribute table has the same shape as the seller's and
                # a buyer can see that a VIN exists and was filled in. The
                # title and badge projections are built without hidden values
                # at all, so this is a no-op on them unless the row predates
                # the axis and has not been re-projected yet.
                data[field] = visibility.redact_daos(rows, audience)
        return data


# --- Coordinates ----------------------------------------------------------


class CoordinateField(DecimalField):
    """A latitude or a longitude, rounded to the column's precision.

    ``DecimalField`` refuses a number carrying more decimal places than its
    column holds. For money that is right: silently dropping a digit changes
    what somebody is charged, so the caller has to say what it meant. For a
    coordinate it is not a defect at all — every geocoder answers in whatever
    precision its source happened to carry, Photon in seven places, a phone's
    GPS in fourteen, and the seventh place of a latitude is **eleven
    centimetres**. Nothing downstream of this field can tell the difference:
    the geohash is computed from the stored value, and search boxes it.

    So a coordinate is quantized on the way in, not rejected. The bounds still
    apply — ``max_digits`` keeps 1000.5 out — because a longitude of 1000 is a
    wrong answer, while a longitude of 37.6174782 is a right one written more
    precisely than the column.

    Rounding happens BEFORE ``validate_precision``, which is the only reason
    this is a subclass and not a ``validate_<field>`` method: DRF raises inside
    ``to_internal_value``, before any per-field validator gets to see the value.
    """

    def validate_precision(self, value):
        return super().validate_precision(self._quantize_to_column(value))

    def _quantize_to_column(self, value):
        if self.decimal_places is None:
            return value
        return value.quantize(
            decimal.Decimal(1).scaleb(-self.decimal_places),
            rounding=self.rounding,
        )


def coordinate_field_for(field_name):
    """A ``CoordinateField`` matching the model column, so it cannot drift.

    The precision is read off the field rather than repeated here: a migration
    that widens the column widens what the API accepts, in one place.
    """
    model_field = Listing._meta.get_field(field_name)
    return CoordinateField(
        max_digits=model_field.max_digits,
        decimal_places=model_field.decimal_places,
        required=False,
        allow_null=True,
    )


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
    # A geocoder's precision is not a client error. See CoordinateField.
    lat_draft = coordinate_field_for("lat_draft")
    lon_draft = coordinate_field_for("lon_draft")

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
            "lat_draft",
            "lon_draft",
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
            # Server-computed from lat_draft/lon_draft (Listing.save() /
            # compute_geohash_draft) via the geo.geohash_encode comm
            # Function — a client sends coordinates, not a geohash.
            "geohash_draft",
        ]

    def validate_price_draft(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("Price must be >= 0.")  # noqa: R002
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
            raise serializers.ValidationError(detail) from exc  # noqa: R002
        return attrs


# --- Read -----------------------------------------------------------------


class ListingCardSerializer(FeatureVisibilityMixin, serializers.ModelSerializer):
    """Compact card projection for lists."""

    # Published twin of `images_draft` — same shape (opaque CDN refs,
    # `<type>/<hash>`, models.py "Opaque list of CDN image references"), but
    # ModelSerializer has no source override for it, so without this explicit
    # declaration it falls back to a bare untyped JSONField in the OpenAPI
    # schema (contract-pipeline.md A1 — typed where typeable, no free-form
    # blob for something that is, in fact, `list[str]`).
    images = serializers.ListField(
        child=serializers.CharField(), read_only=True, allow_null=True
    )
    features_title = ListingFeaturesOutputField(read_only=True)
    features_badges = ListingFeaturesOutputField(read_only=True)
    is_favorited = serializers.BooleanField(read_only=True, allow_null=True)
    # Three-state on purpose (models.py ``with_viewed``): true / false /
    # null-for-anonymous. A storefront greys out a card on `true`; `null`
    # means the answer is not knowable for this reader, which is a different
    # sentence from "not seen".
    viewed = serializers.BooleanField(read_only=True, allow_null=True)

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
            "lat",
            "lon",
            "countable",
            "stock_quantity",
            "status",
            "is_favorited",
            "viewed",
            "view_count",
        ]


class MyListingCardSerializer(ListingCardSerializer):
    """The owner's own card — the public card plus what only an owner sees.

    Same family as :class:`ListingCardSerializer` (one shape for every grid a
    product renders), extended along two axes and no further:

    - **the moderation axis** (``moderation_status``): visibility is decided
      by ``status`` alone, but since 0.5.0 a *published* listing can be under
      re-review, and its owner is the one person who has to be told. A
      dashboard cannot derive that sentence from ``status``.
    - **the draft twins** (``title_draft`` / ``price_draft`` /
      ``images_draft``): the published fields are empty on a listing that has
      never been published, so a drafts tab built on the public card would
      render a column of blank rows. This is the list half of the pair's
      upstream ask #2 — the detail read is unchanged and still serializes the
      published fields only.

    Owner-scoped by construction: this serializer is used by exactly one
    route, ``my/listings``, whose queryset is ``owned_by(request.user)``.
    """

    images_draft = serializers.ListField(
        child=serializers.CharField(), read_only=True, allow_null=True
    )
    available_transitions = serializers.SerializerMethodField()

    @extend_schema_field(
        {"type": "array", "items": {"type": "string", "enum": ListingStatus.values}}
    )
    def get_available_transitions(self, obj) -> list:
        """The moves this seller may make from here — the third axis.

        ``status`` says where the listing is and ``moderation_status`` says
        what is being waited on; neither answers *what can I do about it*, and
        a cabinet that has to work that out re-implements a state machine it
        cannot see. It got it wrong in the direction that matters: for a
        listing in ARCHIVED, REJECTED, PAUSED, EXPIRED, SOLD or BLOCKED, the
        API's answer for a long time was genuinely nothing but ``DELETE``, and
        the UI showed exactly that.

        Read from ``models.OWNER_TRANSITIONS``, which the transition route
        also validates against, so this is a report of the server's rule
        rather than a second opinion about it.
        """
        from .models import owner_transitions_for

        return owner_transitions_for(obj.status)

    class Meta(ListingCardSerializer.Meta):
        fields = ListingCardSerializer.Meta.fields + [
            "moderation_status",
            "available_transitions",
            "title_draft",
            "price_draft",
            "images_draft",
            "created_at",
            "updated_at",
        ]


class ListingDetailSerializer(FeatureVisibilityMixin, serializers.ModelSerializer):
    """Full listing detail."""

    # See ListingCardSerializer.images — same fix, same reason.
    images = serializers.ListField(
        child=serializers.CharField(), read_only=True, allow_null=True
    )
    features = ListingFeaturesOutputField(read_only=True)
    features_title = ListingFeaturesOutputField(read_only=True)
    features_badges = ListingFeaturesOutputField(read_only=True)
    # NOT typed further on purpose (A1 delta note, docs/readme.md): this is a
    # flattened per-category search index (mileage_int, condition_select,
    # ... — one dynamic key per feature slug, keyed and shaped by whatever
    # category schema this listing happens to carry). There is no fixed
    # property set to declare, so `object` is the honest schema, not a gap to
    # "fix" — a oneOf over every live category's feature set would go stale
    # the moment a category adds a feature.
    features_search = serializers.JSONField(read_only=True)
    is_favorited = serializers.BooleanField(read_only=True, allow_null=True)
    # Three-state on purpose (models.py ``with_viewed``): true / false /
    # null-for-anonymous. A storefront greys out a card on `true`; `null`
    # means the answer is not knowable for this reader, which is a different
    # sentence from "not seen".
    viewed = serializers.BooleanField(read_only=True, allow_null=True)

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
            "lat",
            "lon",
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
            "viewed",
            "view_count",
        ]


class ListingEngagementSerializer(serializers.Serializer):
    """The per-viewer overlay for ONE listing in a batch read.

    Exists because a storefront's grid does not come from this module: the
    SERP is served by the search index, whose stored card can carry neither a
    flag that differs per reader nor a counter that moves faster than a
    document re-indexed on a listing event. So the grid draws the card from
    search and asks HERE, once for the whole page, for the three things that
    are about the person looking.
    """

    view_count = serializers.IntegerField(
        help_text="Distinct viewers who have opened this listing. Public — it "
        "is the same number for every reader.",
    )
    viewed = serializers.BooleanField(
        allow_null=True,
        help_text="Whether the CALLER has opened this listing before. `null` "
        "for an anonymous caller: nothing is remembered for a stranger, and "
        "`false` would be a claim rather than an absence.",
    )
    is_favorited = serializers.BooleanField(
        allow_null=True,
        help_text="Whether the CALLER has favorited it. `null` for anonymous, "
        "same reason.",
    )


class ListingEngagementBatchSerializer(serializers.Serializer):
    """``{listing id: overlay}``. An id with no listing is simply absent."""

    items = serializers.DictField(child=ListingEngagementSerializer())


class ListingPresenceSerializer(serializers.Serializer):
    """What a STRANGER may learn from the status probe: that a row exists.

    The probe reads ``all_objects``, so it answers for soft-deleted and
    unpublished listings — that is the point: it is what lets a page say "this
    listing was removed" instead of the 404 a made-up id also produces.

    But the full status view carries ``owner_id`` and ``moderation_status``,
    and listing ids are sequential. Under ``AllowAny`` that made the endpoint
    an enumeration oracle: walk the ids and harvest, for every listing in the
    fleet including other people's drafts and rejected rows, who owns it and
    what a moderator decided about it. Verified live on a stand.

    Deleting the endpoint would have been the wrong fix — a real client uses
    it, and the capability it grants a stranger (learning that a listing
    existed and is gone) is the feature. So the CAPABILITY stays and the
    DISCLOSURE goes: one boolean, which is all the removed-versus-never-existed
    sentence needs.
    """

    is_deleted = serializers.BooleanField()

    def to_representation(self, instance):
        return {"is_deleted": instance.is_deleted}


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


class ListingTransitionRequestSerializer(serializers.Serializer):
    """The body of ``POST listings/{id}/transition/``: where to move it.

    A ``ChoiceField`` over the whole lifecycle rather than over the moves this
    particular listing has, because the two refusals are different sentences
    and a client should be able to tell them apart: a status that does not
    exist is a 400 (the caller is confused about the vocabulary), a status
    that exists but is not this listing's to reach is a 409 with
    ``from_status`` (the caller is confused about the row). Narrowing the
    field to the per-row set would collapse both into 400 and lose the
    ``from_status`` that tells a storefront what to re-render.
    """

    to = serializers.ChoiceField(choices=ListingStatus.choices)


class DeleteResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = DeleteResponse


class MyCountersResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MyCountersResponse


class FavoriteToggleResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = FavoriteToggleResponse
