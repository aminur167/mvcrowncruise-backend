# The column changes that follow 0013's data rewrite.
#
# Separate from it because Postgres will not ALTER a table in the same
# transaction as the UPDATEs that just ran against it:
#
#   cannot ALTER TABLE "packages_package" because it has pending trigger events
#
# Django wraps each migration in its own transaction, so splitting them is the
# whole fix — 0013 commits its UPDATEs, and this one alters a settled table.
#
# discount_value can only become NOT NULL because 0013 has already filled every
# NULL with 0.00. These two must stay in this order.

import django.core.validators
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0013_offer_type_none_and_fixed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="package",
            name="discount_type",
            field=models.CharField(
                choices=[
                    ("none", "No offer"),
                    ("percent", "Percentage off"),
                    ("fixed", "Fixed amount off, per cabin"),
                ],
                default="none",
                help_text="What kind of reduction, if any, this sailing is sold at.",
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="package",
            name="discount_value",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text=(
                    "Percent off, or taka off PER CABIN — a 3-cabin booking gets a "
                    "fixed discount three times, once against each cabin, because "
                    "that is how the cabins are priced."
                ),
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00"))],
            ),
        ),
    ]
