# Brings the offer fields into line with the sister project's vocabulary, so a
# patch written against one codebase still means the same thing in the other.
#
#   discount_type ""     -> "none"     (an absent offer is a value, not a blank)
#   discount_type "flat" -> "fixed"
#   discount_value NULL  -> 0.00       (so the column can drop NULL entirely)
#
# The data is rewritten BEFORE the columns tighten, because the new
# discount_value is NOT NULL and the new discount_type has no blank state to
# fall back on. Reversible in both directions: nothing here is lossy.
#
# On this database the rewrite touches only rows that were never given an
# offer — the one live offer is a percentage, which is spelled the same way
# before and after.

import django.core.validators
from decimal import Decimal
from django.db import migrations, models


def to_named_none(apps, schema_editor):
    Package = apps.get_model("packages", "Package")
    Package.objects.filter(discount_type="").update(discount_type="none")
    Package.objects.filter(discount_type="flat").update(discount_type="fixed")
    Package.objects.filter(discount_value__isnull=True).update(
        discount_value=Decimal("0.00")
    )


def back_to_blank(apps, schema_editor):
    Package = apps.get_model("packages", "Package")
    Package.objects.filter(discount_type="fixed").update(discount_type="flat")
    Package.objects.filter(discount_type="none").update(discount_type="")


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0012_package_offer"),
    ]

    operations = [
        migrations.RunPython(to_named_none, back_to_blank),
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
