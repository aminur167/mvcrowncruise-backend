"""Quote one refund window of 10 working days, end to end.

SSLCommerz require a published standard refund timeline of 7-10 working days.
We were promising 14 working days to *start* the payout, with the customer's
own bank leg on top of that — two numbers, and the wrong ones.

Altering the field default alone is not enough: a default applies to rows
created afterwards, so every ship already in the database would keep quoting 14
on the live site — which is exactly the number the compliance review reads. So
this migration also rewrites the existing rows.

Only rows still sitting on the old default are touched. A ship whose figure was
deliberately set to something else is left alone: an admin who chose a number
should not have it silently overwritten by a deploy.
"""

from django.db import migrations, models

OLD_DEFAULT = 14
NEW_DEFAULT = 10


def adopt_new_sla(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    Ship.objects.filter(refund_sla_days=OLD_DEFAULT).update(refund_sla_days=NEW_DEFAULT)


def restore_old_sla(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    Ship.objects.filter(refund_sla_days=NEW_DEFAULT).update(refund_sla_days=OLD_DEFAULT)


class Migration(migrations.Migration):
    dependencies = [("ships", "0016_fix_mv_crown_room_layout")]

    operations = [
        migrations.AlterField(
            model_name="ship",
            name="refund_sla_days",
            field=models.PositiveSmallIntegerField(
                default=10,
                help_text=(
                    "Working days quoted to the customer for a refund payout, "
                    "END TO END — the customer's own bank or wallet leg "
                    "included, not just our own processing. Drives the promise "
                    "on the refund policy page, in the cancellation email, and "
                    "the overdue-refund alert. SSLCommerz require a published "
                    "timeline of 7-10 working days, so a figure above 10 will "
                    "fail their compliance review."
                ),
            ),
        ),
        migrations.RunPython(adopt_new_sla, restore_old_sla),
    ]
