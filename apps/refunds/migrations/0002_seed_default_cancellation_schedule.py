"""Seed the default cancellation-charge schedule.

These seven tiers are exactly what the public policy page has been printing
from a hardcoded array in the React route (`CANCELLATION_TIERS`). Moving them
into data is the whole point of CancellationRule: from here the admin edits the
schedule and both the site and the money follow, instead of the page promising
one thing while the backend computes another.

ship=NULL means "the default schedule", used by every ship that has none of its
own. Seeding is skipped when default rows already exist, so re-running on a
database that has been edited never resurrects deleted tiers.
"""

from decimal import Decimal

from django.db import migrations

# (days_before_start, label, individual %, group %)
DEFAULT_TIERS = [
    (21, "3 weeks before departure", "5.00", "15.00"),
    (14, "2 weeks before departure", "15.00", "20.00"),
    (7, "1 week before departure", "35.00", "25.00"),
    (3, "3 days before departure", "50.00", "50.00"),
    (2, "48 hours before departure", "75.00", "70.00"),
    (1, "24 hours before departure", "90.00", "90.00"),
    (0, "Less than 24 hours before departure", "100.00", "100.00"),
]


def seed(apps, schema_editor):
    CancellationRule = apps.get_model("refunds", "CancellationRule")
    if CancellationRule.objects.filter(ship__isnull=True).exists():
        return
    CancellationRule.objects.bulk_create(
        [
            CancellationRule(
                ship=None,
                days_before_start=days,
                label=label,
                individual_percent=Decimal(individual),
                group_percent=Decimal(group),
                is_active=True,
            )
            for days, label, individual, group in DEFAULT_TIERS
        ]
    )


def unseed(apps, schema_editor):
    CancellationRule = apps.get_model("refunds", "CancellationRule")
    CancellationRule.objects.filter(
        ship__isnull=True,
        days_before_start__in=[days for days, _, _, _ in DEFAULT_TIERS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("refunds", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
