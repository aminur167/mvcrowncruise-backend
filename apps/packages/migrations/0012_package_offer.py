"""Per-sailing offers: a label, a percentage or flat discount, and an end date.

makemigrations also swept in the model's excl_ship_package_date_overlap
constraint, which had been declared but never migrated long before this
feature. It is left out of here deliberately: it is a Postgres EXCLUDE clause,
so adding it makes every SQLite test run fail at `migrate` with a syntax error,
and it is unrelated to offers. It stays pending exactly as it was — bundling
someone else's outstanding schema change into a feature migration is how a
deploy fails for a reason nobody can find in the diff.
"""

import django.core.validators
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0011_package_rating_and_more'),
        ('ships', '0017_refund_sla_ten_working_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='package',
            name='discount_type',
            field=models.CharField(blank=True, choices=[('percent', 'Percentage off'), ('flat', 'Flat amount off (BDT)')], help_text='Blank means there is no offer on this sailing.', max_length=10),
        ),
        migrations.AddField(
            model_name='package',
            name='discount_value',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='A percentage (0-100), or a flat BDT amount — per the type above.', max_digits=10, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0.00'))]),
        ),
        migrations.AddField(
            model_name='package',
            name='offer_ends_at',
            field=models.DateTimeField(blank=True, help_text='After this moment the offer stops applying. Blank = no end date.', null=True),
        ),
        migrations.AddField(
            model_name='package',
            name='offer_label',
            field=models.CharField(blank=True, help_text='Shown on the card, e.g. "Eid Special". Blank hides the badge.', max_length=60),
        ),
    ]
