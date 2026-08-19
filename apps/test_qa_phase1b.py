"""Phase 1 QA (second pass) — new adversarial tests for Availability & Search.

Follow-up to apps/test_qa_phase1.py. These tests assert the *desired*
behavior; failures indicate bugs documented in
qa-reports/phase1-availability-search.md.
"""

import datetime as dt
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.bookings.test_api import build_fixtures
from apps.packages.models import Package
from apps.testing import ThrottlelessTestMixin


class QaPhase1bTestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures(ship_name="QA Ship B")


# ── Scenario 5: calendar default month must follow Dhaka, not the OS clock ──


class _UtcServerDate(dt.date):
    """Simulates the naive system clock of a UTC server (e.g. Railway):
    Dhaka is already 2099-08-01 (01:00 +06) but UTC is still 2099-07-31."""

    @classmethod
    def today(cls):
        return cls(2099, 7, 31)


class CalendarDefaultMonthTimezoneTests(QaPhase1bTestCase):
    SIMULATED_NOW = dt.datetime(2099, 7, 31, 19, 0, tzinfo=dt.timezone.utc)

    def test_default_month_uses_dhaka_local_date(self):
        """GET /api/calendar/ without params must default to the month it
        currently is in Asia/Dhaka — the same clock every other availability
        decision uses (Package.objects.public, is_bookable) — not the server
        OS date, which on a UTC host lags Dhaka by 6 hours."""
        with (
            mock.patch("apps.packages.views.date", _UtcServerDate),
            mock.patch(
                "django.utils.timezone.now", return_value=self.SIMULATED_NOW
            ),
        ):
            expected = timezone.localdate()  # what Dhaka says "today" is
            self.assertEqual((expected.year, expected.month), (2099, 8))
            response = self.client.get("/api/calendar/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.data["year"], response.data["month"]),
            (2099, 8),
            f"calendar defaulted to {response.data['year']}-{response.data['month']} "
            "(server OS date) instead of the current Asia/Dhaka month",
        )


# ── Scenario 1: month-boundary exactness (no off-by-one at either edge) ─────


class CalendarMonthBoundaryTests(QaPhase1bTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.spanning = Package.objects.create(
            ship=cls.ship,
            start_date=date(2099, 8, 30),
            end_date=date(2099, 9, 1),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )

    def month_days(self, year, month):
        response = self.client.get(f"/api/calendar/?year={year}&month={month}")
        self.assertEqual(response.status_code, 200)
        return {
            entry["date"]
            for entry in response.data["dates"]
            if any(p["id"] == self.spanning.id for p in entry["packages"])
        }

    def test_spanning_package_days_exact_in_both_months(self):
        self.assertEqual(self.month_days(2099, 8), {"2099-08-30", "2099-08-31"})
        self.assertEqual(self.month_days(2099, 9), {"2099-09-01"})

    def test_adjacent_months_untouched(self):
        self.assertEqual(self.month_days(2099, 7), set())
        self.assertEqual(self.month_days(2099, 10), set())


# ── Scenario 6/2: closed-status voyage disappears from the calendar ─────────


class ClosedPackageCalendarTests(QaPhase1bTestCase):
    def test_closed_status_package_absent_from_calendar(self):
        """Documents current behavior: status=CLOSED removes the voyage from
        the public calendar entirely, while is_booking_open=False keeps it
        visible as sold-out. Two adjacent staff controls, opposite public
        results — flagged as an observation in the QA report."""
        self.package.status = Package.Status.CLOSED
        self.package.save()
        response = self.client.get("/api/calendar/?year=2099&month=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["dates"], [])  # gone, not "sold out"

    def test_manually_closed_booking_still_visible_not_bookable(self):
        self.package.is_booking_open = False
        self.package.save()
        response = self.client.get("/api/calendar/?year=2099&month=1")
        entries = [
            p for e in response.data["dates"] for p in e["packages"]
        ]
        self.assertTrue(entries)
        self.assertFalse(any(p["is_bookable"] for p in entries))


# ── Scenario 2/3: quote traffic must not consume the booking rate budget ────


class BookingThrottleScopeTests(APITestCase):
    """No ThrottlelessTestMixin — real rates (booking: 10/min) on purpose."""

    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures(ship_name="QA Ship T")

    def setUp(self):
        cache.clear()  # reset throttle counters between tests

    def tearDown(self):
        cache.clear()

    def test_live_quotes_do_not_block_the_actual_booking(self):
        """The wizard fires a quote on every pax change. A customer who
        adjusts guests ten times within a minute must still be able to
        submit the booking itself."""
        quote_payload = {
            "package_id": self.package.id,
            "rooms": [{"room_id": self.room_4p.id, "adult_count": 1}],
        }
        for i in range(10):
            response = self.client.post(
                "/api/bookings/quote/", quote_payload, format="json"
            )
            # Quotes ride their own "quote" scope (60/min) — never throttled
            # at wizard-interaction volumes.
            self.assertEqual(response.status_code, 200, i)
        create = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.id,
                "customer_name": "QA Customer",
                "phone": "01700000000",
                "email": "qa@example.com",
                "rooms": [
                    {"room_id": self.room_4p.id, "adult_count": 2, "kid_details": []}
                ],
            },
            format="json",
        )
        self.assertEqual(
            create.status_code,
            201,
            f"booking POST got {create.status_code} — quote calls exhausted "
            "the shared 'booking' throttle bucket",
        )


# ── Scenario 3: leftover odd-input calendar params ──────────────────────────


class CalendarParamOddballTests(QaPhase1bTestCase):
    def test_whitespace_and_plus_signs_handled_consistently(self):
        # int() accepts these; they must not crash and must behave like the
        # plain numbers.
        for query, expected_month in (("?year=2099&month=+8", 8), ("?year= 2099 &month= 8 ", 8)):
            response = self.client.get(f"/api/calendar/{query}")
            self.assertEqual(response.status_code, 200, query)
            self.assertEqual(response.data["month"], expected_month, query)

    def test_repeated_params_use_last_value(self):
        response = self.client.get("/api/calendar/?year=2099&month=2&month=3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["month"], 3)


# ── Scenario 1: rooms endpoint 404s for non-public packages ─────────────────


class RoomsEndpointVisibilityTests(QaPhase1bTestCase):
    def test_rooms_of_draft_package_not_reachable(self):
        self.package.status = Package.Status.DRAFT
        self.package.save()
        response = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 404)

    def test_rooms_of_finished_package_not_reachable(self):
        today = timezone.localdate()
        Package.objects.filter(pk=self.package.pk).update(
            start_date=today - timedelta(days=10),
            end_date=today - timedelta(days=8),
        )
        response = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 404)
