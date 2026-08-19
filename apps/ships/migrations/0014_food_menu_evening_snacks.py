"""Split the single "Snacks" sitting into Snacks (before lunch) and Evening
Snacks (after lunch) for Days 1 and 2.

The client's flow for Day 1 and Day 2 is:
    Breakfast → Snacks → Lunch → Evening Snacks → Dinner

Day 3 keeps its single morning Snacks sitting (the tour ends after lunch).

Day-1 evening items already existed in the seed (ships.0006) but were merged
into the one `snacks` bucket, so they are *moved* rather than created. Day-2
evening items are new.

Idempotent: the move is a filtered update and the inserts are
update_or_create, so re-running changes nothing.
"""

from django.db import migrations, models

# Day 1 evening snacks already exist as `snacks` rows — move them.
DAY1_EVENING = ["Noodles/Vegetable Pakura", "Vegetable Roll"]

# Day 2 evening snacks are new rows.
DAY2_EVENING = ["Soup", "French Fry", "Vegetable Pakura"]

# Day-1 seeded a "Vegetable Pakura" alongside "Vegetable Roll" in snacks; the
# client's Day-1 evening list replaces it with "Noodles/Vegetable Pakura".
DAY1_RETIRED = ["Vegetable Pakura"]


def split_evening_snacks(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    FoodMenuItem = apps.get_model("ships", "FoodMenuItem")

    ship = Ship.objects.filter(name="MV Alaska").first()
    if ship is None:
        return

    # Day 1 — move the existing evening items out of the morning bucket.
    for order, name in enumerate(DAY1_EVENING):
        moved = FoodMenuItem.objects.filter(
            ship=ship, day="day_1", meal_type="snacks", name=name
        ).update(meal_type="evening_snacks", order=order)
        if not moved:
            FoodMenuItem.objects.update_or_create(
                ship=ship,
                day="day_1",
                meal_type="evening_snacks",
                name=name,
                defaults={"order": order},
            )

    # "Vegetable Pakura" on Day 1 is superseded by "Noodles/Vegetable Pakura".
    FoodMenuItem.objects.filter(
        ship=ship, day="day_1", meal_type="snacks", name__in=DAY1_RETIRED
    ).delete()

    # Day 2 — new evening sitting.
    for order, name in enumerate(DAY2_EVENING):
        FoodMenuItem.objects.update_or_create(
            ship=ship,
            day="day_2",
            meal_type="evening_snacks",
            name=name,
            defaults={"order": order},
        )


def merge_evening_snacks(apps, schema_editor):
    """Fold Evening Snacks back into the single Snacks bucket."""
    Ship = apps.get_model("ships", "Ship")
    FoodMenuItem = apps.get_model("ships", "FoodMenuItem")

    ship = Ship.objects.filter(name="MV Alaska").first()
    if ship is None:
        return

    # Day-2 evening items did not exist before this migration.
    FoodMenuItem.objects.filter(
        ship=ship, day="day_2", meal_type="evening_snacks", name__in=DAY2_EVENING
    ).delete()

    # Everything else goes back to `snacks`.
    FoodMenuItem.objects.filter(ship=ship, meal_type="evening_snacks").update(
        meal_type="snacks"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("ships", "0013_ship_guide_report_density"),
    ]

    operations = [
        migrations.AlterField(
            model_name="foodmenuitem",
            name="meal_type",
            field=models.CharField(
                choices=[
                    ("breakfast", "Breakfast"),
                    ("snacks", "Snacks"),
                    ("lunch", "Lunch"),
                    ("evening_snacks", "Evening Snacks"),
                    ("dinner", "Dinner"),
                ],
                max_length=20,
            ),
        ),
        migrations.RunPython(split_evening_snacks, merge_evening_snacks),
    ]
