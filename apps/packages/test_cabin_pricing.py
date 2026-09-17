"""Cabins are sold whole, with an allowance for berths nobody travels in.

The operator loses the whole cabin from inventory whether one person takes it
or four, so the fare is its adult capacity. What a missing guest genuinely
saves is their food for the trip, and that much comes back — once per empty
berth, for the package, not per night.

These tests pin the arithmetic and, more importantly, the boundaries: that the
allowance cannot make a cabin cost less than nothing, that it never touches a
booking already priced, and that a ship with no allowance set behaves exactly
as the system did before any of this existed.
"""

from decimal import Decimal

from rest_framework.test import APITestCase

from apps.bookings.models import Payment
from apps.bookings.pricing import price_breakdown, restore_breakdown
from apps.bookings.test_api import build_fixtures
from apps.packages.models import Package
from apps.testing import ThrottlelessTestMixin, create_booking


class CabinPricingTests(ThrottlelessTestMixin, APITestCase):
    """The 2-berth fixture: 2000 base, 3000 per adult. A full cabin is 8000."""

    def setUp(self):
        self.ship, self.type_2p, self.type_4p, self.room, _, self.package = (
            build_fixtures(ship_name="Cabin Ship")
        )
        self.ship.meal_allowance = Decimal("500.00")
        self.ship.save()

    def price(self, adults, kids=(), room_type=None):
        return price_breakdown(
            room_type or self.type_2p, self.package, adults, list(kids)
        )

    def test_a_full_cabin_is_priced_by_its_berths(self):
        bd = self.price(2)
        self.assertEqual(bd["charged_adults"], 2)
        self.assertEqual(bd["empty_berth_count"], 0)
        self.assertEqual(bd["empty_berth_discount"], Decimal("0.00"))
        self.assertEqual(bd["total"], Decimal("8000.00"))

    def test_one_traveller_still_pays_for_the_cabin_less_their_food(self):
        """The heart of it: 8000 for the cabin, 500 back for the meals the
        empty berth will not eat — not 5000 for "one person"."""
        bd = self.price(1)
        self.assertEqual(bd["charged_adults"], 2)
        self.assertEqual(bd["empty_berth_count"], 1)
        self.assertEqual(bd["empty_berth_discount"], Decimal("500.00"))
        self.assertEqual(bd["total"], Decimal("7500.00"))

    def test_a_bigger_cabin_allows_for_every_empty_berth(self):
        """4-berth fixture: 3500 base + 4 × 3000 = 15500 full."""
        bd = self.price(1, room_type=self.type_4p)
        self.assertEqual(bd["empty_berth_count"], 3)
        self.assertEqual(bd["empty_berth_discount"], Decimal("1500.00"))
        self.assertEqual(bd["total"], Decimal("14000.00"))

    def test_children_are_added_on_top_and_do_not_fill_a_berth(self):
        """A child is priced by the kid tiers, not by taking an adult's place:
        the berth is still empty of an adult and still allowed for. The 3-8
        fixture tier is a flat 1500."""
        bd = self.price(1, kids=[5])
        self.assertEqual(bd["empty_berth_count"], 1)
        self.assertEqual(bd["kids_subtotal"], Decimal("1500.00"))
        self.assertEqual(bd["total"], Decimal("9000.00"))

    def test_no_allowance_means_the_cabin_price_stands(self):
        self.ship.meal_allowance = Decimal("0.00")
        self.ship.save()
        self.assertEqual(self.price(1)["total"], Decimal("8000.00"))

    def test_an_allowance_larger_than_the_cabin_gives_a_free_cabin(self):
        """Never a negative one: the booking total feeds a non-negative
        constraint, and money owed to a customer is a refund, not a booking."""
        self.ship.meal_allowance = Decimal("99999.00")
        self.ship.save()
        self.assertEqual(self.price(1)["total"], Decimal("0.00"))

    def test_an_offer_comes_off_what_is_left_after_the_allowance(self):
        """Not off the headline cabin price — the customer is not being given
        20% of money they were never charged."""
        self.package.discount_type = Package.OfferType.PERCENT
        self.package.discount_value = Decimal("20")
        self.package.save()
        bd = self.price(1)
        self.assertEqual(bd["subtotal"], Decimal("7500.00"))
        self.assertEqual(bd["discount"], Decimal("1500.00"))
        self.assertEqual(bd["total"], Decimal("6000.00"))


class CabinPricingSnapshotTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.ship, self.type_2p, _, self.room, _, self.package = build_fixtures(
            ship_name="Cabin Snapshot Ship"
        )
        self.ship.meal_allowance = Decimal("500.00")
        self.ship.save()

    def test_the_booking_is_charged_the_cabin_price(self):
        booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )
        self.assertEqual(booking.total_amount, Decimal("7500.00"))

    def test_the_allowance_is_frozen_with_the_booking(self):
        booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )
        snap = restore_breakdown(booking.rooms.first().price_snapshot)
        self.assertEqual(snap["charged_adults"], 2)
        self.assertEqual(snap["empty_berth_count"], 1)
        self.assertEqual(snap["meal_allowance"], Decimal("500.00"))
        self.assertEqual(snap["empty_berth_discount"], Decimal("500.00"))

    def test_changing_the_allowance_does_not_reprice_a_live_booking(self):
        booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )
        Payment.objects.create(
            booking=booking,
            amount=Decimal("4000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.PENDING,
            transaction_id="CABIN-FROZEN-1",
        )
        self.ship.meal_allowance = Decimal("9000.00")
        self.ship.save()

        booking.reprice()
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("7500.00"))

    def test_a_snapshot_from_before_cabins_were_sold_whole_still_restores(self):
        """Those bookings charged exactly the heads that travelled, so no berth
        was empty and none was allowed for. A KeyError here would 500 the
        invoice of every booking made before this change."""
        legacy = {
            "room_base": "2000.00",
            "adult_price": "3000.00",
            "adult_count": 1,
            "adults_subtotal": "3000.00",
            "kids": [],
            "kids_subtotal": "0.00",
            "total": "5000.00",
        }
        restored = restore_breakdown(legacy)
        self.assertEqual(restored["charged_adults"], 1)
        self.assertEqual(restored["empty_berth_count"], 0)
        self.assertEqual(restored["empty_berth_discount"], Decimal("0.00"))
        self.assertEqual(restored["total"], Decimal("5000.00"))


class PerHeadPricingStaysTheDefaultTests(ThrottlelessTestMixin, APITestCase):
    """A blank allowance is not a zero one.

    Blank means the ship is still sold per head — the model every existing
    booking was made under. A price change must be something staff choose, not
    something that arrives with a deployment.
    """

    def setUp(self):
        self.ship, self.type_2p, _, _, _, self.package = build_fixtures(
            ship_name="Per Head Ship"
        )

    def test_a_ship_starts_selling_per_head(self):
        self.assertIsNone(self.ship.meal_allowance)
        self.assertFalse(self.ship.sells_whole_cabins)

    def test_one_traveller_pays_for_one(self):
        bd = price_breakdown(self.type_2p, self.package, 1, [])
        self.assertEqual(bd["charged_adults"], 1)
        self.assertEqual(bd["empty_berth_count"], 0)
        self.assertEqual(bd["total"], Decimal("5000.00"))

    def test_setting_it_to_zero_switches_the_model_on(self):
        """Zero is a real answer — sell whole cabins, allow nothing back — and
        is why the field is nullable rather than defaulting to 0."""
        self.ship.meal_allowance = Decimal("0.00")
        self.ship.save()
        self.assertTrue(self.ship.sells_whole_cabins)
        bd = price_breakdown(self.type_2p, self.package, 1, [])
        self.assertEqual(bd["charged_adults"], 2)
        self.assertEqual(bd["total"], Decimal("8000.00"))
