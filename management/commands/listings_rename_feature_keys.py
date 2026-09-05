"""Move stored draft keys after a category renamed a feature slug.

    python manage.py listings_rename_feature_keys --category ID \\
        --rename old_slug=new_slug [--rename ...] [--dry-run]

The hand-run twin of the ``listings.rename_feature_keys`` comm Function, which
is what ``stapel-categories``' ``load_catalog --rename-features`` calls for
itself. Use this one when a rename already landed and the listings were left
behind — which is exactly how the defect it was written for was found.

**Why a rename is not a category-side edit.** ``features_draft`` is keyed by
feature slug. Rename the slug in the catalogue and every stored answer is
suddenly filed under a key the schema no longer knows: the facet empties, the
search projection loses the values on its next build, and
``listings_reproject_features`` — which reads the CURRENT slugs — would drop
them rather than repair them. On a live fleet on 2026-09-05 five car features
moved at once (``make_ref_select`` → ``make`` and four more) and every listing
in those categories lost its answers in one import.

This command moves the keys, keeps the values, re-projects the categories it
touched through the same pass ``listings_reproject_features`` runs, and lets
those writes emit ``listing.updated`` so a search index re-pulls.

A draft that already answers the NEW key as well as the old one is reported as
a conflict and left completely alone — no answer of a seller's is overwritten
by a guess. ``--dry-run`` writes nothing and reports the same counts.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Rewrite features_draft keys across a category subtree after a "
        "feature slug was renamed in the catalogue, then re-project."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--category",
            required=True,
            metavar="ID",
            help=(
                "Root of the subtree to rewrite. Descendants are included — a "
                "feature defined on a parent is answered by listings below it."
            ),
        )
        parser.add_argument(
            "--rename",
            action="append",
            required=True,
            metavar="OLD=NEW",
            help="A slug rename. Repeatable (--rename a=b --rename c=d).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would move and every conflict; write nothing.",
        )

    def handle(self, *args, **options):
        from ...services.rename_features import RenameError, rename_feature_keys

        renames = _parse_renames(options["rename"])
        try:
            result = rename_feature_keys(
                category_id=options["category"],
                renames=renames,
                dry_run=options["dry_run"],
            )
        except RenameError as exc:
            raise CommandError(str(exc))

        dry = result["dry_run"]
        pairs = ", ".join(f"{old} → {new}" for old, new in sorted(renames.items()))
        verb = "would rename" if dry else "renamed"
        self.stdout.write(
            f"listings_rename_feature_keys [category {options['category']}, "
            f"{len(result['categories'])} categor"
            f"{'y' if len(result['categories']) == 1 else 'ies'} in scope]"
            f"{' (DRY RUN — nothing written)' if dry else ''}: {pairs}"
        )
        self.stdout.write(
            f"  {result['listings_scanned']} listing(s) with a draft examined, "
            f"{verb} {result['keys_renamed']} key(s) across "
            f"{result['listings_changed']} listing(s)."
        )

        if not result["subtree_resolved"]:
            # Never implied. A run that could not walk the tree covered one
            # category, and a caller told otherwise would stop looking.
            self.stdout.write(self.style.WARNING(
                "  the category children provider did not answer — this run "
                "covered the ONE category named, not its subtree. Re-run per "
                "child category, or wire CATEGORY_CHILDREN_FUNCTION."
            ))

        if result["deleted_skipped"]:
            self.stdout.write(
                f"  {result['deleted_skipped']} soft-deleted listing(s) in "
                "scope were left alone (they render nowhere and have already "
                "told the index they are gone); a restore needs a repair run."
            )

        self._report_conflicts(result)

        reprojected = result["reprojected"]
        if reprojected:
            self.stdout.write(
                f"  re-projected: {reprojected['changed']} listing(s) changed "
                f"(built {reprojected['built']}, refreshed "
                f"{reprojected['refreshed']}), {reprojected['skipped']} skipped; "
                f"listing.updated emitted {reprojected['events_emitted']} time(s)."
            )
        elif not dry and result["listings_changed"]:
            self.stdout.write(self.style.WARNING(
                "  nothing was re-projected — the drafts moved but the stored "
                "projections did not. Run listings_reproject_features."
            ))

        if result["listings_scanned"] == 0:
            self.stdout.write(self.style.WARNING(
                "No listing in this scope carries a draft at all — nothing to "
                "rename. Check the --category id if that is a surprise."
            ))

    def _report_conflicts(self, result):
        """Per listing, per pair — never a bare count.

        A conflict is a listing whose seller answered BOTH slugs. Nothing was
        written for it, and if the run does not name it nobody will ever go
        and decide which answer was meant.
        """
        total = result["conflicts_total"]
        if not total:
            return
        self.stdout.write(self.style.WARNING(
            f"  {total} conflict(s): the draft already answers the NEW slug as "
            "well as the old one. BOTH keys were kept, neither was renamed — "
            "decide which answer is meant and edit those listings:"
        ))
        for conflict in result["conflicts"]:
            self.stdout.write(self.style.WARNING(
                f"    listing {conflict['listing_id']}: "
                f"{conflict['old']} and {conflict['new']} both answered"
            ))
        if result["conflicts_truncated"]:
            self.stdout.write(self.style.WARNING(
                "    (sample truncated; the count above is exact and every "
                "conflict is in the log)"
            ))


def _parse_renames(values) -> dict:
    """``["a=b", "c=d"]`` -> ``{"a": "b", "c": "d"}``."""
    renames = {}
    for value in values:
        if "=" not in value:
            raise CommandError(
                f"--rename {value!r} is not OLD=NEW (e.g. --rename "
                "make_ref_select=make)"
            )
        old, new = value.split("=", 1)
        renames[old.strip()] = new.strip()
    return renames
