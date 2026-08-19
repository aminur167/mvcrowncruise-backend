"""Phase 4 QA — Booking cutoff automation & timezone handling.

Adversarial tests for qa-reports/phase4-cutoff-timezone.md. Tests assert the
*desired* behavior; failures indicate bugs documented in that report.

Clock convention used throughout: Asia/Dhaka is UTC+6 with no DST, so
"noon Dhaka" == 06:00 UTC. All simulated instants are constructed in UTC and
compared as instants — a test that passes here passes identically on a UTC
host (Railway), a Dhaka host, or any other server OS timezone, because every
code path under test uses aware datetimes (USE_TZ=True).
"""

import datetime as dt
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.bookings.test_api import build_fixtures
from apps.packages.models import Package, PackageRoom
from apps.testing import ThrottlelessTestMixin

User = get_user_model()

UTC = dt.timezone.utc


@contextmanager
def frozen_now(instant):
    """Freeze django.utils.timezone.now at an aware instant.

    Everything in the cutoff path (is_bookable, localdate in public(),
    auto_now_add) resolves timezone.now at call time, so patching the module
    attribute covers the whole stack. simplejwt keeps its own clock, so staff
    tokens issued outside the freeze stay valid inside it.
    """
    assert instant.tzinfo is not None
    with mock.patch("django.utils.timezone.now", return_value=instant):
        yield


class QaPhase4TestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,  # start 2099-01-10, end 2099-01-12 → auto cutoff
        ) = build_fixtures(ship_name="QA Ship P4")

    def payload(self, **overrides):
        room_id = overrides.pop("room_id", self.room_4p.id)
        adult_count = overrides.pop("adult_count", 2)
        kid_details = overrides.pop("kid_details", [])
        data = {
            "package_id": self.package.id,
            "customer_name": "Rahim Uddin",
            "phone": "01700000000",
            "email": "rahim@example.com",
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

    def post_booking(self, **overrides):
        return self.client.post("/api/bookings/", self.payload(**overrides), format="json")


# ── 1. The exact boundary: one minute / one second either side of cutoff ────


class CutoffBoundaryTests(QaPhase4TestCase):
    """Fixture package departs 2099-01-10, so the auto cutoff is
    2099-01-09 12:00 Asia/Dhaka == 2099-01-09 06:00 UTC."""

    CUTOFF_UTC = dt.datetime(2099, 1, 9, 6, 0, 0, tzinfo=UTC)

    def test_auto_cutoff_is_noon_dhaka_day_before_departure(self):
        self.assertEqual(self.package.booking_cutoff_datetime, self.CUTOFF_UTC)

    def test_booking_one_minute_before_cutoff_succeeds(self):
        with frozen_now(self.CUTOFF_UTC - timedelta(minutes=1)):
            response = self.post_booking()
        self.assertEqual(response.status_code, 201, response.data)

    def test_booking_one_second_before_cutoff_succeeds(self):
        with frozen_now(self.CUTOFF_UTC - timedelta(seconds=1)):
            response = self.post_booking()
        self.assertEqual(response.status_code, 201, response.data)

    def test_booking_exactly_at_cutoff_rejected(self):
        # "closes AT noon": now < cutoff is strict, so 12:00:00.000000 is
        # already closed. Documents the boundary semantics.
        with frozen_now(self.CUTOFF_UTC):
            response = self.post_booking()
        self.assertEqual(response.status_code, 400)
        self.assertIn("package_id", response.data)

    def test_booking_one_minute_after_cutoff_rejected(self):
        with frozen_now(self.CUTOFF_UTC + timedelta(minutes=1)):
            response = self.post_booking()
        self.assertEqual(response.status_code, 400)
        self.assertIn("package_id", response.data)

    def test_quote_mirrors_the_same_boundary(self):
        quote = lambda: self.client.post(
            "/api/bookings/quote/", self.payload(), format="json"
        )
        with frozen_now(self.CUTOFF_UTC - timedelta(minutes=1)):
            self.assertEqual(quote().status_code, 200)
        with frozen_now(self.CUTOFF_UTC + timedelta(minutes=1)):
            self.assertEqual(quote().status_code, 400)


# ── 2. Server OS timezone vs enforcement timezone vs user timezone ──────────


class ServerVsUserTimezoneTests(QaPhase4TestCase):
    """Production is Railway (UTC container) serving Bangladeshi customers
    (UTC+6) and possibly expats (any zone). Enforcement must be a pure
    instant comparison — the same for every party."""

    CUTOFF_UTC = dt.datetime(2099, 1, 9, 6, 0, 0, tzinfo=UTC)

    def test_cutoff_is_stored_as_the_correct_utc_instant(self):
        # Noon Dhaka == 06:00 UTC. If default_cutoff used the OS clock or a
        # naive datetime, a UTC host would store noon UTC (6 h late — bookings
        # would stay open 6 extra hours). Instant equality proves it doesn't.
        self.assertEqual(self.package.booking_cutoff_datetime, self.CUTOFF_UTC)

    def test_bookability_flip_is_identical_under_any_active_timezone(self):
        # timezone.override simulates request-level timezone activation (what
        # a per-user timezone middleware would do). The gate must not move.
        for tzname in ("UTC", "America/New_York", "Asia/Dhaka", "Australia/Sydney"):
            with timezone.override(tzname):
                with frozen_now(self.CUTOFF_UTC - timedelta(seconds=1)):
                    self.assertTrue(
                        self.package.is_bookable(),
                        f"open side of the boundary moved under {tzname}",
                    )
                with frozen_now(self.CUTOFF_UTC):
                    self.assertFalse(
                        self.package.is_bookable(),
                        f"closed side of the boundary moved under {tzname}",
                    )

    def test_api_serializes_cutoff_with_explicit_dhaka_offset(self):
        # The frontend gets an unambiguous instant ("+06:00"), so any client
        # timezone can convert correctly. A naive string here would be the
        # classic silent-mismatch bug.
        response = self.client.get(f"/api/packages/{self.package.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["booking_cutoff_datetime"], "2099-01-09T12:00:00+06:00"
        )

    def test_bangladesh_expat_in_new_york_books_one_minute_before_cutoff(self):
        # 05:59 UTC == 00:59 in New York (EST) == 11:59 in Dhaka. The user's
        # wall clock is irrelevant; the shared instant decides.
        with frozen_now(dt.datetime(2099, 1, 9, 5, 59, tzinfo=UTC)):
            response = self.post_booking()
        self.assertEqual(response.status_code, 201, response.data)


# ── 3. DST ───────────────────────────────────────────────────────────────────


class DstTests(QaPhase4TestCase):
    def test_dhaka_has_no_dst_cutoff_offset_constant_all_year(self):
        # Bangladesh abolished DST after 2009; the cutoff wall time (noon)
        # must map to the same UTC offset in every month. If TIME_ZONE were
        # ever changed to a DST zone, this test starts guarding make_aware
        # against nonexistent/ambiguous noons.
        tz = timezone.get_default_timezone()
        for month in range(1, 13):
            aware = timezone.make_aware(dt.datetime(2099, month, 15, 12, 0), tz)
            self.assertEqual(
                aware.utcoffset(), timedelta(hours=6), f"offset moved in month {month}"
            )

    def test_cutoff_coinciding_with_us_fall_back_instant_still_exact(self):
        # A voyage departing 2026-11-02 has its cutoff at 2026-11-01 06:00 UTC
        # — the exact instant US clocks fall back (02:00 EDT → 01:00 EST).
        # For a customer in New York the local hour 01:00–02:00 happens twice;
        # the gate must still flip once, at the single UTC instant.
        pkg = Package.objects.create(
            ship=self.ship,
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 4),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        cutoff_utc = dt.datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
        self.assertEqual(pkg.booking_cutoff_datetime, cutoff_utc)
        with timezone.override("America/New_York"):
            with frozen_now(cutoff_utc - timedelta(seconds=1)):
                self.assertTrue(pkg.is_bookable())
            with frozen_now(cutoff_utc):
                self.assertFalse(pkg.is_bookable())


# ── 4. Midnight, month and year boundaries ───────────────────────────────────


class CalendarBoundaryDefaultCutoffTests(ThrottlelessTestMixin, APITestCase):
    """default_cutoff must do real calendar arithmetic — day-before across
    month starts, year starts, and (non-)leap Februaries."""

    @classmethod
    def setUpTestData(cls):
        (cls.ship, *_rest) = build_fixtures(ship_name="QA Ship P4B")

    def make_package(self, start, end):
        return Package.objects.create(
            ship=self.ship,
            start_date=start,
            end_date=end,
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )

    def assert_cutoff(self, pkg, y, m, d):
        local = timezone.localtime(pkg.booking_cutoff_datetime)
        self.assertEqual(
            (local.year, local.month, local.day, local.hour, local.minute),
            (y, m, d, 12, 0),
        )

    def test_month_start_departure_cutoff_lands_on_previous_month(self):
        pkg = self.make_package(date(2099, 8, 1), date(2099, 8, 3))
        self.assert_cutoff(pkg, 2099, 7, 31)

    def test_new_years_day_departure_cutoff_lands_on_previous_year(self):
        pkg = self.make_package(date(2100, 1, 1), date(2100, 1, 3))
        self.assert_cutoff(pkg, 2099, 12, 31)

    def test_march_first_leap_year_cutoff_is_feb_29(self):
        pkg = self.make_package(date(2028, 3, 1), date(2028, 3, 3))
        self.assert_cutoff(pkg, 2028, 2, 29)

    def test_march_first_century_non_leap_cutoff_is_feb_28(self):
        pkg = self.make_package(date(2100, 3, 1), date(2100, 3, 3))
        self.assert_cutoff(pkg, 2100, 2, 28)


class YearBoundaryBookingFlowTests(QaPhase4TestCase):
    """End-to-end across a year boundary: voyage 2099-12-31 → 2100-01-02,
    cutoff 2099-12-30 noon Dhaka (06:00 UTC)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.nye_package = Package.objects.create(
            ship=cls.ship,
            start_date=date(2099, 12, 31),
            end_date=date(2100, 1, 2),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        PackageRoom.objects.create(package=cls.nye_package, room=cls.room_4p)

    CUTOFF_UTC = dt.datetime(2099, 12, 30, 6, 0, tzinfo=UTC)

    def test_booking_open_then_closed_around_the_year_boundary_cutoff(self):
        before = self.post_after_freeze(self.CUTOFF_UTC - timedelta(minutes=1))
        self.assertEqual(before.status_code, 201, before.data)
        after = self.post_after_freeze(self.CUTOFF_UTC + timedelta(minutes=1))
        self.assertEqual(after.status_code, 400)

    def post_after_freeze(self, instant):
        with frozen_now(instant):
            return self.post_booking(
                package_id=self.nye_package.id, room_id=self.room_4p.id
            )


class DhakaMidnightRolloverTests(QaPhase4TestCase):
    def test_manual_cutoff_at_dhaka_midnight_enforced_exactly(self):
        # Midnight Dhaka on departure-eve == 18:00 UTC the previous UTC day —
        # the UTC date and the Dhaka date disagree at this instant.
        midnight_dhaka_utc = dt.datetime(2099, 1, 9, 18, 0, tzinfo=UTC)  # Jan 10 00:00 Dhaka
        self.package.booking_cutoff_datetime = midnight_dhaka_utc
        self.package.save()
        with frozen_now(midnight_dhaka_utc - timedelta(minutes=1)):
            self.assertEqual(self.post_booking().status_code, 201)
        with frozen_now(midnight_dhaka_utc + timedelta(minutes=1)):
            self.assertEqual(self.post_booking().status_code, 400)

    def test_public_visibility_rolls_over_at_dhaka_midnight_not_utc(self):
        # end_date 2099-01-12: at 23:59 Dhaka Jan 12 (17:59 UTC) the package
        # is still listed; at 00:01 Dhaka Jan 13 (18:01 UTC — same UTC day!)
        # it is gone. Proves public() runs on the Dhaka calendar.
        listed = lambda: any(
            p["id"] == self.package.id
            for p in self.client.get("/api/packages/").data
        )
        with frozen_now(dt.datetime(2099, 1, 12, 17, 59, tzinfo=UTC)):
            self.assertTrue(listed(), "package vanished before Dhaka midnight")
        with frozen_now(dt.datetime(2099, 1, 12, 18, 1, tzinfo=UTC)):
            self.assertFalse(listed(), "package survived past Dhaka midnight")


# ── 5. Backend enforcement (not just UI) & staff override ───────────────────


class BackendEnforcementTests(QaPhase4TestCase):
    def past_cutoff(self):
        self.package.booking_cutoff_datetime = timezone.now() - timedelta(hours=1)
        self.package.save()

    def test_direct_api_booking_after_cutoff_rejected_even_though_package_listed(self):
        # The package stays visible (is_bookable=false) — a scraped/cached UI
        # or a hand-crafted POST must still be refused server-side.
        self.past_cutoff()
        listing = self.client.get("/api/packages/").data
        entry = next(p for p in listing if p["id"] == self.package.id)
        self.assertFalse(entry["is_bookable"])
        self.assertEqual(entry["booking_status"], "closed")
        response = self.post_booking()
        self.assertEqual(response.status_code, 400)
        self.assertIn("package_id", response.data)
        response = self.client.post(
            "/api/bookings/quote/", self.payload(), format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_staff_manual_booking_past_cutoff_still_allowed(self):
        # PRD §5.5: the cutoff has a staff-side manual override. Guard it so a
        # future "fix" doesn't accidentally lock staff out too.
        self.past_cutoff()
        User.objects.create_user(
            username="p4staff", password="pass12345", is_staff=True
        )
        tokens = self.client.post(
            "/api/staff/login/", {"username": "p4staff", "password": "pass12345"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.post(
            "/api/staff/bookings/",
            {
                "package_id": self.package.id,
                "rooms": [
                    {"room_id": self.room_4p.id, "adult_count": 2, "kid_details": []}
                ],
                "customer_name": "Walk-in Guest",
                "phone": "01800000000",
                "email": "walkin@example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)


# ── 6. Naive datetime input & mis-set cutoff sanity ─────────────────────────


class StaffCutoffInputTests(QaPhase4TestCase):
    def auth(self):
        User.objects.create_user(username="p4adm", password="pass12345", is_staff=True)
        tokens = self.client.post(
            "/api/staff/login/", {"username": "p4adm", "password": "pass12345"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def patch_cutoff(self, value):
        return self.client.patch(
            f"/api/staff/packages/{self.package.id}/",
            {"booking_cutoff_datetime": value},
            format="json",
        )

    def test_naive_datetime_input_is_interpreted_as_dhaka_time(self):
        # A client that forgets the offset must not silently get UTC (which
        # would push the cutoff 6 h late). DRF + USE_TZ interprets naive input
        # in TIME_ZONE — assert the stored instant proves it.
        self.auth()
        response = self.patch_cutoff("2099-01-09T15:00:00")
        self.assertEqual(response.status_code, 200, response.data)
        self.package.refresh_from_db()
        self.assertEqual(
            self.package.booking_cutoff_datetime,
            dt.datetime(2099, 1, 9, 9, 0, tzinfo=UTC),  # 15:00 Dhaka == 09:00 UTC
        )

    def test_utc_suffixed_input_keeps_its_instant(self):
        self.auth()
        response = self.patch_cutoff("2099-01-09T15:00:00Z")
        self.assertEqual(response.status_code, 200, response.data)
        self.package.refresh_from_db()
        self.assertEqual(
            self.package.booking_cutoff_datetime,
            dt.datetime(2099, 1, 9, 15, 0, tzinfo=UTC),
        )


class MisSetCutoffAfterDepartureTests(QaPhase4TestCase):
    """A cutoff after the ship has sailed must not keep selling the voyage.

    Phase 4 Bug 1 repros, updated post-fix: the staff API now rejects a
    cutoff dated after departure day (Package.clean), and is_bookable()
    refuses to sell once the voyage has departed even if bad data slips in
    through the ORM.
    """

    def staff_auth(self):
        User.objects.create_user(username="p4adm2", password="pass12345", is_staff=True)
        tokens = self.client.post(
            "/api/staff/login/", {"username": "p4adm2", "password": "pass12345"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def patch_package(self, data):
        return self.client.patch(
            f"/api/staff/packages/{self.package.id}/", data, format="json"
        )

    def test_staff_api_rejects_cutoff_after_departure(self):
        self.staff_auth()
        response = self.patch_package(
            # a year late — classic typo for 2099-01-09
            {"booking_cutoff_datetime": "2100-01-09T12:00:00"}
        )
        self.assertEqual(
            response.status_code,
            400,
            "staff API accepted a booking cutoff dated AFTER the voyage ends "
            f"({response.status_code}) — bookings will stay open mid-voyage",
        )
        self.assertIn("booking_cutoff_datetime", response.data)

    def test_public_api_cannot_book_a_voyage_that_already_departed(self):
        # Cutoff typo'd to next year (forced past validation via the ORM);
        # the clock is mid-voyage: Jan 11 noon Dhaka on a Jan 10–12 tour.
        # The ship left port yesterday — is_bookable() must backstop.
        self.package.booking_cutoff_datetime = dt.datetime(
            2100, 1, 9, 6, 0, tzinfo=UTC
        )
        self.package.save()
        with frozen_now(dt.datetime(2099, 1, 11, 6, 0, tzinfo=UTC)):
            response = self.post_booking()
        self.assertEqual(
            response.status_code,
            400,
            "public API sold a cabin on a voyage that departed yesterday "
            f"(got {response.status_code})",
        )

    def test_day_of_departure_cutoff_still_allowed_and_bookable(self):
        # The fix must not over-block: an admin extending the cutoff to
        # departure morning (08:00 Dhaka on start day) is legitimate, and a
        # customer booking at 05:00 that morning succeeds.
        self.staff_auth()
        response = self.patch_package(
            {"booking_cutoff_datetime": "2099-01-10T08:00:00"}
        )
        self.assertEqual(response.status_code, 200, response.data)
        with frozen_now(dt.datetime(2099, 1, 9, 23, 0, tzinfo=UTC)):  # 05:00 Dhaka day-of
            self.assertEqual(self.post_booking().status_code, 201)
        with frozen_now(dt.datetime(2099, 1, 10, 2, 30, tzinfo=UTC)):  # 08:30 Dhaka
            self.assertEqual(self.post_booking().status_code, 400)

    def test_moving_voyage_earlier_resyncs_auto_cutoff_instead_of_rejecting(self):
        # The staff dialog always re-sends the current cutoff alongside a
        # date edit. Moving the voyage EARLIER makes the old (auto-derived)
        # cutoff land after the new start date — save() resyncs it, so
        # validation must not reject the request.
        self.staff_auth()
        response = self.patch_package(
            {
                "start_date": "2098-12-20",
                "end_date": "2098-12-22",
                # old auto cutoff (noon Dhaka 2099-01-09), as the form sends it
                "booking_cutoff_datetime": "2099-01-09T12:00:00+06:00",
            }
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.package.refresh_from_db()
        self.assertEqual(
            self.package.booking_cutoff_datetime,
            dt.datetime(2098, 12, 19, 6, 0, tzinfo=UTC),  # noon Dhaka, new day-before
        )
