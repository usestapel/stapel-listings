"""«Открываю черновик — а он пустой.»

Every user-editable field on a listing is a ``*_draft`` twin, promoted to its
published sibling only at publish. Three calls WROTE that bag and echoed it
back — ``create``, ``update``, ``save-draft`` — and not one call READ it. A
composer reopening a listing by id therefore had exactly one source, the
detail read, and the detail read serves the PUBLISHED fields: empty on
everything that has never been published, stale on anything edited since. The
seller pressed «продолжить», got a blank form, and their text and photos were
in the row the whole time.

``GET listings/{id}/draft/`` is the read half of ``save-draft``: the same
payload, the same serializer, behind the module's one ownership gate. What is
pinned here is that pairing (write it, read the same thing back) and the
boundary: a draft field is the OWNER's, and no other audience gets one.
"""
import pytest

from stapel_listings.models import Listing, ListingStatus

pytestmark = pytest.mark.django_db


def _draft_url(listing):
    return f"/listings/listings/{listing.pk}/draft/"


def _detail_url(listing):
    return f"/listings/listings/{listing.pk}/"


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


def test_the_owner_reads_back_the_draft_they_saved(auth_client, draft_listing):
    """The defect, closed: what the composer wrote comes back to the composer."""
    resp = auth_client.get(_draft_url(draft_listing))

    assert resp.status_code == 200, resp.content
    assert resp.data["title_draft"] == "Toyota Camry"
    assert resp.data["description_draft"] == "A well kept car in great condition."
    assert resp.data["images_draft"] == ["product/abc123"]
    assert resp.data["location_label_draft"] == draft_listing.location_label_draft
    assert resp.data["features_draft"]["mileage"]["value"] == 42000
    assert resp.data["category_id"] == "7"


def test_a_save_draft_round_trips(auth_client, draft_listing):
    """The write and the read are one bag, not two shapes that agree today."""
    written = auth_client.post(
        f"/listings/listings/{draft_listing.pk}/save-draft/",
        {"title_draft": "Toyota Camry 2.5", "description_draft": "Repainted once."},
        format="json",
    )
    assert written.status_code == 200, written.content

    read = auth_client.get(_draft_url(draft_listing))
    assert read.status_code == 200, read.content
    assert read.data == written.data


def test_an_unpublished_listing_is_empty_on_the_detail_read_and_full_here(
    auth_client, draft_listing
):
    """Both halves of the report in one assertion pair.

    The detail read is not wrong — a listing that has never been published
    has no published title. It is simply not the read a composer wants, and
    it was the only one there was.
    """
    detail = auth_client.get(_detail_url(draft_listing))
    assert detail.status_code == 200, detail.content
    assert detail.data["title"] == ""
    assert detail.data["images"] in ([], None)

    draft = auth_client.get(_draft_url(draft_listing))
    assert draft.data["title_draft"] == "Toyota Camry"
    assert draft.data["images_draft"] == ["product/abc123"]


# ── the boundary: a draft belongs to exactly one person ──────────────


def test_a_stranger_is_refused(api_client, draft_listing, other_user):
    api_client.force_authenticate(user=other_user)
    resp = api_client.get(_draft_url(draft_listing))
    # 403 + `listing_not_owner`: the module's one ownership gate, the same
    # refusal every other owner-only call gives.
    assert resp.status_code == 403, resp.content
    assert b"_draft" not in resp.content


def test_an_anonymous_reader_is_refused(api_client, draft_listing):
    resp = api_client.get(_draft_url(draft_listing))
    assert resp.status_code in (401, 403), resp.content
    assert b"_draft" not in resp.content


def test_an_absent_listing_is_a_404(auth_client):
    assert auth_client.get("/listings/listings/424242/draft/").status_code == 404


def test_a_soft_deleted_listing_is_a_404(auth_client, draft_listing):
    draft_listing.delete()
    assert auth_client.get(_draft_url(draft_listing)).status_code == 404


def test_no_public_read_carries_a_draft_field(
    api_client, user, draft_listing, stub_categories, stub_geo, settings
):
    """The reason this is a separate route and not columns on the detail read.

    A shape whose key set depends on who is asking is where a redaction bug
    hides. Here the public shapes simply do not have the columns — asserted
    over the whole key set, so a draft twin added to a public serializer
    later trips this rather than shipping.
    """
    from stapel_listings.services import publish as publish_service

    settings.STAPEL_LISTINGS = {"AUTO_APPROVE_ON_PUBLISH": True}
    publish_service.publish_listing(draft_listing)
    draft_listing.refresh_from_db()

    detail = api_client.get(_detail_url(draft_listing))
    assert detail.status_code == 200, detail.content
    assert [k for k in detail.data if k.endswith("_draft")] == []

    listed = api_client.get("/listings/listings/")
    assert listed.status_code == 200, listed.content
    card = next(
        row for row in listed.data["items"] if row["id"] == draft_listing.pk
    )
    assert [k for k in card if k.endswith("_draft")] == []


def test_the_owners_own_card_still_carries_only_its_three_twins(
    auth_client, draft_listing
):
    """The owner's LIST card is unchanged: this release adds a read, not columns.

    ``my/listings`` carries ``title_draft``/``price_draft``/``images_draft``
    so a drafts tab can render rows. The rest of the bag — description,
    features, the location trio — is composer-sized and belongs to the
    per-row read, not to a page of cards.
    """
    card = auth_client.get("/listings/listings/my/listings/").data["items"][0]
    twins = sorted(k for k in card if k.endswith("_draft"))
    assert twins == ["images_draft", "price_draft", "title_draft"]


def test_a_listing_in_any_status_is_readable_by_its_owner(auth_client, user):
    """Including the states the seller cannot edit from — ARCHIVED especially.

    «Опубликовать снова» hands the seller back to the composer when the draft
    needs work, and a read that 409'd on a filed-away listing would strand
    exactly the row Д193 is about.
    """
    for status in ListingStatus.values:
        listing = Listing.objects.create(
            owner=user, category_id="7", status=status, title_draft="x"
        )
        resp = auth_client.get(_draft_url(listing))
        assert resp.status_code == 200, (status, resp.content)
        assert resp.data["title_draft"] == "x"
