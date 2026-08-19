"""QA Phase 2 — double-booking / race-condition adversarial suite.

Focus: the booking-creation race (same room, same package), the last-room
edge case, the PENDING-booking hold mechanism, hold expiry, and the
concurrency of the payment-settlement paths that interact with holds.

Tests that FAIL here are reproductions of open bugs, asserting the *desired*
behavior (same convention as test_qa_phase1*.py).

Run: manage.py test apps.test_qa_phase2
"""

import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.db import connections
from django.db.models import Count
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.bookings.management.commands.expire_stale_bookings import expire_booking
from apps.bookings.models import Booking, BookingRoom, Payment
from apps.bookings.serializers import PaymentInitiateSerializer
from apps.bookings import payment_service
from apps.bookings.test_api import build_fixtures
from apps.packages.models import PackageRoom
from apps.ships.models import Room
from apps.testing import ThrottlelessTestMixin, create_booking, sign_ipn

GATEWAY_URL = "https://sandbox.sslcommerz.com/gwprocess/qa-phase2"


def booking_payload(package, room, name="Racer", email="race@example.com"):
    return {
        "package_id": package.id,
        "customer_name": name,
        "phone": "01700000000",
        "email": email,
        "rooms": [{"room_id": room.id, "adult_count": 1, "kid_details": []}],
    }


class ConcurrencyTestCase(ThrottlelessTestMixin, TransactionTestCase):
    """Base for thread-based race tests (real transactions, real constraint)."""

    def setUp(self):
        (
            self.ship,
            self.type_2p,
            self.type_4p,
            self.room_2p,
            self.room_4p,
            self.package,
        ) = build_fixtures(ship_name="QA Phase2 Race Ship")

    def race_bookings(self, attempts):
        """Fire all `attempts` (list of payload dicts) simultaneously.
        Returns list of (status_code, response_data)."""
        barrier = threading.Barrier(len(attempts))
        results = []
        lock = threading.Lock()

        def attempt(payload):
            try:
                barrier.wait(timeout=15)
                client = APIClient()
                response = client.post("/api/bookings/", payload, format="json")
                with lock:
                    results.append((response.status_code, response.data))
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=attempt, args=(p,)) for p in attempts
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        return results

    def active_bookings(self, room):
        # A room's active holds now live on BookingRoom (is_active mirrors "still
        # held"), one per active booking.
        return BookingRoom.objects.filter(
            package=self.package, room=room, is_active=True
        )


class EightWaySameRoomRaceTests(ConcurrencyTestCase):
    """Prompt scenario 2-4: 8 simultaneous requests for the SAME room."""

    def test_eight_simultaneous_bookings_exactly_one_winner(self):
        attempts = [
            booking_payload(self.package, self.room_4p, name=f"Customer {i}")
            for i in range(8)
        ]
        results = self.race_bookings(attempts)
        codes = sorted(code for code, _ in results)

        self.assertEqual(len(results), 8, "every thread must get a response")
        self.assertEqual(codes, [201] + [409] * 7, f"got {codes}")
        # losers get the machine-readable race code, not a raw error
        for code, data in results:
            if code == 409:
                self.assertEqual(data.get("code"), "room_unavailable")
        # DB truth: exactly one non-cancelled booking for the room
        self.assertEqual(self.active_bookings(self.room_4p).count(), 1)
        # and zero duplicate active rows anywhere in the table
        dupes = (
            BookingRoom.objects.filter(is_active=True)
            .values("package_id", "room_id")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )
        self.assertEqual(list(dupes), [])


class LastRoomOfTypeRaceTests(ConcurrencyTestCase):
    """Prompt scenario 5: the last remaining room of a type under contention."""

    def setUp(self):
        super().setUp()
        # three 2-person rooms; two already booked → R3 is the last of its type
        self.extra_rooms = []
        for number in ("R2", "R3"):
            room = Room.objects.create(
                ship=self.ship, room_type=self.type_2p, room_number=number
            )
            PackageRoom.objects.create(package=self.package, room=room)
            self.extra_rooms.append(room)
        for room in (self.room_2p, self.extra_rooms[0]):
            create_booking(
                self.package,
                rooms=[{"room": room, "adult_count": 1, "kid_details": []}],
                customer_name="Early Bird",
                phone="01700000001",
                email="early@example.com",
            )

    def test_six_racers_for_the_last_room_one_winner(self):
        last_room = self.extra_rooms[1]
        results = self.race_bookings(
            [
                booking_payload(self.package, last_room, name=f"Late {i}")
                for i in range(6)
            ]
        )
        codes = sorted(code for code, _ in results)
        self.assertEqual(codes, [201] + [409] * 5, f"got {codes}")
        self.assertEqual(self.active_bookings(last_room).count(), 1)

    def test_parallel_bookings_of_different_rooms_all_succeed(self):
        """No over-locking: contention on one room must not fail others."""
        last_2p = self.extra_rooms[1]
        results = self.race_bookings(
            [
                booking_payload(self.package, last_2p, name="A"),
                booking_payload(self.package, self.room_4p, name="B"),
            ]
        )
        codes = sorted(code for code, _ in results)
        self.assertEqual(codes, [201, 201], f"got {codes}")


class HoldMechanismTests(ThrottlelessTestMixin, APITestCase):
    """Prompt scenarios 6-7: the PENDING booking *is* the hold."""

    @classmethod
    def setUpTestData(cls):
        (_, _, _, cls.room_2p, cls.room_4p, cls.package) = build_fixtures(
            ship_name="QA Phase2 Hold Ship"
        )

    def make_pending_booking(self, room=None):
        return create_booking(
            self.package,
            rooms=[{"room": room or self.room_4p, "adult_count": 1, "kid_details": []}],
            customer_name="Holder",
            phone="01700000002",
            email="holder@example.com",
        )

    def backdate(self, booking, minutes):
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )

    def test_unpaid_pending_booking_blocks_the_room(self):
        self.make_pending_booking()
        response = self.client.post(
            "/api/bookings/",
            booking_payload(self.package, self.room_4p),
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data.get("code"), "room_unavailable")

    def test_expired_hold_releases_the_room(self):
        booking = self.make_pending_booking()
        self.backdate(booking, 45)  # past the 30-min hold window
        call_command("expire_stale_bookings", verbosity=0)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        response = self.client.post(
            "/api/bookings/",
            booking_payload(self.package, self.room_4p, name="Next Customer"),
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_fresh_hold_is_not_expired(self):
        booking = self.make_pending_booking()
        call_command("expire_stale_bookings", verbosity=0)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_paid_booking_is_never_expired(self):
        booking = self.make_pending_booking()
        payment = Payment.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_type=Payment.PaymentType.FULL,
            transaction_id=f"{booking.booking_code}-PX1",
        )
        payment.status = Payment.Status.SUCCESS
        payment.save()
        self.backdate(booking, 120)
        call_command("expire_stale_bookings", verbosity=0)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)


class ExpiryVsSettlementClobberTests(ThrottlelessTestMixin, TransactionTestCase):
    """BUG 11 REPRO (now asserting the fix) — the expiry cron's scan races
    the IPN.

    Interleaving (driving the real production code at each step):
      1. the command's scan runs → this booking is in the stale id list
      2. IPN settles a payment → booking becomes PARTIALLY_PAID, paid=4500
      3. the command's per-booking expiry runs on the scanned id
    The fixed expire_booking() re-checks everything under a row lock and
    writes status only, so the settled money and status must survive.
    (Before the fix, the loop's stale full-field save cancelled the paid
    booking and clobbered paid_amount back to 0.00.)
    """

    def setUp(self):
        (_, _, _, _, self.room_4p, self.package) = build_fixtures(
            ship_name="QA Phase2 Clobber Ship"
        )

    def test_settlement_between_fetch_and_save_is_not_clobbered(self):
        booking = create_booking(
            self.package,
            rooms=[{"room": self.room_4p, "adult_count": 1, "kid_details": []}],
            customer_name="Slow Payer",
            phone="01700000003",
            email="slow@example.com",
        )
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(minutes=45)
        )

        # 1. exactly the command's scan, fetched (= loop starts iterating).
        # The live-session exclude is a no-op here: the payment does not
        # exist yet when the cron evaluates its queryset.
        now = timezone.now()
        cutoff = now - timedelta(minutes=30)
        stale_ids = list(
            Booking.objects.filter(
                status=Booking.Status.PENDING, created_at__lt=cutoff
            )
            .exclude(payments__status=Payment.Status.SUCCESS)
            .exclude(payments__status=Payment.Status.PENDING)
            .distinct()
            .values_list("pk", flat=True)
        )
        self.assertEqual(stale_ids, [booking.pk])

        # 2. IPN settles a partial payment mid-loop
        payment = Payment.objects.create(
            booking=booking,
            amount=Decimal("4500.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            transaction_id=f"{booking.booking_code}-PX2",
        )
        payment.status = Payment.Status.SUCCESS
        payment.save()  # → refresh_paid_amount: PARTIALLY_PAID, paid 4500

        # 3. the command's per-booking expiry, on the pre-settlement scan
        for stale_pk in stale_ids:
            expire_booking(stale_pk, cutoff)

        # DESIRED: the settled money and status survive the race
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("4500.00"))
        self.assertNotEqual(booking.status, Booking.Status.CANCELLED)

    def test_truly_stale_booking_still_expires_via_helper(self):
        """Control: the locked re-check does not stop legitimate expiry."""
        booking = create_booking(
            self.package,
            rooms=[{"room": self.room_4p, "adult_count": 1, "kid_details": []}],
            customer_name="Abandoner",
            phone="01700000007",
            email="gone@example.com",
        )
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(minutes=45)
        )
        result = expire_booking(booking.pk, timezone.now() - timedelta(minutes=30))
        self.assertIsNotNone(result)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)


class PayInitiateVsExpiryRaceTests(ThrottlelessTestMixin, APITestCase):
    """BUG 14 REPRO (now asserting the fix) — `pay` was check-then-act with
    no lock: the expiry cron could cancel the booking between the
    serializer's status check and initiate_payment(). The fixed service
    re-checks the booking under a row lock, so no live gateway session can
    exist for a CANCELLED booking."""

    @classmethod
    def setUpTestData(cls):
        (_, _, _, _, cls.room_4p, cls.package) = build_fixtures(
            ship_name="QA Phase2 PayRace Ship"
        )

    def test_initiate_payment_refuses_a_just_cancelled_booking(self):
        booking = create_booking(
            self.package,
            rooms=[{"room": self.room_4p, "adult_count": 1, "kid_details": []}],
            customer_name="Racer",
            phone="01700000004",
            email="payrace@example.com",
        )

        # 1. the view's validation passes while the booking is still PENDING
        serializer = PaymentInitiateSerializer(
            data={"payment_type": "full"}, context={"booking": booking}
        )
        self.assertTrue(serializer.is_valid())

        # 2. the expiry cron cancels the booking right now
        Booking.objects.filter(pk=booking.pk).update(
            status=Booking.Status.CANCELLED
        )

        # 3. the view proceeds to create the gateway session
        with patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            try:
                payment_service.initiate_payment(
                    booking, **serializer.validated_data
                )
            except Exception:
                pass  # refusing loudly is fine — creating the session is not

        # DESIRED: no live payment session exists for a cancelled booking
        self.assertFalse(
            Payment.objects.filter(
                booking=booking, status=Payment.Status.PENDING
            ).exists(),
            "a PENDING gateway session was created for a CANCELLED booking",
        )


class DoubleSettlementTests(ThrottlelessTestMixin, APITestCase):
    """BUG 13 REPRO (now asserting the fix) — a two-tab customer can never
    end up with two payable checkout pages.

    Originally a second initiate SUPERSEDED the first (cancelling its row but
    leaving its gateway session live and payable — QA H4). Now an identical
    re-request REUSES the one live session, so only one page can ever take
    money and paid_amount cannot double."""

    @classmethod
    def setUpTestData(cls):
        (_, _, _, _, cls.room_4p, cls.package) = build_fixtures(
            ship_name="QA Phase2 DoublePay Ship"
        )

    def settle_via_ipn(self, payment):
        verdict = {
            "status": "VALID",
            "tran_id": payment.transaction_id,
            "val_id": f"VAL-{payment.pk}",
            "amount": str(payment.amount),
            "currency": "BDT",
        }
        with patch(
            "apps.bookings.sslcommerz.validate_payment", return_value=verdict
        ):
            return self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {
                        "tran_id": payment.transaction_id,
                        "val_id": f"VAL-{payment.pk}",
                        "status": "VALID",
                    }
                ),
            )

    def test_second_full_session_cannot_over_credit(self):
        booking = create_booking(
            self.package,
            rooms=[{"room": self.room_4p, "adult_count": 1, "kid_details": []}],
            customer_name="Two Tabs",
            phone="01700000005",
            email="twotabs@example.com",
        )
        total = booking.total_amount

        # customer opens the payment page twice — identical full-due request
        with patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            r1 = self.client.post(
                f"/api/bookings/{booking.booking_code}/pay/",
                {"payment_type": "full"},
                format="json",
            )
            r2 = self.client.post(
                f"/api/bookings/{booking.booking_code}/pay/",
                {"payment_type": "full"},
                format="json",
            )
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)

        # Both tabs point at the SAME live session — there is no second
        # payable checkout page to strand the customer's money on.
        self.assertEqual(r2.data["tran_id"], r1.data["tran_id"])
        self.assertEqual(booking.payments.count(), 1)
        payment = Payment.objects.get(transaction_id=r1.data["tran_id"])

        # Both tabs complete at the gateway: the duplicate IPN is idempotent.
        self.settle_via_ipn(payment)
        self.settle_via_ipn(payment)

        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        # Credited exactly once — never doubled, never negative.
        self.assertEqual(booking.paid_amount, total)
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)
        # Nobody's money was stranded, so no refund is owed.
        self.assertFalse(booking.refund_required)


class ConcurrentSettlementLostUpdateTests(ThrottlelessTestMixin, TransactionTestCase):
    """BUG 12 PROBE (now asserting the fix) — refresh_paid_amount() used to
    SUM(payments) without locking the Booking row: two payments settling in
    overlapping transactions each SUMmed before the other committed (READ
    COMMITTED) and the last writer recorded only its own amount. The fixed
    refresh locks the booking row so concurrent settlements serialize.
    Thread-timing dependent — before the fix it failed ~60% of runs."""

    def setUp(self):
        (_, _, _, _, self.room_4p, self.package) = build_fixtures(
            ship_name="QA Phase2 LostUpdate Ship"
        )

    def test_two_simultaneous_partial_settlements_both_counted(self):
        booking = create_booking(
            self.package,
            rooms=[{"room": self.room_4p, "adult_count": 1, "kid_details": []}],
            customer_name="Split Payer",
            phone="01700000006",
            email="split@example.com",
        )

        amounts = (Decimal("2000.00"), Decimal("3000.00"))
        payments = []
        for i, amount in enumerate(amounts):
            payments.append(
                Payment.objects.create(
                    booking=booking,
                    amount=amount,
                    payment_type=Payment.PaymentType.PARTIAL,
                    transaction_id=f"{booking.booking_code}-PLU{i}",
                )
            )

        verdicts = {
            f"VAL-LU{p.pk}": {
                "status": "VALID",
                "tran_id": p.transaction_id,
                "val_id": f"VAL-LU{p.pk}",
                "amount": str(p.amount),
                "currency": "BDT",
            }
            for p in payments
        }

        barrier = threading.Barrier(2)

        def settle(payment):
            try:
                barrier.wait(timeout=15)
                client = APIClient()
                client.post(
                    "/api/payments/ipn/",
                    sign_ipn(
                        {
                            "tran_id": payment.transaction_id,
                            "val_id": f"VAL-LU{payment.pk}",
                            "status": "VALID",
                        }
                    ),
                )
            finally:
                connections.close_all()

        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            side_effect=lambda val_id: verdicts[val_id],
        ):
            threads = [
                threading.Thread(target=settle, args=(p,)) for p in payments
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

        booking.refresh_from_db()
        settled = Payment.objects.filter(
            booking=booking, status=Payment.Status.SUCCESS
        ).count()
        self.assertEqual(settled, 2, "both IPNs must settle")
        self.assertEqual(
            booking.paid_amount,
            Decimal("5000.00"),
            "a settled payment vanished from paid_amount (lost update)",
        )


class BookingCodeCollisionTests(ThrottlelessTestMixin, APITestCase):
    """BUG 15 REPRO (now asserting the fix) — a booking_code collision
    between two different rooms must be retried with a fresh code, not
    misreported as 'Room is no longer available' (409)."""

    @classmethod
    def setUpTestData(cls):
        (_, _, _, cls.room_2p, cls.room_4p, cls.package) = build_fixtures(
            ship_name="QA Phase2 Collision Ship"
        )

    def test_code_collision_is_not_reported_as_room_conflict(self):
        # 1st create draws COLLIDE1; 2nd create draws COLLIDE1 again (the
        # collision) and its retry must draw the fresh code and succeed.
        with patch(
            "apps.bookings.models.generate_booking_code",
            side_effect=["BK-COLLIDE1", "BK-COLLIDE1", "BK-QA2FRESH"],
        ):
            first = self.client.post(
                "/api/bookings/",
                booking_payload(self.package, self.room_4p, name="First"),
                format="json",
            )
            self.assertEqual(first.status_code, 201)
            # different room, same (forced) code — only the code collides
            second = self.client.post(
                "/api/bookings/",
                booking_payload(self.package, self.room_2p, name="Second"),
                format="json",
            )
        # DESIRED: the free room is booked (code regenerated/retried) —
        # certainly not a 'room unavailable' answer for an available room.
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["booking_code"], "BK-QA2FRESH")
        # the real room race still answers 409 with the machine code
        third = self.client.post(
            "/api/bookings/",
            booking_payload(self.package, self.room_2p, name="Third"),
            format="json",
        )
        self.assertEqual(third.status_code, 409)
        self.assertEqual(third.data.get("code"), "room_unavailable")
