"""Seed the real MV Alaska 3-day/2-night food menu from the client's menu card.

Items are a selection pool per (day, meal_type) — the chef picks the day's
actual menu from these active items; this table is not a fixed daily plan.
Day 3 has no Dinner because the tour ends before then.

Idempotent: existing items are matched by (ship, day, meal_type, name) and
updated in place (order); nothing is deleted on forward migration.
"""

from django.db import migrations

Day1 = "day_1"
Day2 = "day_2"
Day3 = "day_3"

Breakfast = "breakfast"
Snacks = "snacks"
Lunch = "lunch"
Dinner = "dinner"

MENU = [
    # Day 1
    (Day1, Breakfast, [
        "Bread", "Butter", "Jelly", "Egg", "Honey", "Banana", "Tea/Coffee",
        "Plain Parata", "Mixed Vegetable", "Cholar Dal", "Chicken Curry", "Juice",
    ]),
    # The second Day-1 snack sitting is split out into its own `evening_snacks`
    # meal type by ships.0014 — kept here as-is so this migration still
    # reproduces the original seed state.
    (Day1, Snacks, ["Fruits Cake", "Fruits"]),
    (Day1, Snacks, ["Vegetable Roll", "Vegetable Pakura"]),
    (Day1, Lunch, [
        "Plain Rice", "Mixed Vegetables", "Vorta", "Vatki Fish", "Chicken",
        "Dal Vuna", "Salad", "Desert",
    ]),
    (Day1, Dinner, [
        "Mixed Fried rice", "Noodles/Vegetable Pakura", "Chinese Vegetables",
        "Spicy Chinese Chicken", "Sweet & Sour Prawn", "Cold Drinks", "Salad", "Desert",
    ]),
    # Day 2
    (Day2, Breakfast, [
        "Khichuri", "Chicken curry", "Bringal fry", "Pickle", "Egg", "Juice", "Tea/Coffee",
    ]),
    (Day2, Snacks, ["Green Coconut", "Biscuit"]),
    (Day2, Lunch, [
        "Plain Rice", "Mixed Vegetables", "Vorta", "Tengra Fish", "Chicken / Beef",
        "Dal Vuna", "Salad", "Desert",
    ]),
    (Day2, Dinner, [
        "Mixed Fried rice", "Plain parata", "Duck Rejala", "Chicken B B Q",
        "Fish B B Q", "Raita Salad", "Cold Drinks", "Desert",
    ]),
    # Day 3
    (Day3, Breakfast, [
        "Plain Parata/ Ruti", "Mixed Vegetable", "Chicken Curry", "Egg",
        "Dal Vona", "Juice", "Tea/Coffee",
    ]),
    (Day3, Snacks, ["Biscuit", "Fruits"]),
    (Day3, Lunch, [
        "Plain Polau", "Muri Ghanta", "Galda Prawn", "Mutton with chui jhal",
        "Chicken Roast", "Doi", "Green Salad", "Cold Drinks",
    ]),
]


def seed_food_menu(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    FoodMenuItem = apps.get_model("ships", "FoodMenuItem")

    ship, _ = Ship.objects.get_or_create(name="MV Alaska")

    for day, meal_type, items in MENU:
        for order, name in enumerate(items):
            FoodMenuItem.objects.update_or_create(
                ship=ship,
                day=day,
                meal_type=meal_type,
                name=name,
                defaults={"order": order},
            )


def unseed_food_menu(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    FoodMenuItem = apps.get_model("ships", "FoodMenuItem")
    ship = Ship.objects.filter(name="MV Alaska").first()
    if ship:
        all_names = [name for _, _, items in MENU for name in items]
        FoodMenuItem.objects.filter(ship=ship, name__in=all_names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ships", "0005_foodmenuitem"),
    ]

    operations = [
        migrations.RunPython(seed_food_menu, unseed_food_menu),
    ]
