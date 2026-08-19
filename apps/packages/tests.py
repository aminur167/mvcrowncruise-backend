from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.ships.models import Ship

from .models import KidPricingRule, Package


def make_package(**kwargs):
    # Not "MV Alaska" — the seed migration (ships.0004) already creates that
    # ship in the test database and Ship.name is unique.
    ship = kwargs.pop("ship", None) or Ship.objects.create(name="Test Ship")
    defaults = {
        "ship": ship,
        "start_date": date(2026, 8, 10),
        "end_date": date(2026, 8, 12),
        "adult_price": Decimal("3000.00"),
        "status": Package.Status.OPEN,
    }
    defaults.update(kwargs)
    return Package.objects.create(**defaults)


class PackageCutoffTests(TestCase):
    def test_cutoff_auto_set_to_noon_day_before_start(self):
        package = make_package()
        cutoff = timezone.localtime(package.booking_cutoff_datetime)
        self.assertEqual(cutoff.date(), date(2026, 8, 9))
        self.assertEqual((cutoff.hour, cutoff.minute), (12, 0))

    def test_explicit_cutoff_not_overwritten(self):
        explicit = timezone.now()
        package = make_package(booking_cutoff_datetime=explicit)
        self.assertEqual(package.booking_cutoff_datetime, explicit)

    def test_auto_cutoff_resyncs_when_start_date_changes(self):
        # An auto-derived cutoff should follow the dates when they move, so a
        # package can't silently become un-bookable after a date edit.
        package = make_package()  # start 2026-08-10 → cutoff 2026-08-09 noon
        package.start_date = date(2026, 9, 20)
        package.end_date = date(2026, 9, 22)
        package.save()
        cutoff = timezone.localtime(package.booking_cutoff_datetime)
        self.assertEqual(cutoff.date(), date(2026, 9, 19))
        self.assertEqual((cutoff.hour, cutoff.minute), (12, 0))

    def test_manual_cutoff_survives_start_date_change(self):
        # A hand-picked cutoff (differs from the date default) is preserved.
        explicit = timezone.make_aware(
            timezone.datetime(2026, 8, 1, 9, 0),
            timezone.get_default_timezone(),
        )
        package = make_package(booking_cutoff_datetime=explicit)
        package.start_date = date(2026, 9, 20)
        package.end_date = date(2026, 9, 22)
        package.save()
        self.assertEqual(package.booking_cutoff_datetime, explicit)

    def test_end_date_must_be_after_start_date(self):
        package = make_package()
        package.end_date = package.start_date
        with self.assertRaises(ValidationError):
            package.full_clean()

    def test_is_bookable_respects_manual_override(self):
        package = make_package(start_date=date(2099, 1, 10), end_date=date(2099, 1, 12))
        self.assertTrue(package.is_bookable())
        package.is_booking_open = False
        self.assertFalse(package.is_bookable())

    def test_is_bookable_false_after_cutoff(self):
        package = make_package(
            booking_cutoff_datetime=timezone.now() - timezone.timedelta(hours=1)
        )
        self.assertFalse(package.is_bookable())

    def test_is_bookable_false_when_not_open(self):
        package = make_package(
            status=Package.Status.DRAFT,
            start_date=date(2099, 1, 10),
            end_date=date(2099, 1, 12),
        )
        self.assertFalse(package.is_bookable())


class KidPricingRuleTests(TestCase):
    def test_overlapping_ranges_rejected(self):
        KidPricingRule.objects.create(
            min_age=0, max_age=3, charge_type=KidPricingRule.ChargeType.FREE
        )
        overlapping = KidPricingRule(
            min_age=2,
            max_age=8,
            charge_type=KidPricingRule.ChargeType.FIXED,
            amount=Decimal("1500.00"),
        )
        with self.assertRaises(ValidationError):
            overlapping.full_clean()

    def test_adjacent_ranges_allowed(self):
        KidPricingRule.objects.create(
            min_age=0, max_age=3, charge_type=KidPricingRule.ChargeType.FREE
        )
        adjacent = KidPricingRule(
            min_age=3,
            max_age=8,
            charge_type=KidPricingRule.ChargeType.FIXED,
            amount=Decimal("1500.00"),
        )
        adjacent.full_clean()  # must not raise

    def test_fixed_rule_requires_amount(self):
        rule = KidPricingRule(
            min_age=3, max_age=8, charge_type=KidPricingRule.ChargeType.FIXED
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_min_age_must_be_below_max_age(self):
        rule = KidPricingRule(
            min_age=8, max_age=3, charge_type=KidPricingRule.ChargeType.FREE
        )
        with self.assertRaises(ValidationError):
            rule.full_clean()

    def test_rule_for_age_boundaries_min_inclusive_max_exclusive(self):
        free = KidPricingRule.objects.create(
            min_age=0, max_age=3, charge_type=KidPricingRule.ChargeType.FREE
        )
        fixed = KidPricingRule.objects.create(
            min_age=3,
            max_age=8,
            charge_type=KidPricingRule.ChargeType.FIXED,
            amount=Decimal("1500.00"),
        )
        self.assertEqual(KidPricingRule.rule_for_age(2), free)
        self.assertEqual(KidPricingRule.rule_for_age(3), fixed)
        self.assertEqual(KidPricingRule.rule_for_age(7), fixed)
        self.assertIsNone(KidPricingRule.rule_for_age(8))
