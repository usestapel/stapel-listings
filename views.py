"""DRF views for stapel-listings.

Thin views over the domain services. Value/config validation delegates to
stapel-attributes via ``services.publish.validate_draft`` — no re-implemented
engine here. The ``save-draft`` write goes through ``ListingDraftSerializer``
(declarative validation), replacing the source view's hand-rolled per-field
checks. Search/filter endpoints are intentionally absent — that is a future
stapel-search module fed by the ``listing.*`` events (see MODULE.md).
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from stapel_attributes.results import ValidationBatchResultSerializer
from stapel_core.django.api.errors import StapelErrorResponse, StapelResponse
from stapel_core.django.api.pagination import IDAnchorPagination

from .dto import (
    DeleteResponse,
    FavoriteToggleResponse,
    ListingActionResponse,
    MyCountersResponse,
    PublishResponse,
)
from .errors import (
    ERR_400_INVALID_STATUS_FILTER,
    ERR_400_PUBLISH_VALIDATION_FAILED,
    ERR_403_LISTING_NOT_OWNER,
    ERR_404_LISTING_NOT_FOUND,
    ERR_409_INVALID_TRANSITION,
    ERR_409_LISTING_CANNOT_DELETE_ACTIVE,
)
from .models import Favorite, Listing, ListingStatus, TransitionError
from .serializers import (
    DeleteResponseSerializer,
    FavoriteToggleResponseSerializer,
    ListingActionResponseSerializer,
    ListingCardSerializer,
    ListingDetailSerializer,
    ListingDraftSerializer,
    ListingStatusSerializer,
    MyCountersResponseSerializer,
    MyListingCardSerializer,
    PublishResponseSerializer,
)
from .services import publish as publish_service


def parse_status_filter(raw_values):
    """``?status=`` → an ordered list of lifecycle statuses.

    Accepts the two spellings a client may reach for interchangeably — a
    repeated parameter (``?status=draft&status=rejected``) and one
    comma-separated value (``?status=draft,rejected``) — because a dashboard
    tab is a SET of statuses (``my/counters`` groups them the same way) and
    which spelling a given HTTP client produces is not the caller's choice to
    make. Empty pieces are skipped, duplicates collapse, order is preserved.

    Raises :class:`ValueError` carrying the offending value for an unknown
    status: an empty page would say "you have none of those", which is a
    different sentence from "that status does not exist".
    """
    wanted = []
    for raw in raw_values:
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if piece not in ListingStatus.values:
                raise ValueError(piece)
            if piece not in wanted:
                wanted.append(piece)
    return wanted


class SerializerSeamMixin:
    """Overridable serializer seam for stapel-listings views.

    Host projects can swap a view's serializer by subclassing and setting the
    ``*_serializer_class`` attributes (or overriding ``get_serializer_class``).
    """

    def get_serializer_class(self):
        return getattr(self, "serializer_class", None)


@extend_schema(tags=["Listings"])
class ListingViewSet(SerializerSeamMixin, viewsets.ModelViewSet):
    """Listings CRUD plus owner lifecycle actions and favorites."""

    queryset = Listing.objects.all()
    permission_classes = [IsAuthenticatedOrReadOnly]
    pagination_class = IDAnchorPagination

    # Per-action serializer seam (subclass to override any entry).
    detail_serializer_class = ListingDetailSerializer
    card_serializer_class = ListingCardSerializer
    my_card_serializer_class = MyListingCardSerializer
    draft_serializer_class = ListingDraftSerializer

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return self.draft_serializer_class
        if self.action == "list":
            return self.card_serializer_class
        return self.detail_serializer_class

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if self.action == "list":
            return qs.published().with_favorited(user)
        if self.action == "retrieve":
            return qs.visible_to(user).with_favorited(user)
        return qs

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user, status=ListingStatus.DRAFT)

    # -- helpers -----------------------------------------------------------

    def _get_own(self, request, pk):
        """The module's one ownership gate: every write goes through it.

        Returns ``(listing, None)`` for the owner, ``(None, response)``
        otherwise — 404 for an absent (or soft-deleted) listing, 403 for
        someone else's.
        """
        try:
            listing = Listing.objects.get(pk=pk)
        except Listing.DoesNotExist:
            return None, StapelErrorResponse(404, ERR_404_LISTING_NOT_FOUND)
        if listing.owner_id != request.user.id:
            return None, StapelErrorResponse(403, ERR_403_LISTING_NOT_OWNER)
        return listing, None

    # -- CRUD writes -------------------------------------------------------

    def update(self, request, *args, **kwargs):
        """Write the draft fields — the write ``save-draft`` also performs.

        Owner only: 404 for an absent listing, 403 for someone else's.
        """
        # DRF's generated update resolves its object off Listing.objects.all()
        # under IsAuthenticatedOrReadOnly, which is no ownership check at all.
        # partial_update routes through here, so PUT and PATCH pass the
        # module's one gate rather than two copies of it.
        _, error = self._get_own(request, kwargs.get("pk"))
        if error:
            return error
        return super().update(request, *args, **kwargs)

    # -- inter-service status ---------------------------------------------

    @extend_schema(responses={200: ListingStatusSerializer})
    @action(detail=True, methods=["get"], permission_classes=[AllowAny])
    def status(self, request, pk=None):  # noqa: R007
        try:
            listing = Listing.all_objects.get(pk=pk)
        except Listing.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_LISTING_NOT_FOUND)
        return StapelResponse(ListingStatusSerializer(listing))

    # -- owner: counters & drafts -----------------------------------------

    @extend_schema(responses={200: MyCountersResponseSerializer})
    @action(detail=False, methods=["get"], url_path="my/counters",
            permission_classes=[IsAuthenticated])
    def my_counters(self, request):  # noqa: R007
        counts = Listing.objects.owned_by(request.user).aggregate(
            active=Count("id", filter=Q(status__in=[ListingStatus.PUBLISHED, ListingStatus.PENDING])),
            archived=Count("id", filter=Q(status__in=[
                ListingStatus.ARCHIVED, ListingStatus.PAUSED,
                ListingStatus.EXPIRED, ListingStatus.SOLD])),
            drafts=Count("id", filter=Q(status__in=[ListingStatus.DRAFT, ListingStatus.REJECTED])),
        )
        return StapelResponse(MyCountersResponseSerializer(MyCountersResponse(**counts)))

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="status",
                description=(
                    "Lifecycle status to narrow to. Repeat the parameter or "
                    "pass one comma-separated value for a set "
                    "(`?status=draft,rejected`); omit it for every status. An "
                    "unknown value is a 400, not an empty page."
                ),
                required=False,
                many=True,
                enum=ListingStatus.values,
            )
        ],
        responses={200: MyListingCardSerializer(many=True)},
    )
    @action(detail=False, methods=["get"], url_path="my/listings",
            permission_classes=[IsAuthenticated])
    def my_listings(self, request):  # noqa: R007
        """The caller's OWN listings, in every status.

        The counterpart of ``my/counters``: the same owner scope and the same
        status grouping, but the rows behind the three numbers. ``list`` is
        the shop window (``published()``, narrowable to nobody), so this is
        the only route by which a person can be shown their own drafts.

        Owner-scoped at the queryset via ``owned_by`` — a stranger's listing
        cannot be reached from here at any status, and soft-deleted rows are
        excluded by the default manager (a deleted listing is gone from the
        owner's dashboard exactly as it is gone from everywhere else).
        """
        try:
            statuses = parse_status_filter(request.query_params.getlist("status"))
        except ValueError as exc:
            return StapelErrorResponse(
                400, ERR_400_INVALID_STATUS_FILTER, {"status": str(exc)}
            )
        qs = Listing.objects.owned_by(request.user).with_favorited(request.user)
        if statuses:
            qs = qs.filter(status__in=statuses)
        page = self.paginate_queryset(qs)
        serializer = self.my_card_serializer_class(page, many=True)
        return self.get_paginated_response(serializer.data)

    @extend_schema(request=None, responses={200: ListingDraftSerializer})
    @action(detail=True, methods=["post"], url_path="save-draft",
            permission_classes=[IsAuthenticated])
    def save_draft(self, request, pk=None):  # noqa: R007
        """Persist draft fields (declarative validation via serializer)."""
        listing, error = self._get_own(request, pk)
        if error:
            return error
        serializer = self.draft_serializer_class(listing, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return StapelResponse(serializer.data)

    @extend_schema(responses={200: ValidationBatchResultSerializer})
    @action(detail=True, methods=["get"], url_path="validate-draft",
            permission_classes=[IsAuthenticated])
    def validate_draft(self, request, pk=None):  # noqa: R007
        listing, error = self._get_own(request, pk)
        if error:
            return error
        result = publish_service.validate_draft(listing)
        return StapelResponse(ValidationBatchResultSerializer(result))

    @extend_schema(request=None,
                   responses={200: PublishResponseSerializer,
                              400: ValidationBatchResultSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def publish(self, request, pk=None):  # noqa: R007
        listing, error = self._get_own(request, pk)
        if error:
            return error

        result = publish_service.validate_draft(listing)
        if not result.valid:
            return Response(  # noqa: R001
                ValidationBatchResultSerializer(result).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            publish_service.publish_listing(listing)
        except DjangoValidationError:
            return StapelErrorResponse(400, ERR_400_PUBLISH_VALIDATION_FAILED)

        listing.refresh_from_db()
        dto = PublishResponse(published=True, listing_id=listing.pk, status=listing.status)
        return StapelResponse(PublishResponseSerializer(dto))

    @extend_schema(request=None, responses={200: ListingActionResponseSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def archive(self, request, pk=None):  # noqa: R007
        return self._transition(request, pk, ListingStatus.ARCHIVED)

    @extend_schema(request=None, responses={200: ListingActionResponseSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def complete(self, request, pk=None):  # noqa: R007
        """Mark a listing sold."""
        return self._transition(request, pk, ListingStatus.SOLD)

    def _transition(self, request, pk, new_status):
        listing, error = self._get_own(request, pk)
        if error:
            return error
        try:
            listing.transition_to(new_status)
        except TransitionError:
            return StapelErrorResponse(
                409, ERR_409_INVALID_TRANSITION, {"from_status": listing.status}
            )
        dto = ListingActionResponse(success=True, status=listing.status)
        return StapelResponse(ListingActionResponseSerializer(dto))

    @extend_schema(responses={200: DeleteResponseSerializer})
    def destroy(self, request, *args, **kwargs):
        listing, error = self._get_own(request, kwargs.get("pk"))
        if error:
            return error
        if listing.status in (ListingStatus.PUBLISHED, ListingStatus.PENDING):
            return StapelErrorResponse(409, ERR_409_LISTING_CANNOT_DELETE_ACTIVE)
        listing.delete()
        return StapelResponse(DeleteResponseSerializer(DeleteResponse(success=True, deleted=True)))

    # -- favorites (first-class engagement) -------------------------------

    @extend_schema(request=None, responses={200: FavoriteToggleResponseSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def favorite(self, request, pk=None):  # noqa: R007
        # Same visibility as the detail read: favoriting is not a side door
        # that confirms a stranger's draft exists.
        try:
            listing = Listing.objects.visible_to(request.user).get(pk=pk)
        except Listing.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_LISTING_NOT_FOUND)
        Favorite.objects.get_or_create(user=request.user, listing=listing)
        return StapelResponse(
            FavoriteToggleResponseSerializer(
                FavoriteToggleResponse(favorited=True, listing_id=listing.pk)
            )
        )

    @extend_schema(request=None, responses={200: FavoriteToggleResponseSerializer})
    @action(detail=True, methods=["post"], permission_classes=[IsAuthenticated])
    def unfavorite(self, request, pk=None):  # noqa: R007
        Favorite.objects.filter(user=request.user, listing_id=pk).delete()
        return StapelResponse(
            FavoriteToggleResponseSerializer(
                FavoriteToggleResponse(favorited=False, listing_id=int(pk))
            )
        )

    @extend_schema(responses={200: ListingCardSerializer(many=True)})
    @action(detail=False, methods=["get"], url_path="my/favorites",
            permission_classes=[IsAuthenticated])
    def my_favorites(self, request):  # noqa: R007
        fav_ids = Favorite.objects.filter(user=request.user).values_list(
            "listing_id", flat=True
        )
        # A favorite outlives the listing's visibility: one blocked or
        # unpublished after the fact must not keep serving its card.
        qs = (
            Listing.objects.filter(id__in=list(fav_ids))
            .visible_to(request.user)
            .with_favorited(request.user)
        )
        page = self.paginate_queryset(qs)
        serializer = self.card_serializer_class(page, many=True)
        return self.get_paginated_response(serializer.data)
