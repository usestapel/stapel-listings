"""Rewrite listing photo references from one CDN asset type to another.

    python manage.py listings_retype_image_refs --from avatar --to product [--dry-run]

The listings half of stapel-cdn's ``retype_images``: once the CDN has moved
the rows (``retype_images --from avatar --to product --claimed-by
listings/listing``), every ``<from>/<hash>`` in ``images`` and
``images_draft`` becomes ``<to>/<hash>``. Order is kept; a reference that
would become a duplicate of one already present is dropped.

Written with a queryset update, so no moderation or publish side effect
fires; the claim change is announced to the CDN the same way ``save()``
does. Search documents still carry the old URLs until the index is rebuilt.
Soft-deleted listings are rewritten too (they claim nothing).
"""
from django.core.management.base import BaseCommand, CommandError


def _rewrite(refs, source, target):
    out = []
    for ref in refs or []:
        if isinstance(ref, str) and ref.startswith(source):
            ref = target + ref[len(source):]
        if ref not in out:
            out.append(ref)
    return out


class Command(BaseCommand):
    help = "Rewrite <from>/<hash> photo references of listings to <to>/<hash>."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="from_type", required=True)
        parser.add_argument("--to", dest="to_type", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from ...models import Listing

        src, dst = options["from_type"].strip("/"), options["to_type"].strip("/")
        if not src or not dst or src == dst or "/" in src or "/" in dst:
            raise CommandError("--from and --to must be two different asset type names")
        source, target = f"{src}/", f"{dst}/"

        rows = []
        for listing in Listing.all_objects.all().order_by("pk").iterator():
            images = _rewrite(listing.images, source, target)
            draft = _rewrite(listing.images_draft, source, target)
            if images != (listing.images or []) or draft != (listing.images_draft or []):
                rows.append((listing, images, draft))

        refs = sum(
            1
            for listing, _, _ in rows
            for ref in [*(listing.images or []), *(listing.images_draft or [])]
            if isinstance(ref, str) and ref.startswith(source)
        )
        self.stdout.write(
            f"listings_retype_image_refs: {len(rows)} listing(s), {refs} "
            f"reference(s) {source}* -> {target}*"
        )
        for listing, images, draft in rows:
            self.stdout.write(
                f"  listing {listing.pk} ({listing.status}): images {listing.images} -> {images}; "
                f"draft {listing.images_draft} -> {draft}"
            )
        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS("dry-run: nothing changed"))
            return

        for listing, images, draft in rows:
            old_claim = listing.cdn_image_refs()
            Listing.all_objects.filter(pk=listing.pk).update(images=images, images_draft=draft)
            new_claim = Listing._cdn_refs_of(images, draft, listing.deleted_at)
            Listing._sync_cdn_image_refs(listing.pk, old_claim, new_claim)
        self.stdout.write(self.style.SUCCESS(f"rewrote {len(rows)} listing(s)"))
