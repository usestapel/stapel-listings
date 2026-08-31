"""Refresh the stored attribute projections of existing listings.

    python manage.py listings_reproject_features [--category ID[,ID...]]
                                                 [--batch-size N] [--dry-run]

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

It refreshes a derived projection; it is NOT a re-publication. Lifecycle
status, moderation status, expiry and timestamps are untouched and no
``listing.submitted`` is emitted. ``listing.updated`` IS emitted (by
``Listing.save()``) for every changed listing that is in an indexed status,
deliberately — a search index serving the old text is exactly the damage this
repairs — and the run reports how many of those events it produced. See
``services/reproject.py`` for the full argument.
"""
from django.core.management.base import BaseCommand


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

    def handle(self, *args, **options):
        from ...services.reproject import reproject_listings

        category_ids = _split_categories(options.get("category"))
        dry_run = options["dry_run"]

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


def _split_categories(values):
    """``["7", "8,9"]`` -> ``["7", "8", "9"]``; ``None``/empty -> ``None``."""
    if not values:
        return None
    ids = [part.strip() for value in values for part in str(value).split(",")]
    return [i for i in ids if i] or None
