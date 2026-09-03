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
    assert card["available_transitions"] == [ListingStatus.DRAFT]


def test_the_offered_moves_are_the_accepted_moves(auth_client, user):
    """The anti-drift assertion: whatever the card offers, the route takes."""
    for status in ListingStatus.values:
        listing = Listing.objects.create(owner=user, category_id="7", status=status)
        card = next(
            row
            for row in auth_client.get(MINE).data["items"]
            if row["id"] == listing.pk
        )
        for target in card["available_transitions"]:
            fresh = Listing.objects.create(
                owner=user, category_id="7", status=status
            )
            resp = auth_client.post(
                _url(fresh, "transition"), {"to": target}, format="json"
            )
            assert resp.status_code == 200, (
                f"the card offered {status} -> {target} and the route refused it: "
                f"{resp.content}"
            )


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
