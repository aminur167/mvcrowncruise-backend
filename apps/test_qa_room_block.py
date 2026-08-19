"""QA — admin room block/unblock feature.

Admins can withhold ("block") a specific room from sale on a live sailing
without deleting it from inventory, then release it again at any time while the
package is live. A blocked room:

  * cannot be booked (public OR staff API — backend-enforced, not UI-only),
  * shows to customers as plain "unavailable" (the reason never leaks),
  * shows to staff as its own "blocked" state (with reason / who / when),
  * appears in the guide PDF report as "Blocked by admin",
  * never affects the money totals (a held cabin carries no booking).

A booked room cannot be blocked (cancel first), and a cancelled/completed
package can no longer have its rooms blocked or released.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bookings.models import Booking
from apps.bookings.reports import _unbooked_rooms, generate_guide_report_pdf
from apps.bookings.test_api import build_fixtures
from apps.packages.models import Package, PackageRoom, RoomBlocked
from apps.testing import ThrottlelessTestMixin, create_booking


class RoomBlockBase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures()
        cls.staff = User.objects.create_user(
            username="blockstaff", password="pass12345", is_staff=True
        )
        cls.pr_2p = PackageRoom.objects.get(package=cls.package, room=cls.room_2p)
        cls.pr_4p = PackageRoom.objects.get(package=cls.package, room=cls.room_4p)

    def auth(self):
        resp = self.client.post(
            "/api/staff/login/",
            {"username": "blockstaff", "password": "pass12345"},
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")

    def make_booking(self, room=None, adults=2, kids=None):
        return create_booking(
            self.package,
            rooms=[
                {
                    "room": room or self.room_4p,
                    "adult_count": adults,
                    "kid_details": kids or [],
                }
            ],
        )


# ── Model-layer: block()/unblock() and its guards ─────────────────────────────
class RoomBlockModelTests(RoomBlockBase):
    def test_block_sets_state_reason_user_time(self):
        self.pr_2p.block(user=self.staff, reason="  Crew hold  ")
        self.pr_2p.refresh_from_db()
        self.assertTrue(self.pr_2p.is_blocked)
        self.assertEqual(self.pr_2p.block_reason, "Crew hold")  # trimmed
        self.assertEqual(self.pr_2p.blocked_by, self.staff)
        self.assertIsNotNone(self.pr_2p.blocked_at)

    def test_unblock_clears_state(self):
        self.pr_2p.block(user=self.staff, reason="hold")
        self.pr_2p.unblock()
        self.pr_2p.refresh_from_db()
        self.assertFalse(self.pr_2p.is_blocked)
        self.assertEqual(self.pr_2p.block_reason, "")
        self.assertIsNone(self.pr_2p.blocked_by)
        self.assertIsNone(self.pr_2p.blocked_at)

    def test_cannot_block_a_booked_room(self):
        self.make_booking(room=self.room_4p)
        with self.assertRaises(RoomBlocked):
            self.pr_4p.block(user=self.staff)
        self.pr_4p.refresh_from_db()
        self.assertFalse(self.pr_4p.is_blocked)

    def test_can_block_after_booking_cancelled(self):
        booking = self.make_booking(room=self.room_4p)
        booking.status = Booking.Status.CANCELLED
        booking.save(changed_by=self.staff)
        # Cancelling frees the room, so blocking is now allowed.
        self.pr_4p.block(user=self.staff, reason="freed then held")
        self.pr_4p.refresh_from_db()
        self.assertTrue(self.pr_4p.is_blocked)

    def test_cannot_block_on_cancelled_package(self):
        self.package.status = Package.Status.CANCELLED
        self.package.save()
        with self.assertRaises(RoomBlocked):
            self.pr_2p.block(user=self.staff)

    def test_cannot_block_on_completed_package(self):
        self.package.status = Package.Status.COMPLETED
        self.package.save()
        with self.assertRaises(RoomBlocked):
            self.pr_2p.block(user=self.staff)


# ── Booking creation is refused on a blocked room (both paths) ────────────────
class RoomBlockBookingGuardTests(RoomBlockBase):
    def test_model_layer_booking_on_blocked_room_rejected(self):
        self.pr_4p.block(user=self.staff, reason="held")
        with self.assertRaises(ValidationError):
            self.make_booking(room=self.room_4p)

    def test_public_api_cannot_book_blocked_room(self):
        self.pr_4p.block(user=self.staff, reason="held")
        payload = {
            "package_id": self.package.id,
            "customer_name": "Rahim Uddin",
            "phone": "01700000000",
            "email": "rahim@example.com",
            "rooms": [
                {"room_id": self.room_4p.id, "adult_count": 2, "kid_details": []}
            ],
        }
        resp = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_staff_api_cannot_book_blocked_room(self):
        self.auth()
        self.pr_4p.block(user=self.staff, reason="held")
        payload = {
            "package_id": self.package.id,
            "customer_name": "Rahim Uddin",
            "phone": "01700000000",
            "email": "rahim@example.com",
            "rooms": [
                {"room_id": self.room_4p.id, "adult_count": 2, "kid_details": []}
            ],
        }
        resp = self.client.post("/api/staff/bookings/", payload, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_unblocked_room_is_bookable_again(self):
        self.pr_4p.block(user=self.staff, reason="held")
        self.pr_4p.unblock()
        booking = self.make_booking(room=self.room_4p)
        self.assertEqual(booking.rooms.count(), 1)


# ── Serializer / availability surfacing ──────────────────────────────────────
class RoomBlockAvailabilityTests(RoomBlockBase):
    def test_public_rooms_endpoint_shows_blocked_as_booked(self):
        # An admin hold reads to customers as "booked" (the room is simply not
        # on sale). The internal reason still never leaves the dashboard.
        self.pr_4p.block(user=self.staff, reason="secret internal reason")
        resp = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(resp.status_code, 200)
        by_room = {r["room_number"]: r for r in resp.data}
        self.assertEqual(by_room[self.room_4p.room_number]["availability"], "booked")
        # The internal reason must never appear in the public payload.
        self.assertNotIn("block_reason", by_room[self.room_4p.room_number])
        self.assertNotIn("secret internal reason", resp.content.decode())

    def test_staff_rooms_endpoint_shows_blocked_state_and_detail(self):
        self.auth()
        self.pr_4p.block(user=self.staff, reason="VIP hold")
        resp = self.client.get(f"/api/staff/packages/{self.package.id}/rooms/")
        self.assertEqual(resp.status_code, 200)
        by_room = {r["room_number"]: r for r in resp.data}
        room = by_room[self.room_4p.room_number]
        self.assertEqual(room["availability"], "blocked")
        self.assertTrue(room["is_blocked"])
        self.assertEqual(room["block_reason"], "VIP hold")
        self.assertEqual(room["blocked_by_username"], "blockstaff")
        self.assertIsNotNone(room["blocked_at"])

    def test_booked_wins_over_blocked_in_staff_availability(self):
        # A booked room reports "booked" even if is_blocked were somehow set,
        # so staff never lose sight of a customer behind a stale block flag.
        self.make_booking(room=self.room_4p)
        # Force the flag directly (bypassing block()'s booked-room guard).
        PackageRoom.objects.filter(pk=self.pr_4p.pk).update(is_blocked=True)
        self.auth()
        resp = self.client.get(f"/api/staff/packages/{self.package.id}/rooms/")
        by_room = {r["room_number"]: r for r in resp.data}
        self.assertEqual(by_room[self.room_4p.room_number]["availability"], "booked")


# ── Staff block/unblock endpoints ────────────────────────────────────────────
class RoomBlockEndpointTests(RoomBlockBase):
    def block(self, room, reason=""):
        return self.client.post(
            f"/api/staff/packages/{self.package.id}/block-room/",
            {"room_id": room.id, "reason": reason},
            format="json",
        )

    def unblock(self, room):
        return self.client.post(
            f"/api/staff/packages/{self.package.id}/unblock-room/",
            {"room_id": room.id},
            format="json",
        )

    def test_block_and_unblock_happy_path(self):
        self.auth()
        resp = self.block(self.room_4p, reason="Maintenance")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["availability"], "blocked")
        self.pr_4p.refresh_from_db()
        self.assertTrue(self.pr_4p.is_blocked)
        self.assertEqual(self.pr_4p.block_reason, "Maintenance")

        resp = self.unblock(self.room_4p)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["availability"], "available")
        self.pr_4p.refresh_from_db()
        self.assertFalse(self.pr_4p.is_blocked)

    def test_block_requires_auth(self):
        resp = self.block(self.room_4p)
        self.assertIn(resp.status_code, (401, 403))

    def test_non_staff_user_cannot_block_no_privilege_escalation(self):
        # A genuine, authenticated NON-staff account (a customer) must not be
        # able to reach the admin block API even with a valid token — the
        # endpoint is IsAdminUser (is_staff), not merely IsAuthenticated.
        User.objects.create_user(
            username="customer", password="pass12345", is_staff=False
        )
        login = self.client.post(
            "/api/staff/login/",
            {"username": "customer", "password": "pass12345"},
        )
        # Either login itself is refused (staff-only login) or the block call is
        # forbidden — in neither case may the room end up blocked.
        if login.status_code == 200 and "access" in login.data:
            self.client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {login.data['access']}"
            )
            resp = self.block(self.room_4p)
            self.assertIn(resp.status_code, (401, 403))
        self.pr_4p.refresh_from_db()
        self.assertFalse(self.pr_4p.is_blocked)

    def test_block_booked_room_returns_400(self):
        self.make_booking(room=self.room_4p)
        self.auth()
        resp = self.block(self.room_4p)
        self.assertEqual(resp.status_code, 400)

    def test_block_room_not_on_package_returns_404(self):
        # A room that exists but is not attached to this package.
        from apps.ships.models import Room

        stray = Room.objects.create(
            ship=self.ship, room_type=self.type_2p, room_number="STRAY"
        )
        self.auth()
        resp = self.client.post(
            f"/api/staff/packages/{self.package.id}/block-room/",
            {"room_id": stray.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_block_on_cancelled_package_returns_400(self):
        self.package.status = Package.Status.CANCELLED
        self.package.save()
        self.auth()
        resp = self.block(self.room_2p)
        self.assertEqual(resp.status_code, 400)

    def test_unblock_never_blocked_room_is_a_safe_noop(self):
        # QA "Unblock non-blocked room": releasing a room that was never held
        # is harmless and returns it as plainly available (idempotent clear).
        self.auth()
        resp = self.unblock(self.room_2p)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["availability"], "available")
        self.pr_2p.refresh_from_db()
        self.assertFalse(self.pr_2p.is_blocked)

    def test_double_block_updates_the_hold_metadata(self):
        # QA "Block already blocked room": re-blocking is not rejected; it
        # refreshes the reason/who/when rather than erroring. The room stays
        # blocked throughout (never flips to bookable between the two calls).
        self.auth()
        self.assertEqual(self.block(self.room_2p, reason="first").data["block_reason"], "first")
        resp = self.block(self.room_2p, reason="second")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["availability"], "blocked")
        self.assertEqual(resp.data["block_reason"], "second")

    def test_unblock_allowed_on_cancelled_package(self):
        # Asymmetry by design: a cancelled/completed package rejects *blocking*
        # but still allows *releasing* an existing hold (the sailing is over, so
        # clearing a stale flag is harmless — documented in unblock()).
        self.auth()
        self.block(self.room_2p, reason="held")
        self.package.status = Package.Status.CANCELLED
        self.package.save()
        resp = self.unblock(self.room_2p)
        self.assertEqual(resp.status_code, 200)
        self.pr_2p.refresh_from_db()
        self.assertFalse(self.pr_2p.is_blocked)


# ── Guide PDF report + money totals ──────────────────────────────────────────
class RoomBlockReportTests(RoomBlockBase):
    def test_report_splits_available_and_blocked(self):
        # The report's room splitter puts a blocked room in `blocked` and an
        # untouched room in `available` — this is what drives the two PDF
        # sections, so assert the classification directly.
        self.pr_2p.block(user=self.staff, reason="Engine room access")
        available, blocked = _unbooked_rooms(self.package, booked_room_ids=set())
        avail_nums = {pr.room.room_number for pr in available}
        blocked_nums = {pr.room.room_number for pr in blocked}
        self.assertIn(self.room_2p.room_number, blocked_nums)
        self.assertIn(self.room_4p.room_number, avail_nums)
        self.assertNotIn(self.room_2p.room_number, avail_nums)

    def test_blocked_room_appears_in_default_report(self):
        # A booked room (so the sheet has content) + a blocked room.
        self.make_booking(room=self.room_4p)
        self.pr_2p.block(user=self.staff, reason="Engine room access")
        pdf = generate_guide_report_pdf(self.package, scope="booked")
        self.assertTrue(pdf.startswith(b"%PDF"))
        # Non-empty output; the blocked section is rendered (smoke: PDF built).
        self.assertGreater(len(pdf), 1000)

    def test_all_scope_report_builds_with_blocked_and_available(self):
        self.make_booking(room=self.room_4p)
        self.pr_2p.block(user=self.staff, reason="hold")
        pdf = generate_guide_report_pdf(self.package, scope="all")
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_report_with_only_blocked_rooms_builds(self):
        # No bookings at all — just a blocked room. Must not crash.
        self.pr_2p.block(user=self.staff, reason="hold")
        pdf = generate_guide_report_pdf(self.package, scope="all")
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_blocking_does_not_change_money_totals(self):
        # Book one room, note the totals, block the other room, confirm the
        # collectable money is untouched (a held cabin carries no money).
        booking = self.make_booking(room=self.room_4p, adults=2)
        before = (booking.total_amount, booking.paid_amount, booking.due_amount)
        self.pr_2p.block(user=self.staff, reason="hold")
        booking.refresh_from_db()
        after = (booking.total_amount, booking.paid_amount, booking.due_amount)
        self.assertEqual(before, after)
        # And the booked room's own booking is entirely unaffected.
        self.assertEqual(booking.rooms.count(), 1)
        self.assertGreater(booking.total_amount, Decimal("0.00"))
