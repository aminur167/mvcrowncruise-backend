from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.packages.models import KidPricingRule, Package, PackageRoom
from apps.ships.models import Room, RoomType, Ship

from .models import Booking, BookingRoom, Payment
from .pricing import calculate_total


class BookingBaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # get_or_create / distinct names: the seed migration (ships.0004)
        # already creates MV Alaska and the standard room types in the test DB.
        cls.ship = Ship.objects.create(name="Test Ship")
        cls.type_2p, _ = RoomType.objects.get_or_create(
            name="2-Person Room",
            defaults=dict(max_adults=2, max_kids=1, base_price=Decimal("2000.00")),
        )
        cls.type_4p, _ = RoomType.objects.get_or_create(
            name="4-Person Room",
            defaults=dict(max_adults=4, max_kids=2, base_price=Decimal("3500.00")),
        )
        cls.room_2p = Room.objects.create(
            ship=cls.ship, room_type=cls.type_2p, room_number="101"
        )
        cls.room_4p = Room.objects.create(
            ship=cls.ship, room_type=cls.type_4p, room_number="201"
        )
        cls.package = Package.objects.create(
            ship=cls.ship,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        PackageRoom.objects.create(package=cls.package, room=cls.room_2p)
        PackageRoom.objects.create(package=cls.package, room=cls.room_4p)
        KidPricingRule.objects.create(
            min_age=0, max_age=3, charge_type=KidPricingRule.ChargeType.FREE
        )
        KidPricingRule.objects.create(
            min_age=3,
            max_age=8,
            charge_type=KidPricingRule.ChargeType.FIXED,
            amount=Decimal("1500.00"),
        )
        KidPricingRule.objects.create(
            min_age=8, max_age=99, charge_type=KidPricingRule.ChargeType.FULL_ADULT
        )

    def make_booking(self, rooms=None, **kwargs):
        """Create a booking with one or more rooms.

        Single-room legacy call: make_booking(room=..., adult_count=...,
        kid_details=...) — the per-room fields are validated on the
        BookingRoom, exactly as before. Multi-room: pass
        rooms=[{"room":…, "adult_count":…, "kid_details":…}, …].

        full_clean() runs on each BookingRoom so pax/availability/pricing
        errors surface as they did when they lived on Booking.
        """
        room = kwargs.pop("room", self.room_2p)
        adult_count = kwargs.pop("adult_count", 2)
        kid_details = kwargs.pop("kid_details", [])
        if rooms is None:
            rooms = [
                {"room": room, "adult_count": adult_count, "kid_details": kid_details}
            ]
        booking_defaults = {
            "customer_name": "Rahim Uddin",
            "phone": "01700000000",
            "email": "rahim@example.com",
            "package": self.package,
        }
        booking_defaults.update(kwargs)
        booking = Booking(**booking_defaults)
        booking.full_clean()
        booking.save()
        for entry in rooms:
            br = BookingRoom(
                booking=booking,
                package=booking.package,
                room=entry["room"],
                adult_count=entry["adult_count"],
                kid_details=entry.get("kid_details", []),
                foreign_guests=entry.get("foreign_guests", []),
            )
            br.full_clean()
            br.save()
        booking.reprice()
        booking.save(update_fields=["total_amount", "price_snapshot", "due_amount"])
        return booking


class PricingTests(BookingBaseTestCase):
    def test_kid_age_tiers(self):
        # age 2 → free, age 5 → fixed 1500, age 10 → full adult 3000
        total = calculate_total(
            self.type_4p, self.package, adult_count=2, kid_ages=[2, 5]
        )
        self.assertEqual(total, Decimal("3500.00") + 2 * Decimal("3000.00") + Decimal("1500.00"))

        total_with_older_kid = calculate_total(
            self.type_4p, self.package, adult_count=1, kid_ages=[10]
        )
        self.assertEqual(
            total_with_older_kid,
            Decimal("3500.00") + Decimal("3000.00") + Decimal("3000.00"),
        )

    def test_all_amounts_are_decimal(self):
        total = calculate_total(self.type_2p, self.package, 2, [2, 5])
        self.assertIsInstance(total, Decimal)

    def test_unmatched_age_raises(self):
        with self.assertRaises(ValidationError):
            calculate_total(self.type_2p, self.package, 1, [200])

    def test_booking_total_computed_server_side(self):
        # Client-supplied total must be overwritten by clean().
        booking = self.make_booking(
            adult_count=2,
            kid_details=[{"age": 5}],
            total_amount=Decimal("1.00"),
            room=self.room_4p,
        )
        expected = Decimal("3500.00") + 2 * Decimal("3000.00") + Decimal("1500.00")
        self.assertEqual(booking.total_amount, expected)
        self.assertEqual(booking.due_amount, expected)


class PaxLimitTests(BookingBaseTestCase):
    def test_too_many_adults_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_booking(adult_count=3)
        self.assertIn("adult_count", ctx.exception.message_dict)

    def test_too_many_kids_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_booking(adult_count=1, kid_details=[{"age": 4}, {"age": 6}])
        self.assertIn("kid_details", ctx.exception.message_dict)

    def test_at_least_one_adult_required(self):
        with self.assertRaises(ValidationError):
            self.make_booking(adult_count=0)

    def test_malformed_kid_details_rejected(self):
        with self.assertRaises(ValidationError):
            self.make_booking(kid_details=[{"age": "five"}])

    def test_room_not_in_package_rejected(self):
        other_room = Room.objects.create(
            ship=self.ship, room_type=self.type_2p, room_number="102"
        )
        with self.assertRaises(ValidationError) as ctx:
            self.make_booking(room=other_room)
        self.assertIn("room", ctx.exception.message_dict)

    def test_limits_within_bounds_accepted(self):
        booking = self.make_booking(
            room=self.room_4p, adult_count=4, kid_details=[{"age": 2}, {"age": 5}]
        )
        self.assertEqual(booking.total_pax, 6)


class DoubleBookingTests(BookingBaseTestCase):
    def test_same_room_same_package_blocked(self):
        self.make_booking()
        # A second active hold on the same (package, room) violates the partial
        # unique constraint on BookingRoom.
        booking = Booking.objects.create(
            customer_name="Karim",
            phone="01800000000",
            email="karim@example.com",
            package=self.package,
        )
        with self.assertRaises(IntegrityError):
            BookingRoom.objects.create(
                booking=booking,
                package=self.package,
                room=self.room_2p,
                adult_count=1,
            )

    def test_cancelled_booking_frees_the_room(self):
        first = self.make_booking()
        first.status = Booking.Status.CANCELLED
        first.save()
        second = self.make_booking(customer_name="Karim")
        self.assertEqual(second.rooms.first().room, self.room_2p)


class PaymentTests(BookingBaseTestCase):
    def test_partial_payment_updates_paid_and_due(self):
        booking = self.make_booking()  # total = 2000 + 2×3000 = 8000
        Payment.objects.create(
            booking=booking,
            amount=Decimal("3000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("3000.00"))
        self.assertEqual(booking.due_amount, Decimal("5000.00"))

    def test_full_payment_leaves_zero_due(self):
        booking = self.make_booking()
        Payment.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_type=Payment.PaymentType.FULL,
            status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        booking.refresh_from_db()
        self.assertEqual(booking.due_amount, Decimal("0.00"))

    def test_failed_payment_not_counted(self):
        booking = self.make_booking()
        Payment.objects.create(
            booking=booking,
            amount=Decimal("3000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.FAILED,
        )
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("0.00"))

    def test_paid_amount_recomputed_not_incremented(self):
        booking = self.make_booking()
        payment = Payment.objects.create(
            booking=booking,
            amount=Decimal("3000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
        )
        payment.save()  # saving twice must not double-count
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("3000.00"))


class AuditTrailTests(BookingBaseTestCase):
    def test_status_change_logged(self):
        booking = self.make_booking()
        self.assertEqual(booking.status_logs.count(), 1)  # creation log

        booking.status = Booking.Status.FULLY_PAID
        booking.save()
        log = booking.status_logs.first()
        self.assertEqual(log.old_status, Booking.Status.PENDING)
        self.assertEqual(log.new_status, Booking.Status.FULLY_PAID)

    def test_no_log_when_status_unchanged(self):
        booking = self.make_booking()
        booking.customer_name = "Renamed"
        booking.save()
        self.assertEqual(booking.status_logs.count(), 1)


class BookingCodeTests(BookingBaseTestCase):
    def test_code_auto_generated_and_unique(self):
        first = self.make_booking()
        second = self.make_booking(customer_name="Karim", room=self.room_4p)
        self.assertTrue(first.booking_code.startswith("BK-"))
        self.assertNotEqual(first.booking_code, second.booking_code)
