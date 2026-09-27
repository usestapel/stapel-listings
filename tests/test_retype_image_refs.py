"""listings_retype_image_refs: photo refs follow a CDN asset-type move."""
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from stapel_listings.models import Listing

pytestmark = pytest.mark.django_db


@pytest.fixture
def capture_sync(monkeypatch):
    import stapel_core.django.cdn.ref_sync as ref_sync

    calls = []

    def fake_sync(service, entity_type, entity_id, old_refs, new_refs):
        calls.append((entity_id, set(old_refs), set(new_refs)))
        return ref_sync.RefSyncResult(ok=True)

    monkeypatch.setattr(ref_sync, "sync_cdn_refs", fake_sync)
    return calls


def _listing(user, images, draft):
    return Listing.objects.create(
        owner=user, category_id="7", title_draft="Photo", images=images, images_draft=draft
    )


def _run(*args):
    out = StringIO()
    call_command("listings_retype_image_refs", *args, stdout=out)
    return out.getvalue()


def test_dry_run_reports_and_writes_nothing(user, capture_sync):
    row = _listing(user, ["avatar/aa"], ["avatar/aa", "product/bb"])
    capture_sync.clear()

    out = _run("--from", "avatar", "--to", "product", "--dry-run")

    assert "1 listing(s), 2 reference(s)" in out
    row.refresh_from_db()
    assert row.images == ["avatar/aa"]
    assert capture_sync == []


def test_refs_are_rewritten_in_order_and_claims_move(user, capture_sync):
    row = _listing(user, ["avatar/aa", "product/bb"], ["product/bb", "avatar/aa", "avatar/cc"])
    untouched = _listing(user, ["product/zz"], ["product/zz"])
    capture_sync.clear()

    _run("--from", "avatar", "--to", "product")

    row.refresh_from_db()
    untouched.refresh_from_db()
    assert row.images == ["product/aa", "product/bb"]
    assert row.images_draft == ["product/bb", "product/aa", "product/cc"]
    assert untouched.images == ["product/zz"]
    assert capture_sync == [
        (
            row.pk,
            {"avatar/aa", "product/bb", "avatar/cc"},
            {"product/aa", "product/bb", "product/cc"},
        )
    ]


def test_a_rewrite_that_meets_an_existing_ref_keeps_one(user, capture_sync):
    row = _listing(user, [], ["avatar/aa", "product/aa"])

    _run("--from", "avatar", "--to", "product")

    row.refresh_from_db()
    assert row.images_draft == ["product/aa"]


def test_same_type_is_refused(user):
    with pytest.raises(CommandError):
        _run("--from", "product", "--to", "product")
