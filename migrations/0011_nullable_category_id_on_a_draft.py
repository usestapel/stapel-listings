"""Drop the NOT NULL on ``listing.category_id`` (0.21.4).

Additive / expand-only: widening a column to accept NULL adds no constraint
and rewrites no row, so an older writer keeps working against it unchanged.
Every existing row keeps the category it has; only rows a composer opens
before the category step are NULL, and they are drafts.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0010_listing_draft_meta'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listing',
            name='category_id',
            field=models.CharField(blank=True, db_index=True, default=None, max_length=64, null=True, validators=[django.core.validators.RegexValidator(code='invalid_category_id', message='A category id is an opaque id, not a path. Got a value containing a separator — a slash-joined category PATH belongs in the search ?category= filter, not in category_id.', regex='\\A[A-Za-z0-9][A-Za-z0-9_-]*\\Z')]),
        ),
    ]
