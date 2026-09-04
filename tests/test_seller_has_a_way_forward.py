"""«Нельзя поменять им статус, только удалить.»

The owner's sentence about his own board, and it was literally true of the
HTTP surface rather than of any one row.

``LISTING_TRANSITIONS`` describes a lifecycle with a way out of every state:
ARCHIVED goes back to DRAFT, PAUSED returns to PUBLISHED, EXPIRED can be
renewed, SOLD can be relisted, REJECTED and BLOCKED can be reworked. The
owner API exposed **two** of those edges — ``archive`` and ``complete`` —
and both of them are exits. Every state a seller could reach by pressing a
button in the cabinet was therefore a one-way door, and the only call left
that still answered on such a row was ``DELETE``.

A live stand held 40 listings in exactly that position (25 approved, 15 the
machine had flagged), plus 167 drafts announcing ``moderation_status:
pending`` about content that had never been submitted to anything — no case
existed for a single one of them. The cabinet was not lying about the data;
the data was saying the wrong thing.

Two mechanisms are pinned here, and the point of both is that the answer
lives in ONE place:

* ``OWNER_TRANSITIONS`` is the seller's half of the state machine, declared
  next to the machine it is a subset of. The route validates against it and
  the serializer reports it, so what a client is offered and what the server
  accepts cannot drift.
* a listing that has never been submitted says so, instead of borrowing the
  word the moderation queue uses.
"""
import pytest

from stapel_listings.models import (
    LISTING_TRANSITIONS,
    Listing,
    ListingStatus,
    ModerationStatus,
    OWNER_TRANSITIONS,
)

pytestmark = pytest.mark.django_db

MINE = "/listings/listings/my/listings/"


def _url(listing, action):
    return f"/listings/listings/{listing.pk}/{action}/"


def _publishable(user, status):
    """A row in *status* whose draft would survive a publish.

    The same content ``conftest.draft_listing`` carries, per status: since
    Д193 one owner edge runs the publish service, so a row with an empty
    draft is refused for its CONTENT and no longer tests the edge.
    """
    return Listing.objects.create(
        owner=user,
        category_id="7",
        status=status,
        title_draft="Toyota Camry",
        description_draft="A well kept car in great condition.",
        price_draft="15000.00",
        currency="EUR",
        images_draft=["product/abc123"],
        lat_draft="55.755800",
        lon_draft="37.617300",
        location_label_draft="Tverskaya 7, Moscow",
        features_draft={
            "mileage": {"type": "int", "value": 42000},
            "condition": {"type": "select", "value": ["used"]},
        },
    )


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


# ── The seller's half of the state machine ───────────────────────────


def test_owner_transitions_is_a_subset_of_the_machine():
    """A seller may never be offered an edge the FSM would refuse.

    Declared as a subset rather than as a second table, because two tables
    are two things to keep in step and this is the pair that would drift
    silently: the route would 409 on an edge the card had just advertised.
    """
    for source, targets in OWNER_TRANSITIONS.items():
        assert targets, f"{source} must offer the seller SOMETHING"
        assert targets <= LISTING_TRANSITIONS[source], (
            f"{source} offers the owner an edge the state machine refuses"
        )


def test_every_status_a_seller_can_reach_has_a_way_out():
    """The defect, stated as a property.

    Not "ARCHIVED has a route" — that is the bug report. Any status a seller
    can be sitting in must offer at least one move that is not deletion, or
    the next state added to the enum reproduces this whole ticket.
    """
    for status in ListingStatus.values:
        assert OWNER_TRANSITIONS.get(status), (
            f"a listing in {status!r} offers its owner nothing but DELETE"
        )


def test_the_owner_cannot_approve_their_own_listing():
    """The one edge that must NOT be in the seller's half.

    PENDING -> PUBLISHED is moderation's decision. It is in
    ``LISTING_TRANSITIONS`` because ``apply_moderation`` drives it; a subset
    that included it would be a self-service publish gate.
    """
    assert ListingStatus.PUBLISHED not in OWNER_TRANSITIONS[ListingStatus.PENDING]
    assert ListingStatus.PUBLISHED not in OWNER_TRANSITIONS[ListingStatus.BLOCKED]


# ── The route ────────────────────────────────────────────────────────


def test_an_archived_listing_can_be_taken_back_to_draft(auth_client, user):
    """The owner's row, and the whole complaint, in one call."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.ARCHIVED
    )
    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.DRAFT}, format="json"
    )
    assert resp.status_code == 200, resp.content
    assert resp.data["status"] == ListingStatus.DRAFT
    listing.refresh_from_db()
    assert listing.status == ListingStatus.DRAFT


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (ListingStatus.ARCHIVED, ListingStatus.DRAFT),
        (ListingStatus.REJECTED, ListingStatus.DRAFT),
        (ListingStatus.BLOCKED, ListingStatus.DRAFT),
        (ListingStatus.PAUSED, ListingStatus.PUBLISHED),
        (ListingStatus.SOLD, ListingStatus.PUBLISHED),
        (ListingStatus.PUBLISHED, ListingStatus.PAUSED),
        (ListingStatus.EXPIRED, ListingStatus.PENDING),
    ],
)
def test_each_offered_edge_actually_works(auth_client, user, start, target):
    """Offered and accepted are the same list, exercised one row at a time."""
    listing = Listing.objects.create(owner=user, category_id="7", status=start)
    resp = auth_client.post(_url(listing, "transition"), {"to": target}, format="json")
    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.status == target


def test_an_edge_the_seller_does_not_own_is_refused(auth_client, user):
    """A pending listing cannot be published by the person who wrote it."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )
    assert resp.status_code == 409, resp.content
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PENDING


def test_an_unknown_target_status_is_a_400_not_a_500(auth_client, user):
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.ARCHIVED
    )
    resp = auth_client.post(
        _url(listing, "transition"), {"to": "on_the_moon"}, format="json"
    )
    assert resp.status_code == 400, resp.content


def test_a_stranger_cannot_move_someone_elses_listing(api_client, user, other_user):
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.ARCHIVED
    )
    api_client.force_authenticate(user=other_user)
    resp = api_client.post(
        _url(listing, "transition"), {"to": ListingStatus.DRAFT}, format="json"
    )
    # 403 + `listing_not_owner`, the same refusal `_get_own` gives every other
    # owner action — the new route joins the module's dialect, it does not
    # invent a second one.
    assert resp.status_code == 403, resp.content
    listing.refresh_from_db()
    assert listing.status == ListingStatus.ARCHIVED


def test_the_named_actions_still_work(auth_client, user):
    """``archive`` and ``complete`` keep their URLs — a storefront is shipped."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PUBLISHED
    )
    assert auth_client.post(_url(listing, "archive")).status_code == 200
    listing.refresh_from_db()
    assert listing.status == ListingStatus.ARCHIVED


# ── What the cabinet is told ─────────────────────────────────────────


def test_the_card_carries_the_moves_the_seller_actually_has(auth_client, user):
    """The client must not have to derive this, because it derived it wrong.

    The cabinet had ``status`` and ``moderation_status`` and nothing else, so
    "what can I do with this row" was a guess reimplemented per surface. The
    server owns the state machine; it can answer.
    """
    Listing.objects.create(owner=user, category_id="7", status=ListingStatus.ARCHIVED)
    resp = auth_client.get(MINE)
    assert resp.status_code == 200, resp.content
    card = resp.data["items"][0]
    # Д193: «опубликовать снова» is derived from this set, so an archived row
    # gets the button without one line changing in the cabinet.
    assert card["available_transitions"] == [
        ListingStatus.DRAFT,
        ListingStatus.PUBLISHED,
    ]


def test_the_offered_moves_are_the_accepted_moves(
    auth_client, user, stub_categories, stub_geo
):
    """The anti-drift assertion: whatever the card offers, the route takes.

    Rows carry a PUBLISHABLE draft, because since Д193 one of the offered
    edges (ARCHIVED -> PUBLISHED) is a publication and refuses invalid
    content with a 400. That refusal is about the draft, not about the edge —
    a row with nothing in it would make this gate measure the wrong thing.
    """
    for status in ListingStatus.values:
        listing = _publishable(user, status)
        card = next(
            row
            for row in auth_client.get(MINE).data["items"]
            if row["id"] == listing.pk
        )
        for target in card["available_transitions"]:
            fresh = _publishable(user, status)
            resp = auth_client.post(
                _url(fresh, "transition"), {"to": target}, format="json"
            )
            assert resp.status_code == 200, (
                f"the card offered {status} -> {target} and the route refused it: "
                f"{resp.content}"
            )


# ── «Опубликовать снова» on an archived row (Д193) ───────────────────
#
# ARCHIVED could only go back to DRAFT, so a seller who filed a listing away
# had to walk the whole composer again to sell the same thing twice. The edge
# added here is not a lifecycle hop: it runs the publish service, so the
# content is re-validated, re-promoted and re-submitted for review, and it
# lands wherever the fleet's moderation policy lands a publication.


def test_a_restore_is_offered_as_published_so_the_cabinet_needs_no_change(
    auth_client, user
):
    from stapel_listings.models import owner_transitions_for
    from stapel_listings.views import RESTORE_EDGES

    assert ListingStatus.PUBLISHED in owner_transitions_for(ListingStatus.ARCHIVED)
    for source, target in RESTORE_EDGES:
        assert target in OWNER_TRANSITIONS[source], (
            f"{source} -> {target} is handled as a restore but is not an edge "
            "the owner is ever offered"
        )


def test_restoring_an_archived_listing_re_enters_moderation(
    auth_client, user, stub_categories, stub_geo, capture_events
):
    """The default (pre) gate: one tap re-opens the case, nothing goes live.

    The status that comes back is the one the listing is actually IN, which
    under this gate is PENDING and not the PUBLISHED that was asked for — a
    cabinet that painted the row from its own request would be lying to the
    seller.
    """
    listing = _publishable(user, ListingStatus.ARCHIVED)
    submitted = capture_events("listing.submitted")

    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )

    assert resp.status_code == 200, resp.content
    assert resp.data["status"] == ListingStatus.PENDING
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PENDING
    assert listing.moderation_status == ModerationStatus.PENDING
    assert len(submitted) == 1


def test_restoring_under_the_post_gate_goes_back_on_sale_at_once(
    auth_client, user, stub_categories, stub_geo, settings, capture_events
):
    settings.STAPEL_LISTINGS = {"MODERATION_GATE": "post"}
    listing = _publishable(user, ListingStatus.ARCHIVED)
    published = capture_events("listing.published")

    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )

    assert resp.status_code == 200, resp.content
    assert resp.data["status"] == ListingStatus.PUBLISHED
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PUBLISHED
    # Live is not approved: the case is still open.
    assert listing.moderation_status == ModerationStatus.PENDING
    assert len(published) == 1
    assert Listing.objects.published().filter(pk=listing.pk).exists()


def test_a_restore_promotes_the_draft_and_restarts_the_clock(
    auth_client, user, stub_categories, stub_geo, settings
):
    """A restore publishes the CURRENT draft, not the snapshot it was filed with.

    Same promotion a first publication does — including a fresh TTL, so six
    months in the archive do not bring a listing back already expired.
    """
    from django.utils import timezone

    settings.STAPEL_LISTINGS = {"AUTO_APPROVE_ON_PUBLISH": True}
    listing = _publishable(user, ListingStatus.ARCHIVED)
    listing.title_draft = "Toyota Camry, second run"
    listing.save(update_fields=["title_draft"])

    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )

    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PUBLISHED
    assert listing.moderation_status == ModerationStatus.APPROVED
    assert listing.title == "Toyota Camry, second run"
    assert listing.expires_at is None or listing.expires_at > timezone.now()


def test_a_restore_of_an_unpublishable_draft_is_refused_not_shipped(
    auth_client, user, stub_categories, stub_geo
):
    """The seller gets the composer's refusal, not a blank listing on sale."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.ARCHIVED
    )
    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )
    assert resp.status_code == 400, resp.content
    listing.refresh_from_db()
    assert listing.status == ListingStatus.ARCHIVED


def test_the_archive_is_not_a_laundry_for_a_takedown(
    auth_client, user, stub_categories, stub_geo, capture_events
):
    """BLOCKED -> ARCHIVED -> PUBLISHED must not be a self-service reinstate.

    ``BLOCKED -> PUBLISHED`` is kept out of the seller's half of the machine
    on purpose. ARCHIVED is reachable from BLOCKED, so a restore that was a
    bare FSM hop would hand every taken-down listing a two-press route back
    into the index. Going through the publish service is what closes it: the
    verdict is cleared back to PENDING and a case is re-opened, so the row
    faces moderation again instead of inheriting an old answer.
    """
    listing = _publishable(user, ListingStatus.BLOCKED)
    listing.moderation_status = ModerationStatus.REJECTED
    listing.save(update_fields=["moderation_status"])
    auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.ARCHIVED}, format="json"
    )
    submitted = capture_events("listing.submitted")

    resp = auth_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )

    assert resp.status_code == 200, resp.content
    listing.refresh_from_db()
    assert listing.status == ListingStatus.PENDING
    assert listing.moderation_status == ModerationStatus.PENDING
    assert len(submitted) == 1
    assert not Listing.objects.published().filter(pk=listing.pk).exists()


def test_a_stranger_cannot_restore_someone_elses_listing(
    api_client, user, other_user, stub_categories, stub_geo
):
    listing = _publishable(user, ListingStatus.ARCHIVED)
    api_client.force_authenticate(user=other_user)
    resp = api_client.post(
        _url(listing, "transition"), {"to": ListingStatus.PUBLISHED}, format="json"
    )
    assert resp.status_code == 403, resp.content
    listing.refresh_from_db()
    assert listing.status == ListingStatus.ARCHIVED


# ── A draft nobody has submitted is not «на модерации» ───────────────


def test_a_new_draft_does_not_claim_it_is_awaiting_moderation(auth_client, user):
    """167 rows on a live stand said `pending` with no case behind any of them.

    ``pending`` is a claim about a queue — somebody is waiting on a decision.
    A draft that has never been published has not asked anyone anything, and
    a cabinet that renders ``moderation_status`` verbatim tells its owner his
    blank draft is under review.
    """
    listing = Listing.objects.create(owner=user, category_id="7")
    assert listing.status == ListingStatus.DRAFT
    assert listing.moderation_status == ModerationStatus.NOT_SUBMITTED

    card = auth_client.get(MINE).data["items"][0]
    assert card["moderation_status"] == ModerationStatus.NOT_SUBMITTED


def test_publishing_is_what_makes_moderation_pending(
    auth_client, draft_listing, stub_categories, stub_geo
):
    """And the word means what it says from that moment on."""
    from stapel_listings.services import publish as publish_service

    assert draft_listing.moderation_status == ModerationStatus.NOT_SUBMITTED
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()
    assert draft_listing.moderation_status == ModerationStatus.PENDING


def test_not_submitted_is_never_a_moderation_verdict(user):
    """``apply_moderation`` writes the three real states and never this one."""
    listing = Listing.objects.create(
        owner=user, category_id="7", status=ListingStatus.PENDING
    )
    for decision in ("approved", "rejected", "needs_review"):
        listing.status = ListingStatus.PENDING
        listing.save(update_fields=["status"])
        listing.apply_moderation(decision, auto_publish=False)
        listing.refresh_from_db()
        assert listing.moderation_status != ModerationStatus.NOT_SUBMITTED
