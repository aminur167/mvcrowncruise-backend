import django.contrib.postgres.constraints
from django.db import migrations, models


def try_add_exclusion(apps, schema_editor):
    try:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist;")
        Package = apps.get_model("packages", "Package")
        constraint = django.contrib.postgres.constraints.ExclusionConstraint(
            condition=models.Q(
                ("status__in", ("draft", "cancelled")), _negated=True
            ),
            expressions=[
                ("ship", "="),
                (
                    models.Func(
                        models.F("start_date"),
                        models.F("end_date"),
                        function="daterange",
                    ),
                    "&&",
                ),
            ],
            name="excl_ship_package_date_overlap",
        )
        schema_editor.add_constraint(Package, constraint)
    except Exception:
        pass


def try_remove_exclusion(apps, schema_editor):
    try:
        Package = apps.get_model("packages", "Package")
        constraint = django.contrib.postgres.constraints.ExclusionConstraint(
            condition=models.Q(
                ("status__in", ("draft", "cancelled")), _negated=True
            ),
            expressions=[
                ("ship", "="),
                (
                    models.Func(
                        models.F("start_date"),
                        models.F("end_date"),
                        function="daterange",
                    ),
                    "&&",
                ),
            ],
            name="excl_ship_package_date_overlap",
        )
        schema_editor.remove_constraint(Package, constraint)
    except Exception:
        pass


class Migration(migrations.Migration):

    dependencies = [
        ("packages", "0002_package_hero_image_package_highlights_and_more"),
        ("ships", "0006_seed_mv_alaska_food_menu"),
    ]

    operations = [
        migrations.RunPython(try_add_exclusion, try_remove_exclusion),
    ]
