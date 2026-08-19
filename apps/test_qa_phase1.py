"""Phase 1 QA — adversarial tests for Availability & Search.

Written by QA to probe the scenarios in MV_Alaska_QA_Prompts.md Phase 1.
These tests assert the *desired* behavior; failures indicate bugs that are
documented in qa-reports/phase1-availability-search.md.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.bookings.models import Booking
from apps.bookings.test_api import build_fixtures
from apps.packages.models import Package, PackageRoom
from apps.ships.models import Room
from apps.testing import ThrottlelessTestMixin

User = get_user_model()


class QaPhase1TestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures(ship_name="QA Ship")
        cls.staff = User.objects.create_user(
            username="qastaff", password="pass12345", is_staff=True
        )

    def staff_auth(self):
        tokens = self.client.post(
            "/api/staff/login/", {"username": "qastaff", "password": "pass12345"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def booking_payload(self, **overrides):
        room_id = overrides.pop("room_id", self.room_4p.id)
        adult_count = overrides.pop("adult_count", 2)
        kid_details = overrides.pop("kid_details", [])
        data = {
            "package_id": self.package.id,
            "customer_name": "QA Customer",
            "phone": "01700000000",
            "email": "qa@example.com",
            "rooms": [
                {
                    "room_id": room_id,
                    "adult_count": adult_count,
                    "kid_details": kid_details,
                }
            ],
        }
        data.update(overrides)
        return data


# ── Scenario 3: invalid date ranges must give a clean 400, never a 500 ──────


class InvalidDateRangeTests(QaPhase1TestCase):
    """Public users never submit date ranges (packages are fixed), so the
    only user-submitted date ranges enter via the staff package API."""

    def staff_package_payload(self, **overrides):
        data = {
            "ship": self.ship.id,
            "start_date": "2099-06-10",
            "end_date": "2099-06-12",
            "adult_price": "3000.00",
            "status": "open",
        }
        data.update(overrides)
        return data

    def test_create_package_end_before_start_returns_400(self):
        self.staff_auth()
        response = self.client.post(
            "/api/staff/packages/",
            self.staff_package_payload(start_date="2099-06-12", end_date="2099-06-10"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.status_code)

    def test_create_package_end_equals_start_returns_400(self):
        self.staff_auth()
        response = self.client.post(
            "/api/staff/packages/",
            self.staff_package_payload(start_date="2099-06-10", end_date="2099-06-10"),
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.status_code)

    def test_update_package_to_inverted_range_returns_400(self):
        self.staff_auth()
        response = self.client.patch(
            f"/api/staff/packages/{self.package.id}/",
            {"end_date": "2098-01-01"},  # before the existing 2099 start
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.status_code)


# ── Scenario 3/6: calendar params — extreme values and empty months ─────────


class CalendarEdgeTests(QaPhase1TestCase):
    def test_extreme_and_garbage_params_rejected_with_400(self):
        for query in (
            "?year=999999999999999999&month=1",
            "?year=2099&month=0",
            "?year=2099&month=-3",
            "?year=-1&month=5",
            "?year=2101&month=1",
            "?year=1999&month=12",
            "?year=2099.5&month=1",
            "?year=2099&month=1.5",
            "?year=2099&month=",
        ):
            response = self.client.get(f"/api/calendar/{query}")
            self.assertEqual(response.status_code, 400, f"{query} -> {response.status_code}")

    def test_boundary_years_accepted(self):
        for query in ("?year=2000&month=1", "?year=2100&month=12"):
            response = self.client.get(f"/api/calendar/{query}")
            self.assertEqual(response.status_code, 200, f"{query} -> {response.status_code}")

    def test_empty_month_returns_clean_empty_list(self):
        response = self.client.get("/api/calendar/?year=2098&month=6")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["dates"], [])

    def test_no_packages_at_all_returns_empty_list(self):
        Booking.objects.all().delete()
        PackageRoom.objects.all().delete()
        Package.objects.all().delete()
        response = self.client.get("/api/packages/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])


# ── Scenario 2: a booking must be visible in the next availability call ─────


class ImmediateAvailabilityTests(QaPhase1TestCase):
    def rooms(self):
        response = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 200)
        return {r["room_number"]: r for r in response.data}

    def test_full_api_roundtrip_booking_flips_room_to_booked(self):
        self.assertEqual(self.rooms()["T2"]["availability"], "available")
        create = self.client.post("/api/bookings/", self.booking_payload(), format="json")
        self.assertEqual(create.status_code, 201)
        self.assertEqual(self.rooms()["T2"]["availability"], "booked")

    def test_second_booking_for_same_room_gets_409(self):
        first = self.client.post("/api/bookings/", self.booking_payload(), format="json")
        self.assertEqual(first.status_code, 201)
        second = self.client.post("/api/bookings/", self.booking_payload(), format="json")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data.get("code"), "room_unavailable")

    def test_quote_also_sees_the_booked_room(self):
        self.client.post("/api/bookings/", self.booking_payload(), format="json")
        quote = self.client.post(
            "/api/bookings/quote/",
            {
                "package_id": self.package.id,
                "rooms": [{"room_id": self.room_4p.id, "adult_count": 1}],
            },
            format="json",
        )
        self.assertEqual(quote.status_code, 409)

    def test_unpaid_pending_booking_holds_the_room(self):
        """A PENDING booking with zero payments blocks the room (the hold);
        freeing it depends on the expire_stale_bookings cron."""
        self.client.post("/api/bookings/", self.booking_payload(), format="json")
        self.assertEqual(
            Booking.objects.get().status, Booking.Status.PENDING
        )
        self.assertEqual(self.rooms()["T2"]["availability"], "booked")


# ── Scenario 4: overlapping date ranges (package-level analog) ──────────────


class OverlappingPackagesTests(QaPhase1TestCase):
    """Overlapping active packages on one ship would let the same physical
    room be sold twice for the same night (Bug 2). The guard must hold at
    every layer: staff API (400), model clean() (admin panel), and the DB
    exclusion constraint (raw ORM / racing sessions)."""

    def overlap_payload(self, **overrides):
        data = {
            "ship": self.ship.id,
            "start_date": "2099-01-11",  # overlaps fixture package 10th–12th
            "end_date": "2099-01-13",
            "adult_price": "3000.00",
            "status": "open",
        }
        data.update(overrides)
        return data

    def test_staff_api_rejects_overlapping_package_with_400(self):
        self.staff_auth()
        response = self.client.post(
            "/api/staff/packages/", self.overlap_payload(), format="json"
        )
        self.assertEqual(response.status_code, 400, response.status_code)
        self.assertIn("overlap", str(response.data).lower())

    def test_model_clean_rejects_overlap(self):
        from django.core.exceptions import ValidationError

        package = Package(
            ship=self.ship,
            start_date=date(2099, 1, 11),
            end_date=date(2099, 1, 13),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        with self.assertRaises(ValidationError):
            package.full_clean()

    def test_db_constraint_blocks_raw_orm_overlap(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            Package.objects.create(
                ship=self.ship,
                start_date=date(2099, 1, 11),
                end_date=date(2099, 1, 13),
                adult_price=Decimal("3000.00"),
                status=Package.Status.OPEN,
            )

    def test_same_day_turnaround_and_drafts_still_allowed(self):
        # End date == next start date is a legitimate same-day turnaround.
        back_to_back = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 1, 12),  # fixture package ends 2099-01-12
            end_date=date(2099, 1, 14),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        back_to_back.full_clean()
        # Drafts may float over existing dates until they go live.
        draft = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 1, 10),
            end_date=date(2099, 1, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.DRAFT,
        )
        draft.full_clean()

    def test_double_booking_across_packages_no_longer_reachable(self):
        """The original Bug 2 repro: with the overlap guard in place, the
        overlapping package can no longer exist, so the double-sell path is
        closed end-to-end."""
        self.staff_auth()
        created = self.client.post(
            "/api/staff/packages/", self.overlap_payload(), format="json"
        )
        self.assertEqual(created.status_code, 400)
        # Only the fixture package exists — book its room once, as normal.
        self.client.credentials()  # drop staff auth; book as the public
        first = self.client.post("/api/bookings/", self.booking_payload(), format="json")
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            "/api/bookings/",
            self.booking_payload(customer_name="Other Guest"),
            format="json",
        )
        self.assertEqual(second.status_code, 409)


# ── Scenario 7: N+1 guards on the public availability endpoints ─────────────


class QueryCountTests(QaPhase1TestCase):
    def test_package_list_query_count_constant(self):
        for i in range(6):
            Package.objects.create(
                ship=self.ship,
                start_date=date(2099, 3 + i, 10),
                end_date=date(2099, 3 + i, 12),
                adult_price=Decimal("3000.00"),
                status=Package.Status.OPEN,
            )
        # Two, and two for ANY number of packages — which is what this guards.
        # 1: the package list itself (ship join included).
        # 2: the global ForeignerSurcharge policy row, fetched once per
        #    response and reused for every package in it. It is published on
        #    each package because that is where a booking client needs it.
        with self.assertNumQueries(2):
            self.client.get("/api/packages/")

    def test_calendar_query_count_constant(self):
        for day in (1, 8, 15, 22):
            Package.objects.create(
                ship=self.ship,
                start_date=date(2099, 7, day),
                end_date=date(2099, 7, day + 2),
                adult_price=Decimal("3000.00"),
                status=Package.Status.OPEN,
            )
        with self.assertNumQueries(1):
            self.client.get("/api/calendar/?year=2099&month=7")

    def test_rooms_endpoint_query_count_constant_with_many_rooms(self):
        for i in range(10):
            room = Room.objects.create(
                ship=self.ship, room_type=self.type_2p, room_number=f"Q{i}"
            )
            PackageRoom.objects.create(package=self.package, room=room)
        # package + annotated rooms + one gallery-image prefetch for ALL rooms,
        # + one lookup of the showcase cabins these rooms fall back to for
        # preview photos (none of them have their own here). Still constant
        # however many rooms the package has — which is what this guards.
        with self.assertNumQueries(4):
            self.client.get(f"/api/packages/{self.package.id}/rooms/")


# ── Scenario 5: timezone — backend date handling ─────────────────────────────


class TimezoneTests(QaPhase1TestCase):
    def test_public_listing_uses_dhaka_local_date_not_utc(self):
        """Between 00:00 and 05:59 Asia/Dhaka, UTC is still on the previous
        day. A package whose end_date is 'yesterday in UTC but today in Dhaka'
        must still be visible — public() must use localdate(), not date.today()
        in UTC. (Backend stores dates naive; cutoff datetimes are tz-aware.)"""
        today_dhaka = timezone.localdate()
        ending_today = Package.objects.create(
            ship=self.ship,
            start_date=today_dhaka - timedelta(days=2),
            end_date=today_dhaka,
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        ids = [p["id"] for p in self.client.get("/api/packages/").data]
        self.assertIn(ending_today.id, ids)

    def test_default_cutoff_is_noon_dhaka_day_before(self):
        package = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 5, 20),
            end_date=date(2099, 5, 22),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        local = timezone.localtime(package.booking_cutoff_datetime)
        self.assertEqual(
            (local.year, local.month, local.day, local.hour, local.minute),
            (2099, 5, 19, 12, 0),
        )
