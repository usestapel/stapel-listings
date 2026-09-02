"""CDN reference claiming (owner mandate: the orphan sweeper must never reap
a live listing's photos, and photos dropped from a draft must become
unclaimed and get reaped).

The claimed set of a listing is the UNION of ``images`` and ``images_draft``
— a photo still on the published side stays claimed even while an edit drops
it from the draft — and it goes empty when the listing is deleted (soft or
hard). The sync lives in the model layer (``Listing.save()`` /
``hard_delete()``) so EVERY writer is covered: the draft serializer, the
publish promotion, moderation saves, soft delete, restore and GDPR erasure
all funnel through those two methods.

Boundary mocked: ``stapel_core.django.cdn.ref_sync.sync_cdn_refs`` — the same
seam stapel-profiles' avatar tests patch — plus ``stapel_core.bus.publish``
for the helper's own graceful ok=False path.
"""
import pytest

from stapel_listings.models import Listing
from stapel_listings.services import publish as publish_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def capture_sync(monkeypatch):
    """Record every ``sync_cdn_refs`` call as ``(service, entity_type,
    entity_id, old_set, new_set)`` without touching the bus."""
    import stapel_core.django.cdn.ref_sync as ref_sync

    calls = []

    def fake_sync(service, entity_type, entity_id, old_refs, new_refs):
        calls.append(
            (service, entity_type, entity_id, set(old_refs), set(new_refs))
        )
        return ref_sync.RefSyncResult(ok=True)

    monkeypatch.setattr(ref_sync, "sync_cdn_refs", fake_sync)
    return calls


class TestClaimOnAttach:
    def test_create_with_draft_images_claims_refs(self, user, capture_sync):
        listing = Listing.objects.create(
            owner=user,
            category_id="7",
            images_draft=["product/aaa1", "product/bbb2"],
        )
        assert capture_sync == [
            ("listings", "listing", listing.pk, set(), {"product/aaa1", "product/bbb2"})
        ]

    def test_create_without_images_syncs_nothing(self, user, capture_sync):
        Listing.objects.create(owner=user, category_id="7")
        assert capture_sync == []

    def test_adding_image_to_draft_claims_it(self, draft_listing, capture_sync):
        draft_listing.images_draft = ["product/abc123", "product/new456"]
        draft_listing.save()
        assert capture_sync == [
            (
                "listings",
                "listing",
                draft_listing.pk,
                {"product/abc123"},
                {"product/abc123", "product/new456"},
            )
        ]


class TestDropOnDetach:
    def test_removing_image_from_draft_drops_ref(self, draft_listing, capture_sync):
        draft_listing.images_draft = []
        draft_listing.save()
        (call,) = capture_sync
        assert call[3] == {"product/abc123"}
        assert call[4] == set()

    def test_published_side_keeps_ref_claimed_while_draft_drops_it(
        self, draft_listing, capture_sync
    ):
        """An edit that drops a photo from the draft must NOT unclaim it while
        the published listing still shows it — the claimed set is the union."""
        publish_service.publish_listing(draft_listing)  # images = [product/abc123]
        draft_listing.images_draft = ["product/other789"]
        draft_listing.save()
        _, _, _, old, new = capture_sync[-1]
        assert "product/abc123" in new  # still on the published side
        assert new == {"product/abc123", "product/other789"}

    def test_unrelated_save_does_not_sync(self, draft_listing, capture_sync):
        draft_listing.title_draft = "New title"
        draft_listing.save()
        assert capture_sync == []


class TestPublish:
    def test_publish_without_dropped_photo_removes_its_ref(
        self, draft_listing, capture_sync
    ):
        """Publish promotes images_draft -> images; a photo dropped from the
        draft beforehand leaves the union at that publish and gets reaped."""
        publish_service.publish_listing(draft_listing)  # images = [product/abc123]
        draft_listing.images_draft = ["product/keep111"]
        draft_listing.save()  # union {abc123, keep111}
        publish_service.publish_listing(draft_listing)  # images -> [keep111]
        _, _, _, old, new = capture_sync[-1]
        assert "product/abc123" not in new
        assert new == {"product/keep111"}
        assert "product/abc123" in old


class TestDelete:
    def test_soft_delete_releases_all_refs(self, draft_listing, capture_sync):
        draft_listing.delete()
        _, _, _, old, new = capture_sync[-1]
        assert old == {"product/abc123"}
        assert new == set()

    def test_restore_reclaims_refs(self, draft_listing, capture_sync):
        draft_listing.delete()
        draft_listing.restore()
        _, _, _, old, new = capture_sync[-1]
        assert old == set()
        assert new == {"product/abc123"}

    def test_hard_delete_releases_all_refs(self, draft_listing, capture_sync):
        pk = draft_listing.pk
        draft_listing.hard_delete()
        assert capture_sync[-1] == (
            "listings", "listing", pk, {"product/abc123"}, set()
        )

    def test_gdpr_erasure_releases_refs(self, draft_listing, user, capture_sync):
        from stapel_listings.gdpr import ListingsGDPRProvider

        pk = draft_listing.pk
        ListingsGDPRProvider().delete(user.id)
        assert (
            "listings", "listing", pk, {"product/abc123"}, set()
        ) in capture_sync
        assert not Listing.all_objects.filter(pk=pk).exists()


class TestGracefulDegradation:
    def test_sync_raising_does_not_block_save(self, draft_listing, monkeypatch):
        import stapel_core.django.cdn.ref_sync as ref_sync

        def boom(*args, **kwargs):
            raise RuntimeError("sync down")

        monkeypatch.setattr(ref_sync, "sync_cdn_refs", boom)
        draft_listing.images_draft = ["product/other789"]
        draft_listing.save()
        draft_listing.refresh_from_db()
        assert draft_listing.images_draft == ["product/other789"]

    def test_bus_publish_failure_does_not_block_save(self, draft_listing, monkeypatch):
        """The real helper's ok=False path: bus down -> warning, write lands."""
        import stapel_core.bus as bus

        def boom(topic, event):
            raise RuntimeError("bus down")

        monkeypatch.setattr(bus, "publish", boom)
        draft_listing.images_draft = []
        draft_listing.save()
        draft_listing.refresh_from_db()
        assert draft_listing.images_draft == []

    def test_bus_failure_does_not_block_delete(self, draft_listing, monkeypatch):
        import stapel_core.bus as bus

        def boom(topic, event):
            raise RuntimeError("bus down")

        monkeypatch.setattr(bus, "publish", boom)
        draft_listing.delete()
        assert draft_listing.is_deleted


class TestRefEventShape:
    def test_sync_publishes_to_the_ref_sync_topic(self, draft_listing):
        """End to end into the MemoryBus: the event apply_ref_sync consumes,
        keyed by the listing pk, carrying the '<type>/<hash>' strings the
        stored JSON lists already hold verbatim."""
        from stapel_core.bus import get_bus

        draft_listing.images_draft = ["product/abc123", "product/def456"]
        draft_listing.save()
        event = get_bus().events[-1]
        assert event.event_type == "cdn.ref.sync"
        assert event.payload["service"] == "listings"
        assert event.payload["entity_type"] == "listing"
        assert event.payload["entity_id"] == str(draft_listing.pk)
        assert set(event.payload["old_hashes"]) == {"product/abc123"}
        assert set(event.payload["new_hashes"]) == {
            "product/abc123",
            "product/def456",
        }
