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
from stapel_core.django.api.permissions import IsNotAnonymousUser, IsServiceRequest

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
    ERR_403_ANONYMOUS_NOT_ALLOWED,
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
    ListingEngagementBatchSerializer,
    ListingDraftSerializer,
    ListingPresenceSerializer,
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


def anonymous_write_refusal(request):
    """The 403 a GUEST gets on an authorship write, or ``None`` to let it through.

    A storefront may mint an anonymous account silently (press the heart as a
    stranger, get a real ``User`` row with ``is_anonymous=True`` and a valid
    session). That session is authenticated, so ``IsAuthenticated`` waves it
    through and the "sign up to sell" wall is decoration unless this module
    asks the question itself. ``ALLOW_ANONYMOUS_WRITES`` is where the answer
    lives, and it is closed by default (see ``conf.py``).

    The predicate is core's :class:`IsNotAnonymousUser` — the module owns the
    switch and the error key, never a second copy of the rule. A *response* is
    returned rather than an exception raised because the refusal has to read
    as this module's error dialect, and only ``StapelErrorResponse`` does that
    under DRF's stock exception handler as well as under the host's.

    Called from the authorship writes only. Favoriting is deliberately not one
    of them: the anonymous session exists so a stranger CAN keep a favorite,
    and a wall across that endpoint would break the feature it was built for.
    """
    from .conf import listings_settings

    if listings_settings.ALLOW_ANONYMOUS_WRITES:
        return None
    if IsNotAnonymousUser().has_permission(request, None):
        return None
    return StapelErrorResponse(403, ERR_403_ANONYMOUS_NOT_ALLOWED)


class SerializerSeamMixin:
    """Overridable serializer seam for stapel-listings views.

    Host projects can swap a view's serializer by subclassing and setting the
    ``*_serializer_class`` attributes (or overriding ``get_serializer_class``).
    """

    def get_serializer_class(self):
        return getattr(self, "serializer_class", None)


@extend_schema(tags=["Listings"])
def _may_see_full_status(request, listing) -> bool:
    """Who gets `owner_id` and `moderation_status` from the status probe.

    A fleet service (the `listings.status` function's transport, X-API-KEY)
    and the listing's own owner. Everyone else learns only whether the row is
    deleted, which is all the removed-versus-never-existed sentence needs.
    """
    if IsServiceRequest().has_permission(request, None):
        return True
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return str(getattr(user, "pk", "")) == str(listing.owner_id)


class ListingViewSet(SerializerSeamMixin, viewsets.ModelViewSet):
    """Listings CRUD plus owner lifecycle actions and favorites.

    Read and write live in one class, so the guest wall
    (:func:`anonymous_write_refusal`, the ``ALLOW_ANONYMOUS_WRITES`` switch)
    is applied per ACTION and never as a class permission: an anonymous
    session must keep browsing and keep its favorites, and only the
    authorship actions — ``create``, ``update``/``partial_update``,
    ``save-draft``, ``publish`` — are the ones that turn a caller into a
    seller.
    """

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
            return qs.published().with_favorited(user).with_viewed(user)
        if self.action == "retrieve":
            return qs.visible_to(user).with_favorited(user).with_viewed(user)
        return qs

    def retrieve(self, request, *args, **kwargs):
        """Read one listing — and count the open.

        A GET with a side effect, deliberately, because "opening a listing"
        IS the event and a storefront that had to POST it separately would
        report a different number than the page it drew (and would stop
        reporting at all the moment a client forgot the second call).

        The side effect is bounded on both ends. It cannot lie: the owner's
        own opens and unattributable ones are refused inside
        ``record_view``. And it cannot become a write per read: every repeat
        open inside ``VIEW_DEDUP_WINDOW_SECONDS`` is one cache hit and no
        query. The response is therefore safe to give a private cache and
        must never be given a shared one — which is the pre-existing rule
        for an authenticated read anyway.

        ``viewed`` is annotated BEFORE the recording, so the open that first
        sees a listing answers ``false`` and the next one answers ``true``.
        A card that greyed itself out on the very read that discovered it
        would be telling the reader they had already been here.
        """
        from stapel_core.netintel import client_ip

        from .services.engagement import record_view

        listing = self.get_object()
        session = getattr(request, "session", None)
        record_view(
            listing,
            user=request.user,
            session_key=getattr(session, "session_key", "") or "",
            client_key=self._client_key(request, client_ip(request)),
        )
        listing.refresh_from_db(fields=["view_count"])
        return Response(self.get_serializer(listing).data)

    @staticmethod
    def _client_key(request, ip):
        """The coarse anonymous fingerprint: IP + User-Agent, or ``""``.

        Assembled here and hashed in the service — see
        ``services/engagement.py`` for why this is deliberately weak and why
        making it stronger would be the wrong repair.
        """
        if not ip:
            return ""
        agent = request.META.get("HTTP_USER_AGENT", "")
        return f"{ip}|{agent}"

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

    def create(self, request, *args, **kwargs):
        """Open a draft — the first act of becoming a seller, so the guest
        wall stands here and not only at ``publish``."""
        refusal = anonymous_write_refusal(request)
        if refusal is not None:
            return refusal
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        """Write the draft fields — the write ``save-draft`` also performs.

        Owner only: 404 for an absent listing, 403 for someone else's.
        """
        refusal = anonymous_write_refusal(request)
        if refusal is not None:
            return refusal
        # DRF's generated update resolves its object off Listing.objects.all()
        # under IsAuthenticatedOrReadOnly, which is no ownership check at all.
        # partial_update routes through here, so PUT and PATCH pass the
        # module's one gate rather than two copies of it.
        _, error = self._get_own(request, kwargs.get("pk"))
        if error:
            return error
        return super().update(request, *args, **kwargs)

    # -- inter-service status ---------------------------------------------

    # Reads `all_objects`, so it answers for soft-deleted and unpublished rows.
    # That is the FEATURE: it is what lets a page distinguish "this listing was
    # removed" from the 404 a made-up id also produces, and a real browser
    # client uses it for exactly that.
    #
    # What it must NOT do is hand a stranger `owner_id` and `moderation_status`
    # for every id in the fleet. Listing ids are sequential, so under a single
    # AllowAny shape this was an enumeration oracle over other people's drafts,
    # rejected and deleted listings — verified live on a stand, which is how it
    # was found. Locking the whole action to services was the first fix tried
    # and it was wrong: it would have broken the browser client that legitimately
    # needs the removed-versus-never-existed answer.
    #
    # So the CAPABILITY stays and the DISCLOSURE goes. A service (the
    # `listings.status` function's transport) and the listing's own owner get
    # the full view; everyone else gets one boolean.
    @extend_schema(responses={200: ListingStatusSerializer})
    @action(detail=True, methods=["get"], permission_classes=[AllowAny])
    def status(self, request, pk=None):  # noqa: R007
        try:
            listing = Listing.all_objects.get(pk=pk)
        except Listing.DoesNotExist:
            return StapelErrorResponse(404, ERR_404_LISTING_NOT_FOUND)
        if _may_see_full_status(request, listing):
            return StapelResponse(ListingStatusSerializer(listing))
        return StapelResponse(ListingPresenceSerializer(listing))

    # -- owner: counters & drafts -----------------------------------------

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="ids",
                description="Listing ids, comma-separated or the parameter "
                "repeated. Capped at ENGAGEMENT_BATCH_LIMIT ids per call — "
                "one page of cards, not a crawl of the board.",
                required=True,
                many=True,
                type=str,
            )
        ],
        responses={200: ListingEngagementBatchSerializer},
    )
    @action(detail=False, methods=["get"], url_path="engagement",
            permission_classes=[AllowAny])
    def engagement(self, request):  # noqa: R007
        """The per-viewer overlay for a page of cards, in one call.

        ``AllowAny`` on purpose: ``view_count`` is public, and an anonymous
        caller gets it with both per-viewer flags answering ``null``. A
        storefront therefore makes the same request whether or not anyone is
        signed in, and a guest's grid is not a second code path.
        """
        raw: list[str] = []
        for value in request.query_params.getlist("ids"):
            raw.extend(part.strip() for part in str(value).split(","))
        from .conf import listings_settings

        ids = [part for part in raw if part][
            : int(listings_settings.ENGAGEMENT_BATCH_LIMIT)
        ]
        user = request.user
        user_id = str(user.pk) if getattr(user, "is_authenticated", False) else ""
        from .services.engagement import engagement_for

        return StapelResponse(
            ListingEngagementBatchSerializer({"items": engagement_for(ids, user_id=user_id)})
        )

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
        qs = (
            Listing.objects.owned_by(request.user)
            .with_favorited(request.user)
            .with_viewed(request.user)
        )
        if statuses:
            qs = qs.filter(status__in=statuses)
        page = self.paginate_queryset(qs)
        # The context is what makes the owner an OWNER to
        # ``FeatureVisibilityMixin``: without it the mixin fails closed and
        # would redact the seller's own VIN out of their own dashboard.
        serializer = self.my_card_serializer_class(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)

    @extend_schema(request=None, responses={200: ListingDraftSerializer})
    @action(detail=True, methods=["post"], url_path="save-draft",
            permission_classes=[IsAuthenticated])
    def save_draft(self, request, pk=None):  # noqa: R007
        """Persist draft fields (declarative validation via serializer)."""
        refusal = anonymous_write_refusal(request)
        if refusal is not None:
            return refusal
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
        """Put a listing in front of buyers — the act the wall is named for:
        a seller nobody can reach again is not a seller."""
        refusal = anonymous_write_refusal(request)
        if refusal is not None:
            return refusal
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
        serializer = self.card_serializer_class(
            page, many=True, context=self.get_serializer_context()
        )
        return self.get_paginated_response(serializer.data)
