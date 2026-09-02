# Д51: NULL is the honest spelling of "price not stated" — the old
# ``default=0`` published every skipped price as a public «0 ₽».
#
# AlterField only, deliberately no data rewrite: an existing 0 in the column
# cannot be told apart from a genuinely free item by a migration, so the
# decision stays with the host (a classified with no free-items section
# sweeps price=0 to NULL; another deployment must not).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0005_alter_listing_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listing',
            name='price',
            field=models.DecimalField(
                blank=True, decimal_places=2, default=None,
                max_digits=12, null=True,
            ),
        ),
    ]
