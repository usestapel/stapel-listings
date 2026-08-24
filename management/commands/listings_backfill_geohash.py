"""Stamp ``geohash``/``geohash_draft`` on listings written before
``Listing.save()`` called ``geo.geohash_encode``.

    python manage.py listings_backfill_geohash [--batch-size N] [--limit N] [--dry-run]

Every listing has always been free to carry ``lat``/``lon`` (or the draft
twins) without a matching geohash — that was the bug this release fixes for
new writes (see ``Listing.compute_geohash_draft()``). This command is the
one-time (or rerunnable) pass over the rows that predate the fix: stamps a
geohash on every row that has coordinates and none, via the
``geo.geohash_encode`` comm Function (stapel-geo consumed by name, same as
every runtime call site — no hard dependency).

Idempotent and resumable by construction — it only ever touches rows where
the geohash column is still empty, so a re-run after a crash picks up
exactly what the crash left, and a second full run is a no-op. A row
``geo.geohash_encode`` cannot answer for (stapel-geo not deployed/reachable)
is left unstamped and counted as ``unresolved``, not an error — rerun once
geo is reachable.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Backfill Listing.geohash / geohash_draft from stored lat/lon via "
        "geo.geohash_encode, for listings that predate stamp-on-save."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Listings updated per bulk_update() call (default 500).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Stop after this many candidate rows, applied independently "
                "to the published and draft passes. For running the "
                "backfill in bounded slices on a large table."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count candidates and report, write nothing.",
        )

    def handle(self, *args, **options):
        from ...services.geohash_backfill import backfill_geohashes

        result = backfill_geohashes(
            batch_size=options["batch_size"],
            limit=options["limit"],
            dry_run=options["dry_run"],
        )
        verb = "would stamp" if options["dry_run"] else "stamped"
        for population, stats in result.items():
            self.stdout.write(
                f"listings_backfill_geohash [{population}]: "
                f"{stats['candidates']} candidate(s), {verb} "
                f"{stats['stamped']} geohash(es), {stats['unresolved']} left "
                f"unresolved (geo unreachable or no answer)."
            )
        any_unresolved = any(s["unresolved"] for s in result.values())
        any_stamped = any(s["stamped"] for s in result.values())
        if any_unresolved and not any_stamped and not options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "Every candidate was left unresolved — geo.geohash_encode "
                    "answered nothing. Check stapel-geo is deployed and "
                    "reachable (INSTALLED_APPS / FUNCTION_TRANSPORT routing) "
                    "before assuming there is nothing left to backfill."
                )
            )
