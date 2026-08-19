"""QA — full manual-equivalent end-to-end walk of the room-block workflow.

Drives the SAME HTTP endpoints the admin dashboard and the public site call,
in order, asserting the database and every API response at each step. This is
the automated stand-in for a human clicking through the two apps: there is no
browser in this environment, so "refresh the page" is modelled as re-issuing the
GET the page makes on load and asserting the state it would render is consistent
and never stale (the API is the single source of truth the UI has no cache ahead
of — React Query refetches these same URLs).

Each step prints a PASS line so the run reads like a manual test log.
"""

from datetime import date
from decimal import Decimal

from django.db import connections
from rest_framework.test import APITransactionTestCase

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom
from apps.packages.models import KidPricingRule, Package, PackageRoom
from apps.ships.models import Room, RoomType, Ship
from apps.testing import ThrottlelessTestMixin


def _p(step, msg):
    print(f"  [PASS] step {step:>2} — {msg}")


class RoomBlockE2ETests(ThrottlelessTestMixin, APITransactionTestCase):
    def setUp(self):
        # Base reference data (a ship, room types, kid-pricing) — the equivalent
        # of an already-seeded system the admin logs into.
        self.ship = Ship.objects.create(name="MV Alaska E2E")
        self.t2, _ = RoomType.objects.get_or_create(
            name="2-Person Room",
            defaults=dict(max_adults=2, max_kids=1, base_price=Decimal("2000.00")),
        )
        self.t4, _ = RoomType.objects.get_or_create(
            name="4-Person Room",
            defaults=dict(max_adults=4, max_kids=2, base_price=Decimal("3500.00")),
        )
        # Five cabins so we can block several, book several, and leave a margin.
        self.rooms = [
            Room.objects.create(
                ship=self.ship,
                room_type=self.t4 if i % 2 else self.t2,
                room_number=f"R{i}",
                floor_number=1,
            )
            for i in range(1, 6)
        ]
        for lo, hi, ctype, amt in [
            (0, 3, KidPricingRule.ChargeType.FREE, None),
            (3, 8, KidPricingRule.ChargeType.FIXED, Decimal("1500.00")),
            (8, 99, KidPricingRule.ChargeType.FULL_ADULT, None),
        ]:
            KidPricingRule.objects.get_or_create(
                min_age=lo, max_age=hi,
                defaults={"charge_type": ctype, "amount": amt},
            )
        self.admin = User.objects.create_user(
            username="e2eadmin", password="pass12345", is_staff=True
        )

    def tearDown(self):
        connections.close_all()

    # -- helpers ---------------------------------------------------------------
    def login(self):
        resp = self.client.post(
            "/api/staff/login/",
            {"username": "e2eadmin", "password": "pass12345"},
        )
        assert resp.status_code == 200, resp.content
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}")

    def logout(self):
        self.client.credentials()  # clear the auth header

    def staff_rooms(self, package_id):
        """The GET the staff room grid issues on every load / refresh."""
        r = self.client.get(f"/api/staff/packages/{package_id}/rooms/")
        assert r.status_code == 200, r.content
        return {row["room_number"]: row for row in r.data}

    def public_rooms(self, package_id):
        """The GET the public booking page issues on every load / refresh."""
        r = self.client.get(f"/api/packages/{package_id}/rooms/")
        assert r.status_code == 200, r.content
        return {row["room_number"]: row for row in r.data}

    # -- the walk --------------------------------------------------------------
    def test_full_workflow(self):
        print("\n== E2E: admin room-block full workflow ==")

        # STEP 1 — create a package (real staff CRUD endpoint) ────────────────
        self.login()
        resp = self.client.post(
            "/api/staff/packages/",
            {
                "ship": self.ship.id,
                "start_date": "2099-03-10",
                "end_date": "2099-03-12",
                "adult_price": "3000.00",
                "status": "draft",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        package_id = resp.data["id"]
        self.assertEqual(Package.objects.get(pk=package_id).status, "draft")
        _p(1, f"package #{package_id} created (status=draft)")

        # STEP 2 — attach rooms (generate-rooms pulls the ship's cabins) ──────
        resp = self.client.post(
            f"/api/staff/packages/{package_id}/generate-rooms/"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            PackageRoom.objects.filter(package_id=package_id).count(), 5
        )
        _p(2, "5 rooms attached via generate-rooms")

        # STEP 3 — publish / start the package (draft → open) ─────────────────
        resp = self.client.patch(
            f"/api/staff/packages/{package_id}/",
            {"status": "open"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Package.objects.get(pk=package_id).status, "open")
        # Refresh the grid (page reload): all 5 rooms read "available".
        grid = self.staff_rooms(package_id)
        self.assertTrue(all(r["availability"] == "available" for r in grid.values()))
        _p(3, "package published (open); all 5 rooms 'available' on reload")

        # STEP 4 — block multiple available rooms as admin (R1, R3) ───────────
        for rn, reason in [("R1", "Crew hold"), ("R3", "Maintenance")]:
            room = next(x for x in self.rooms if x.room_number == rn)
            resp = self.client.post(
                f"/api/staff/packages/{package_id}/block-room/",
                {"room_id": room.id, "reason": reason},
                format="json",
            )
            self.assertEqual(resp.status_code, 200, resp.content)
            self.assertEqual(resp.data["availability"], "blocked")
            self.assertEqual(resp.data["block_reason"], reason)
        self.assertEqual(
            PackageRoom.objects.filter(
                package_id=package_id, is_blocked=True
            ).count(),
            2,
        )
        _p(4, "R1 + R3 blocked (reasons stored, state=blocked)")

        # STEP 5 — users see blocked rooms as unavailable, reason never leaks ──
        pub = self.public_rooms(package_id)
        self.assertEqual(pub["R1"]["availability"], "booked")  # public label
        self.assertEqual(pub["R3"]["availability"], "booked")
        raw = self.client.get(f"/api/packages/{package_id}/rooms/").content.decode()
        self.assertNotIn("Crew hold", raw)
        self.assertNotIn("Maintenance", raw)
        self.assertNotIn("block_reason", raw)
        # Not-on-sale to a customer: exactly the 3 untouched rooms are available.
        avail_public = [rn for rn, r in pub.items() if r["availability"] == "available"]
        self.assertEqual(sorted(avail_public), ["R2", "R4", "R5"])
        _p(5, "public sees R1/R3 as not-for-sale; reason absent from payload")

        # STEP 6 — book several remaining rooms as a normal user (R2, R4) ─────
        self.logout()  # public booking needs no auth
        for rn in ("R2", "R4"):
            room = next(x for x in self.rooms if x.room_number == rn)
            resp = self.client.post(
                "/api/bookings/",
                {
                    "package_id": package_id,
                    "customer_name": f"Customer {rn}",
                    "phone": "01700000000",
                    "email": "cust@example.com",
                    "rooms": [
                        {"room_id": room.id, "adult_count": 2, "kid_details": []}
                    ],
                },
                format="json",
            )
            self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Booking.objects.filter(package_id=package_id).count(), 2)
        _p(6, "R2 + R4 booked by public user (2 bookings)")

        # A blocked room still refuses a booking even via a direct API call.
        blocked_room = next(x for x in self.rooms if x.room_number == "R1")
        resp = self.client.post(
            "/api/bookings/",
            {
                "package_id": package_id,
                "customer_name": "Sneaky",
                "phone": "01700000000",
                "email": "s@example.com",
                "rooms": [
                    {"room_id": blocked_room.id, "adult_count": 1, "kid_details": []}
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        _p(6, "direct booking of blocked R1 rejected (400) — no UI bypass")

        # STEP 7 — unblock one previously blocked room (R1) ───────────────────
        self.login()
        room1 = next(x for x in self.rooms if x.room_number == "R1")
        resp = self.client.post(
            f"/api/staff/packages/{package_id}/unblock-room/",
            {"room_id": room1.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["availability"], "available")
        self.assertFalse(
            PackageRoom.objects.get(package_id=package_id, room=room1).is_blocked
        )
        _p(7, "R1 unblocked (state cleared)")

        # STEP 8 — the room immediately becomes available (staff + public) ────
        self.assertEqual(self.staff_rooms(package_id)["R1"]["availability"], "available")
        self.logout()
        self.assertEqual(self.public_rooms(package_id)["R1"]["availability"], "available")
        _p(8, "R1 reads 'available' on both staff and public reload — no stale state")

        # STEP 9 — book the newly unblocked room (R1) ─────────────────────────
        resp = self.client.post(
            "/api/bookings/",
            {
                "package_id": package_id,
                "customer_name": "Customer R1",
                "phone": "01700000000",
                "email": "r1@example.com",
                "rooms": [
                    {"room_id": room1.id, "adult_count": 1, "kid_details": []}
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Booking.objects.filter(package_id=package_id).count(), 3)
        _p(9, "newly-unblocked R1 booked successfully (3 bookings total)")

        # STEP 10 — generate the PDF guide report (both scopes) ───────────────
        self.login()
        for scope in ("booked", "all"):
            r = self.client.get(
                f"/api/staff/packages/{package_id}/guide-report/",
                {"scope": scope} if scope == "all" else {},
            )
            self.assertEqual(r.status_code, 200, r.content)
            body = b"".join(r.streaming_content) if r.streaming else r.content
            self.assertTrue(body.startswith(b"%PDF"), f"scope={scope} not a PDF")
            self.assertGreater(len(body), 1000)
        _p(10, "guide PDF generated for both scopes (valid %PDF, non-trivial)")

        # STEP 11 — statistics match the database exactly ─────────────────────
        grid = self.staff_rooms(package_id)
        counts = {"available": 0, "booked": 0, "blocked": 0, "unavailable": 0}
        for r in grid.values():
            counts[r["availability"]] += 1
        total = len(grid)
        # DB truth:
        db_blocked = PackageRoom.objects.filter(
            package_id=package_id, is_blocked=True
        ).count()
        db_booked = BookingRoom.objects.filter(
            package_id=package_id, is_active=True
        ).count()
        db_total = PackageRoom.objects.filter(package_id=package_id).count()
        self.assertEqual(counts["booked"], db_booked, "booked count ≠ DB")
        self.assertEqual(counts["blocked"], db_blocked, "blocked count ≠ DB")
        self.assertEqual(total, db_total)
        # The invariant the dashboard tally relies on: the four states partition
        # the total with no gap and no overlap.
        self.assertEqual(
            counts["available"] + counts["booked"]
            + counts["blocked"] + counts["unavailable"],
            total,
            "room-state counts do not sum to total",
        )
        # Concretely: 3 booked (R1,R2,R4), 1 blocked (R3), 1 available (R5).
        self.assertEqual(counts["booked"], 3)
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(counts["available"], 1)
        self.assertEqual(counts["unavailable"], 0)
        _p(11, f"stats match DB: {counts} sum={total} (booked=3 blocked=1 avail=1)")

        # STEP 12 — dashboard counts (booking summary endpoint) ───────────────
        resp = self.client.get(
            "/api/staff/bookings/summary/", {"package": package_id}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.data["count"], 3)  # 3 active bookings on this package
        # Money is server-computed; total must be the sum of the 3 bookings' dues
        # + paid (nothing paid yet → all due).
        db_due = sum(
            (b.due_amount for b in Booking.objects.filter(package_id=package_id)),
            Decimal("0.00"),
        )
        self.assertEqual(Decimal(resp.data["due_amount"]), db_due)
        _p(12, f"dashboard summary: 3 bookings, due={resp.data['due_amount']} matches DB")

        # STEP 13 — room "status colors" are driven by the availability string ─
        # The UI maps availability→color (emerald/gold/indigo/muted). We can't
        # render pixels here, but the contract the colors derive from is the
        # availability value, which we assert is exactly one known token per room.
        valid = {"available", "booked", "blocked", "unavailable"}
        self.assertTrue(all(r["availability"] in valid for r in grid.values()))
        _p(13, "every room carries exactly one valid availability token "
                "(the value the UI color-maps)")

        # STEP 14 & 15 — refresh / multi-session / stale-cache consistency ────
        # Model each "refresh"/"new tab"/"new session" as an independent fresh
        # GET (no shared client cache) and assert identical state every time.
        snapshots = []
        for _ in range(4):  # 4 independent reloads / tabs / sessions
            self.login()  # a distinct admin session each iteration
            snap = {rn: r["availability"] for rn, r in self.staff_rooms(package_id).items()}
            snapshots.append(snap)
        self.assertTrue(all(s == snapshots[0] for s in snapshots),
                        "staff room state differs across reloads/sessions")
        # Admin + public simultaneously: public view is consistent with staff
        # (blocked/booked → not available to the customer).
        self.logout()
        pub = self.public_rooms(package_id)
        for rn, staff_state in snapshots[0].items():
            if staff_state in ("booked", "blocked"):
                self.assertEqual(pub[rn]["availability"], "booked")
            else:
                self.assertEqual(pub[rn]["availability"], staff_state)
        _p(14, "4 independent admin reloads/sessions return identical state")
        _p(15, "admin + public views mutually consistent; no stale/cache drift "
                "(API is single source of truth)")

        print("== E2E complete: 15/15 steps PASS ==\n")
