"""Claim CDN media for listings written before claim-on-save (0.14.0).

    python manage.py listings_backfill_cdn_refs [--limit N] [--dry-run]

``Listing.save()`` only announces a claim when the claimed set MOVES, so a
re-save of an unchanged listing publishes nothing — rows that predate
0.14.0 stay zero-ref and stapel-cdn's orphan sweeper would reap their
photos. This command publishes an ADDITIVE claim (``old_hashes=[]``) for
every live listing that references media: idempotent and rerunnable by
construction (nothing is ever released), safe to run before or after the
sweeper deploys, and a failed bus publish is counted, not raised — rerun
once the bus is reachable. See ``services/cdn_refs_backfill.py``.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Publish additive CDN ref claims (stapel.cdn.ref-sync) for every "
        "live listing's images/images_draft, for rows that predate "
        "claim-on-save."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Stop after this many candidate rows (rows that carry refs). "
                "For running the backfill in bounded slices on a large table."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count candidates and report, publish nothing.",
        )

    def handle(self, *args, **options):
        from ...services.cdn_refs_backfill import backfill_cdn_refs

        stats = backfill_cdn_refs(
            limit=options["limit"], dry_run=options["dry_run"]
        )
        verb = "would claim" if options["dry_run"] else "claimed"
        self.stdout.write(
            f"listings_backfill_cdn_refs: {stats['candidates']} candidate(s), "
            f"{verb} {stats['candidates'] if options['dry_run'] else stats['published']} "
            f"claim set(s), {stats['failed']} failed to publish."
        )
        if stats["failed"] and not stats["published"]:
            self.stdout.write(
                self.style.WARNING(
                    "Every publish failed — the bus is unreachable. Nothing "
                    "was claimed; rerun once the broker is up, and do NOT "
                    "let the CDN sweeper run before this pass succeeds."
                )
            )
