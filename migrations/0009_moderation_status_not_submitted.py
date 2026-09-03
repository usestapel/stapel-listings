"""``moderation_status`` gains "not_submitted", and the drafts that meant it say it.

The choice on its own would only fix rows created after the deploy. What made
this a defect was the rows already in the table: ``pending`` was the field
DEFAULT, so every draft ever created claimed to be awaiting a moderation
decision from the instant it existed. One live stand held 167 of them and not
one had a moderation case behind it — the cabinet was reading the column
correctly and the column was saying the wrong thing.

The backfill is deliberately narrow, and each of the three predicates is
load-bearing:

* ``status='draft'`` — a listing in any other status has been through publish
  at least once, and ``pending`` there is a real claim about a real queue;
* ``published_at IS NULL`` — an ARCHIVED-then-restored draft HAS been
  submitted before, and its history is not this migration's to erase;
* ``moderation_status='pending'`` — only the default-shaped value is touched;
  an approved or rejected verdict is a decision somebody made.

Reversible, and the reverse is exact: ``not_submitted`` is a value no code
before this migration could write, so every row carrying it on the way back
down is a row this migration wrote on the way up.
"""
from django.db import migrations, models


def to_not_submitted(apps, schema_editor):
    listing = apps.get_model("listings", "Listing")
    moved = listing.objects.filter(
        status="draft", published_at__isnull=True, moderation_status="pending"
    ).update(moderation_status="not_submitted")
    print(f"  listings: {moved} never-submitted draft(s) no longer claim review")


def back_to_pending(apps, schema_editor):
    listing = apps.get_model("listings", "Listing")
    listing.objects.filter(moderation_status="not_submitted").update(
        moderation_status="pending"
    )


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0008_alter_listing_category_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listing',
            name='moderation_status',
            field=models.CharField(choices=[('not_submitted', 'Not submitted for review'), ('pending', 'Pending Review'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('needs_review', 'Needs Manual Review')], default='not_submitted', max_length=20),
        ),
        migrations.RunPython(to_not_submitted, back_to_pending),
    ]
