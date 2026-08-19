"""Phase 1 QA (third pass) — new adversarial tests for Availability & Search.

Follow-up to apps/test_qa_phase1.py and apps/test_qa_phase1b.py. These tests
assert the *desired* behavior; failures indicate bugs documented in
qa-reports/phase1-availability-search.md (third pass).
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.bookings.models import Booking, Payment
from apps.bookings.test_api import build_fixtures
from apps.packages.models import Package, PackageRoom
from apps.ships.models import Room, Ship
from apps.testing import ThrottlelessTestMixin, create_booking

User = get_user_model()


class QuietAPIClient(APIClient):
    """Surface 500s as status codes instead of re-raised tracebacks, so the
    "must not crash" assertions read cleanly."""

    def __init__(self, **kwargs):
        kwargs.setdefault("raise_request_exception", False)
        super().__init__(**kwargs)


class QaPhase1cTestCase(ThrottlelessTestMixin, APITestCase):
    client_class = QuietAPIClient

    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures(ship_name="QA Ship C")
        cls.staff = User.objects.create_user(
            username="qa3staff", password="pass12345", is_staff=True
        )

    def auth(self):
        tokens = self.client.post(
            "/api/staff/login/", {"username": "qa3staff", "password": "pass12345"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def make_booking(self, room=None, package=None, status_=Booking.Status.PENDING):
        booking = create_booking(
            package or self.package,
            rooms=[{"room": room or self.room_4p, "adult_count": 2, "kid_details": []}],
        )
        if status_ != Booking.Status.PENDING:
            booking.status = status_
            booking.save()
        return booking


# ── Scenario 3: malformed identifiers must not crash the API ────────────────


class NonNumericLookupTests(QaPhase1cTestCase):
    """Every availability endpoint that takes an id must answer 400/404 to
    garbage — never 500. get_object_or_404 only catches DoesNotExist, so a
    non-numeric or out-of-bigint pk reaches the ORM/driver raw."""

    def assert_clean(self, url):
        response = self.client.get(url)
        self.assertIn(
            response.status_code,
            (400, 404),
            f"{url} answered {response.status_code} — expected a clean 400/404",
        )

    def test_public_package_detail_garbage_pk(self):
        self.assert_clean("/api/packages/abc/")

    def test_public_package_rooms_garbage_pk(self):
        self.assert_clean("/api/packages/abc/rooms/")

    def test_public_package_rooms_huge_pk(self):
        # Out of bigint range — the DB, not Python, is what rejects this.
        self.assert_clean("/api/packages/99999999999999999999999/rooms/")

    def test_public_ship_layout_garbage_pk(self):
        self.assert_clean("/api/ships/abc/layout/")

    def test_staff_package_garbage_pk(self):
        self.auth()
        self.assert_clean("/api/staff/packages/abc/")

    def test_staff_bookings_garbage_package_filter(self):
        self.auth()
        self.assert_clean("/api/staff/bookings/?package=abc")

    def test_staff_bookings_huge_package_filter(self):
        # Out-of-bigint filter values compare fine in Postgres (numeric cast)
        # and yield an empty page — 200-empty is clean; only a 500 is a bug.
        self.auth()
        response = self.client.get(
            "/api/staff/bookings/?package=99999999999999999999999"
        )
        self.assertIn(response.status_code, (200, 400, 404))
        if response.status_code == 200:
            self.assertEqual(response.data["results"], [])

    def test_staff_booking_summary_garbage_package_filter(self):
        self.auth()
        self.assert_clean("/api/staff/bookings/summary/?package=abc")


# ── Scenario 7/2: un-cancelling a booking whose room was resold ─────────────


class UncancelResoldRoomTests(QaPhase1cTestCase):
    def test_uncancel_into_occupied_room_is_clean_conflict(self):
        """Staff cancels booking A, the freed room is booked by B, then staff
        flips A back to pending (the status field is freely editable). The
        partial unique index (package, room, not-cancelled) fires on UPDATE —
        this must surface as a 400/409, not an IntegrityError 500."""
        booking_a = self.make_booking(status_=Booking.Status.CANCELLED)
        self.make_booking()  # B now actively holds room_4p
        self.auth()
        response = self.client.patch(
            f"/api/staff/bookings/{booking_a.id}/", {"status": "pending"}
        )
        self.assertIn(
            response.status_code,
            (400, 409),
            f"un-cancel onto an occupied room answered {response.status_code}",
        )
        booking_a.refresh_from_db()
        self.assertEqual(
            booking_a.status,
            Booking.Status.CANCELLED,
            "booking A must stay cancelled after the failed un-cancel",
        )


# ── Scenario 4/1: cancelled packages as a double-selling back door ──────────


class InactivePackageBookingTests(QaPhase1cTestCase):
    def test_staff_cannot_book_on_cancelled_package(self):
        """CANCELLED packages are exempt from the ship-date exclusion
        constraint (first-pass Bug 2 fix), so one may overlap an OPEN voyage.
        If staff can still create bookings on it, the same physical room is
        sold twice for the same night — reopening Bug 2 through the staff API."""
        cancelled = Package.objects.create(
            ship=self.ship,
            start_date=self.package.start_date + timedelta(days=1),  # overlaps
            end_date=self.package.end_date + timedelta(days=1),
            adult_price=Decimal("3000.00"),
            status=Package.Status.CANCELLED,
        )
        PackageRoom.objects.create(package=cancelled, room=self.room_4p)

        # Customer books the room on the OPEN package (public API).
        public = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.id,
                "rooms": [{"room_id": self.room_4p.id, "adult_count": 2}],
                "customer_name": "Karim",
                "phone": "01800000000",
                "email": "karim@example.com",
            },
            format="json",
        )
        self.assertEqual(public.status_code, 201)

        self.auth()
        response = self.client.post(
            "/api/staff/bookings/",
            {
                "package_id": cancelled.id,
                "rooms": [{"room_id": self.room_4p.id, "adult_count": 2}],
                "customer_name": "Jorim",
                "phone": "01900000000",
                "email": "jorim@example.com",
            },
            format="json",
        )
        self.assertEqual(
            response.status_code,
            400,
            "staff booking on a CANCELLED package must be rejected — it sells "
            f"the same cabin twice for the same night (got {response.status_code})",
        )

    def test_staff_cannot_book_on_draft_package(self):
        draft = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 6, 10),
            end_date=date(2099, 6, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.DRAFT,
        )
        PackageRoom.objects.create(package=draft, room=self.room_2p)
        self.auth()
        response = self.client.post(
            "/api/staff/bookings/",
            {
                "package_id": draft.id,
                "rooms": [{"room_id": self.room_2p.id, "adult_count": 2}],
                "customer_name": "Jorim",
                "phone": "01900000000",
                "email": "jorim@example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


# ── Scenario 1: package must not drift to a ship its rooms don't belong to ──


class PackageShipChangeTests(QaPhase1cTestCase):
    def test_ship_change_with_attached_rooms_rejected(self):
        """PATCHing a package to a different ship while PackageRooms (and
        bookings) from the old ship are attached makes the public rooms
        endpoint sell cabins that are not on the sailing vessel. PackageRoom
        .clean() guards this pairing but is never re-run on a package update."""
        other_ship = Ship.objects.create(name="QA Other Ship C")
        Room.objects.create(
            ship=other_ship, room_type=self.type_2p, room_number="O1"
        )
        self.make_booking()  # room_4p (QA Ship C) actively booked
        self.auth()
        response = self.client.patch(
            f"/api/staff/packages/{self.package.id}/", {"ship": other_ship.id}
        )
        self.assertEqual(
            response.status_code,
            400,
            "ship change with attached rooms/bookings from the old ship must "
            f"be rejected (got {response.status_code})",
        )
        self.package.refresh_from_db()
        self.assertEqual(self.package.ship_id, self.ship.id)


# ── Scenario 7/2: hold expiry races an in-flight payment session ────────────


class HoldExpiryInFlightPaymentTests(QaPhase1cTestCase):
    def test_booking_with_pending_payment_is_not_expired(self):
        """A customer who clicked "pay" is at the SSLCommerz page: the booking
        has a PENDING payment. If the hold expiry cancels it anyway, the room
        is released for resale while real money is in flight — pay at minute
        31 and the charge lands on a cancelled booking whose room may already
        belong to someone else."""
        booking = self.make_booking()
        Payment.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_type=Payment.PaymentType.FULL,
            transaction_id=f"{booking.booking_code}-P1",
            status=Payment.Status.PENDING,
        )
        # Age the booking past the hold window (created_at is auto_now_add).
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(minutes=45)
        )
        call_command("expire_stale_bookings", verbosity=0)
        booking.refresh_from_db()
        self.assertNotEqual(
            booking.status,
            Booking.Status.CANCELLED,
            "hold expiry cancelled a booking with a payment in flight — the "
            "room is released for resale while the customer is at the gateway",
        )

    def test_booking_with_no_payment_still_expires(self):
        """Control: the hold window keeps working for truly abandoned holds."""
        booking = self.make_booking()
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(minutes=45)
        )
        call_command("expire_stale_bookings", verbosity=0)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
