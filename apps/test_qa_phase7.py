"""QA Phase 7 — booking-flow edge cases (exploratory / adversarial).

Focus of this pass:
1. Booking the very last available room of a type (off-by-one).
2. Network interruption mid-booking (request lands, response lost) → retry.
3. Rapid double-submit of "Confirm Booking" (frontend disable + backend
   idempotency).
4. Browser back button after a completed booking, then resubmit.
5. Session/token expiry mid-booking-flow.
6. Extremely long input in text fields (name, phone, special requests).

Tests that FAIL are reproductions of open issues, asserting the *desired*
behavior (same convention as test_qa_phase1*.py / test_qa_phase2.py).

Run: manage.py test apps.test_qa_phase7 --settings=config.settings_qa5
"""

import threading
from decimal import Decimal

from django.db import connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from apps.bookings.models import Booking, BookingRoom
from apps.bookings.test_api import build_fixtures
from apps.testing import ThrottlelessTestMixin


def booking_payload(package, room, **overrides):
    adult_count = overrides.pop("adult_count", 1)
    kid_details = overrides.pop("kid_details", [])
    data = {
        "package_id": package.id,
        "customer_name": "Edge Case",
        "phone": "01700000000",
        "email": "edge@example.com",
        "rooms": [
            {"room_id": room.id, "adult_count": adult_count, "kid_details": kid_details}
        ],
    }
    data.update(overrides)
    return data


class LastRoomTests(ThrottlelessTestMixin, APITestCase):
    """Edge case 1 — the very last available room of a type."""

    def setUp(self):
        (
            self.ship,
            self.type_2p,
            self.type_4p,
            self.room_2p,
            self.room_4p,
            self.package,
        ) = build_fixtures(ship_name="QA P7 LastRoom")

    def _availability(self, room):
        resp = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        for r in resp.data:
            if r["room_number"] == room.room_number:
                return r["availability"]
        return None

    def test_last_room_shows_available_until_booked(self):
        # Only one 2-person room (T1) in the fixture — it is the "last" one.
        self.assertEqual(self._availability(self.room_2p), "available")

    def test_booking_last_room_flips_it_to_booked_not_gone(self):
        resp = self.client.post(
            "/api/bookings/", booking_payload(self.package, self.room_2p),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        # Still listed, now "booked" — never silently dropped from the grid.
        self.assertEqual(self._availability(self.room_2p), "booked")

    def test_last_room_cannot_be_double_booked_sequentially(self):
        first = self.client.post(
            "/api/bookings/", booking_payload(self.package, self.room_2p),
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            "/api/bookings/",
            booking_payload(self.package, self.room_2p, email="other@example.com"),
            format="json",
        )
        self.assertIn(second.status_code, (400, 409))
        self.assertEqual(
            BookingRoom.objects.filter(
                package=self.package, room=self.room_2p, is_active=True
            ).count(),
            1,
        )


class NetworkRetryTests(ThrottlelessTestMixin, APITestCase):
    """Edge case 2 & 3 — a lost response / a resubmit of the *same* booking.

    The frontend cannot tell 'request never arrived' from 'response was lost',
    so on any perceived failure it may resubmit the identical payload. This
    asserts the backend never lets that create a second booking for the room.
    """

    def setUp(self):
        (*_, self.room_2p, self.room_4p, self.package) = build_fixtures(
            ship_name="QA P7 Retry"
        )

    def test_identical_resubmit_does_not_duplicate(self):
        payload = booking_payload(self.package, self.room_4p)
        first = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        # Exact same payload again (the "retry after lost response" case).
        second = self.client.post("/api/bookings/", payload, format="json")
        self.assertIn(second.status_code, (400, 409))
        self.assertEqual(
            BookingRoom.objects.filter(
                package=self.package, room=self.room_4p, is_active=True
            ).count(),
            1,
        )

    def test_resubmit_error_shape_is_room_unavailable(self):
        """The retry's 4xx must be the room-taken signal the SPA branches on,
        not a generic validation blob — otherwise the UI can't guide the user
        to pick another room."""
        payload = booking_payload(self.package, self.room_4p)
        self.client.post("/api/bookings/", payload, format="json")
        second = self.client.post("/api/bookings/", payload, format="json")
        body = str(second.data).lower()
        self.assertTrue(
            second.status_code == 409 or "room_unavailable" in body
            or "unavailable" in body or "no longer available" in body,
            f"Unhelpful resubmit error ({second.status_code}): {second.data}",
        )


class DoubleSubmitRaceTests(ThrottlelessTestMixin, TransactionTestCase):
    """Edge case 3 — rapid double-click fires two creates before the first
    returns. Backend idempotency must hold even with zero frontend debounce."""

    def setUp(self):
        (*_, self.room_2p, self.room_4p, self.package) = build_fixtures(
            ship_name="QA P7 DoubleClick"
        )

    def test_concurrent_identical_submits_create_one_booking(self):
        payload = booking_payload(self.package, self.room_4p)
        barrier = threading.Barrier(6)
        results = []
        lock = threading.Lock()

        def attempt():
            try:
                barrier.wait(timeout=15)
                resp = APIClient().post("/api/bookings/", payload, format="json")
                with lock:
                    results.append(resp.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(results.count(201), 1, results)
        self.assertEqual(
            BookingRoom.objects.filter(
                package=self.package, room=self.room_4p, is_active=True
            ).count(),
            1,
        )


class LongInputTests(ThrottlelessTestMixin, APITestCase):
    """Edge case 6 — extremely long values in text fields."""

    def setUp(self):
        (*_, self.room_2p, self.room_4p, self.package) = build_fixtures(
            ship_name="QA P7 LongInput"
        )

    def test_overlong_name_is_rejected_not_500(self):
        payload = booking_payload(self.package, self.room_4p, customer_name="A" * 5000)
        resp = self.client.post("/api/bookings/", payload, format="json")
        # A 400 with a field error is correct; a 500 / DB DataError is not.
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("customer_name", resp.data)

    def test_overlong_phone_is_rejected_not_500(self):
        payload = booking_payload(self.package, self.room_4p, phone="0" * 5000)
        resp = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("phone", resp.data)

    def test_special_requests_field_is_persisted(self):
        """The wizard collects 'special requests'; assert it survives to the
        booking. (Reproduction: the field is dropped on the floor today.)"""
        payload = booking_payload(
            self.package, self.room_4p, special_requests="Wheelchair access please"
        )
        resp = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        booking = Booking.objects.get(booking_code=resp.data["booking_code"])
        self.assertTrue(
            hasattr(booking, "special_requests")
            and booking.special_requests == "Wheelchair access please",
            "special_requests was silently discarded — no model field / not read.",
        )
        # Also echoed back on the create response so the UI can confirm it.
        self.assertEqual(resp.data["special_requests"], "Wheelchair access please")

    def test_overlong_special_requests_rejected_not_500(self):
        """The one free-text field on the anonymous endpoint must be capped —
        not an unbounded row-size vector."""
        payload = booking_payload(
            self.package, self.room_4p, special_requests="x" * 5000
        )
        resp = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("special_requests", resp.data)

    def test_missing_special_requests_defaults_blank(self):
        """Omitting the field entirely is fine — it is optional."""
        payload = booking_payload(self.package, self.room_2p)
        resp = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        booking = Booking.objects.get(booking_code=resp.data["booking_code"])
        self.assertEqual(booking.special_requests, "")


class NameBoundaryTests(ThrottlelessTestMixin, APITestCase):
    """Exactly-at-the-limit name (100 chars) must be accepted; 101 rejected."""

    def setUp(self):
        (*_, self.room_2p, self.room_4p, self.package) = build_fixtures(
            ship_name="QA P7 Boundary"
        )

    def test_name_at_max_length_ok(self):
        resp = self.client.post(
            "/api/bookings/",
            booking_payload(self.package, self.room_4p, customer_name="B" * 100),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_name_one_over_max_rejected(self):
        resp = self.client.post(
            "/api/bookings/",
            booking_payload(self.package, self.room_4p, customer_name="B" * 101),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
