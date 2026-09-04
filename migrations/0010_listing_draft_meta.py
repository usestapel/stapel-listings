"""Add ``draft_meta``: an opaque, owner-only JSON sidecar on the draft twin.

Additive, nullable, default ``{}`` — no backfill needed, every existing row
already satisfies the new column's default. Not a `*_draft` twin: it carries
no listing content, is never promoted by publish, and is never cleared by it
either (see models.py for the full rationale).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0009_moderation_status_not_submitted'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='draft_meta',
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
    ]
