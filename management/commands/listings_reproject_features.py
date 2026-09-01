"""Refresh the stored attribute projections of existing listings.

    python manage.py listings_reproject_features [--category ID[,ID...]]
                                                 [--batch-size N] [--dry-run]
                                                 [--strict]

``publish_listing`` snapshots ``features`` / ``features_title`` /
``features_badges`` / ``features_search`` at publish time so a card can render
without fetching the category. That snapshot is only ever as fresh as the last
publish, and there has never been a way to refresh it. This command is that
way: it re-derives the four columns from each listing's stored
``features_draft`` and the CURRENT category schema, through the same
``services.features.build_projections`` that publish uses, and writes back
nothing else.

The immediate reason it exists: listings published before stapel-attributes
0.7.0 carry ``select`` DAOs with no ``labels``, so their cards print storage
slugs where the display copy belongs. The standing reason: ``ref_select``'s
label snapshot and any category whose option copy an owner edits go stale the
same way.

**One bad field no longer costs a listing its whole repair.** Until 0.13.0 a
draft that failed ``validate_dto`` anywhere was skipped entirely, so a single
attribute that had drifted out of its category's bounds left every OTHER field
on that listing printing storage slugs — measured on this fleet as 12 listings
stuck on a stale shape. Now each field is judged on its own: the valid ones are
re-projected, the invalid ones keep their stored DAO (dropping them would turn a
stale value into a missing one) and are reported per listing, loudly, with the
slug and the engine's message. The exit code is non-zero only when the run
repaired NOTHING it was asked to repair — an invalid field that was worked
around is a report, not a failure, which is the whole point.

It refreshes a derived projection; it is NOT a re-publication. Lifecycle
status, moderation status, expiry and timestamps are untouched and no
``listing.submitted`` is emitted. ``listing.updated`` IS emitted (by
``Listing.save()``) for every changed listing that is in an indexed status,
deliberately — a search index serving the old text is exactly the damage this
repairs — and the run reports how many of those events it produced. See
``services/reproject.py`` for the full argument.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Re-derive Listing.features / features_title / features_badges / "
        "features_search from the stored draft and the current category "
        "schema. Refreshes a stale write-time snapshot; does not re-publish."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--category",
            action="append",
            default=None,
            metavar="ID",
            help=(
                "Limit to these category ids. Repeatable and/or "
                "comma-separated (--category 7 --category 8,9). Default: "
                "every listing that has projections."
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows fetched per database chunk (default 500).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and why rows are skipped; write nothing.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help=(
                "Exit non-zero if ANY field could not be re-derived, not only "
                "when nothing was repaired (for CI / monitoring)."
            ),
        )

    def handle(self, *args, **options):
        from ...services.reproject import repair_failures, reproject_listings

        category_ids = _split_categories(options.get("category"))
        dry_run = options["dry_run"]

        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be >= 1")

        result = reproject_listings(
            category_ids=category_ids,
            batch_size=options["batch_size"],
            dry_run=dry_run,
        )

        scope = (
            f"categories {', '.join(category_ids)}" if category_ids else "all categories"
        )
        verb = "would re-project" if dry_run else "re-projected"
        self.stdout.write(
            f"listings_reproject_features [{scope}]"
            f"{' (DRY RUN — nothing written)' if dry_run else ''}: "
            f"{result['examined']} examined, {verb} {result['changed']}, "
            f"{result['unchanged']} already current, {result['skipped']} skipped."
        )

        for reason, count in result["skipped_by_reason"].items():
            if not count:
                continue
            ids = result["skipped_ids"][reason]
            shown = ", ".join(str(i) for i in ids)
            more = f" (first {len(ids)} of {count})" if count > len(ids) else ""
            self.stdout.write(
                self.style.WARNING(
                    f"  skipped {count} as {reason}{more}: {shown}"
                )
            )

        emitted = result["events_emitted"]
        self.stdout.write(
            f"  listing.updated: {emitted} "
            f"{'would be' if dry_run else ''} emitted "
            f"(changed listings in an indexed status — a search index holding "
            f"the old text is what this repairs)."
        )

        if result["examined"] == 0:
            self.stdout.write(
                self.style.WARNING(
                    "No listing carries projections in this scope — nothing to "
                    "re-project. Check the --category ids if that is a surprise."
                )
            )

        self._report_invalid_fields(result)

        # Non-zero means "this run repaired nothing it was asked to repair" —
        # not "some field was invalid". A field that was worked around is
        # exactly what the per-field repair is for, and failing the run over
        # one would put back the all-or-nothing gate it replaced. `--strict`
        # is there for the caller who does want that stricter reading.
        #
        # `repair_failures` and not `skipped`: a row with no draft is not
        # damage, it is a row this pass does not apply to.
        failed = repair_failures(result)
        if failed and not result["changed"]:
            raise CommandError(
                f"repaired nothing: {failed} listing(s) could not be repaired "
                f"and 0 were re-projected. See the skip reasons above."
            )
        if options["strict"] and result["invalid_field_count"]:
            raise CommandError(
                f"--strict: {result['invalid_field_count']} field(s) across "
                f"{len(result['invalid_fields'])} listing(s) could not be "
                "re-derived and kept their stored values."
            )

    def _report_invalid_fields(self, result):
        """Per listing, per field, with the reason — never a bare count.

        A repair that hides what it worked around is how a catalogue rots
        quietly: the run goes green, the numbers look better every time, and
        the fields nobody can fix are never named to anybody.
        """
        invalid = result["invalid_fields"]
        if not invalid:
            return

        listings = len(invalid)
        total = result["invalid_field_count"]
        self.stdout.write(
            self.style.WARNING(
                f"  {total} field(s) on {listings} listing(s) could not be "
                f"re-derived and KEPT THEIR STORED VALUES "
                f"({result['repaired_with_invalid_fields']} of those listings "
                f"were still repaired in their other fields):"
            )
        )
        for listing_id, failures in invalid.items():
            for slug, message in failures.items():
                self.stdout.write(
                    self.style.WARNING(f"    listing {listing_id} [{slug}]: {message}")
                )
        if result["invalid_fields_truncated"]:
            self.stdout.write(
                self.style.WARNING(
                    "    (listing sample truncated; the counts above are exact "
                    "and every field is in the log)"
                )
            )


def _split_categories(values):
    """``["7", "8,9"]`` -> ``["7", "8", "9"]``; ``None``/empty -> ``None``."""
    if not values:
        return None
    ids = [part.strip() for value in values for part in str(value).split(",")]
    return [i for i in ids if i] or None
