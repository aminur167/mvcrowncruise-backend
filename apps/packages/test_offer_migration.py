"""Exercise the 0013 data migration against the shapes that are actually in the
live table today: discount_type "" (three rows) and "percent" (one), plus the
"flat" and NULL cases the column still allows.

Run with Django's own migrator so this is the real migration, not a copy of it.
"""

from decimal import Decimal

from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from django.test import TransactionTestCase


class OfferMigrationTests(TransactionTestCase):
    # TransactionTestCase so migrate() can run against a real (test) database.

    def migrate_to(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(target)
        executor.loader.build_graph()
        return executor

    def test_the_data_migration_renames_every_legacy_spelling(self):
        from django.apps import apps as global_apps

        # Back to just before the rename, and read the table as it was then.
        executor = self.migrate_to([("packages", "0012_package_offer")])
        old_apps = executor.loader.project_state(
            [("packages", "0012_package_offer")]
        ).apps
        Ship = old_apps.get_model("ships", "Ship")
        Package = old_apps.get_model("packages", "Package")

        ship = Ship.objects.create(name="Migration Ship")
        from datetime import date

        def make(**kw):
            return Package.objects.create(
                ship=ship,
                start_date=date(2099, 3, 1),
                end_date=date(2099, 3, 4),
                adult_price=Decimal("3000.00"),
                **kw,
            )

        blank = make(discount_type="", discount_value=None)
        flat = make(discount_type="flat", discount_value=Decimal("1500.00"))
        pct = make(discount_type="percent", discount_value=Decimal("25.00"))

        # Forwards.
        self.migrate_to([("packages", "0013_offer_type_none_and_fixed")])
        Package = global_apps.get_model("packages", "Package")

        self.assertEqual(Package.objects.get(pk=blank.pk).discount_type, "none")
        self.assertEqual(Package.objects.get(pk=blank.pk).discount_value, Decimal("0.00"))
        self.assertEqual(Package.objects.get(pk=flat.pk).discount_type, "fixed")
        self.assertEqual(Package.objects.get(pk=flat.pk).discount_value, Decimal("1500.00"))
        # The one shape that is actually live: a percentage, spelled the same
        # before and after, with its value untouched.
        self.assertEqual(Package.objects.get(pk=pct.pk).discount_type, "percent")
        self.assertEqual(Package.objects.get(pk=pct.pk).discount_value, Decimal("25.00"))

        # And the offer still reads as live after the rename.
        self.assertTrue(Package.objects.get(pk=pct.pk).offer_is_live())
        self.assertFalse(Package.objects.get(pk=blank.pk).offer_is_live())
