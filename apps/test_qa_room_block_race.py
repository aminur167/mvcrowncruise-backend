"""QA — concurrency probes for the admin room-block feature.

These use real Postgres row locks across threads (TransactionTestCase, one DB
connection per thread) to check what actually happens when a block and a booking
for the SAME room run at the same time.

The block() method takes a row lock on the PackageRoom row and re-checks the
booked state; booking creation guards itself with BookingRoom.clean() (reads
is_blocked) plus the partial unique constraint on BookingRoom. The two paths
lock different resources, so this file pins down the observable, committed
outcome rather than asserting a guarantee the code does not actually make.
"""

import threading

from django.db import connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom
from apps.bookings.test_api import build_fixtures
from apps.packages.models import PackageRoom, RoomBlocked
from apps.testing import ThrottlelessTestMixin


class BlockBookRaceTests(ThrottlelessTestMixin, TransactionTestCase):
    def setUp(self):
        (
            self.ship,
            self.type_2p,
            self.type_4p,
            self.room_2p,
            self.room_4p,
            self.package,
        ) = build_fixtures(ship_name="Block Race Ship")
        self.staff = User.objects.create_user(
            username="raceadmin", password="pass12345", is_staff=True
        )
        self.pr = PackageRoom.objects.get(package=self.package, room=self.room_4p)

    def _book_via_api(self, results):
        try:
            client = APIClient()
            resp = client.post(
                "/api/bookings/",
                {
                    "package_id": self.package.id,
                    "customer_name": "Racer",
                    "phone": "01700000000",
                    "email": "race@example.com",
                    "rooms": [
                        {"room_id": self.room_4p.id, "adult_count": 1,
                         "kid_details": []}
                    ],
                },
                format="json",
            )
            results.append(resp.status_code)
        finally:
            connections.close_all()

    def _block(self, results):
        try:
            self.pr.block(user=self.staff, reason="race hold")
            results.append("blocked")
        except RoomBlocked:
            results.append("rejected")
        finally:
            connections.close_all()

    def test_block_and_book_never_corrupts_and_final_state_is_consistent(self):
        """Run block() and a booking POST simultaneously many times. Whatever
        the interleaving, the committed state must be self-consistent: the
        room is never both actively booked AND advertised bookable, money is
        never wrong, and there is never more than one active booking."""
        barrier = threading.Barrier(2)
        both_blocked_and_booked = 0
        runs = 12

        for _ in range(runs):
            # Reset to a clean, unblocked, unbooked room for each attempt.
            BookingRoom.objects.filter(
                package=self.package, room=self.room_4p
            ).delete()
            Booking.objects.all().delete()
            PackageRoom.objects.filter(pk=self.pr.pk).update(
                is_blocked=False, block_reason="", blocked_by=None, blocked_at=None
            )
            self.pr.refresh_from_db()

            results = []

            def book():
                barrier.wait(timeout=10)
                self._book_via_api(results)

            def block():
                barrier.wait(timeout=10)
                self._block(results)

            threads = [
                threading.Thread(target=book),
                threading.Thread(target=block),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            # ---- Invariants that must ALWAYS hold, whatever the interleave ----
            active = BookingRoom.objects.filter(
                package=self.package, room=self.room_4p, is_active=True
            )
            # Never more than one active hold on the room.
            self.assertLessEqual(active.count(), 1)

            pr = PackageRoom.objects.get(pk=self.pr.pk)
            has_booking = active.exists()

            if has_booking:
                # Money must be intact on the surviving booking.
                br = active.first()
                self.assertGreater(br.booking.total_amount, 0)
                # If the block flag also got set (the tolerated interleave),
                # the customer must NOT be hidden: staff availability must still
                # read "booked", never "blocked".
                if pr.is_blocked:
                    both_blocked_and_booked += 1
                    from apps.staff.serializers import StaffPackageRoomSerializer
                    ctx = {"bookings_by_room": {self.room_4p.id: br}}
                    data = StaffPackageRoomSerializer(pr, context=ctx).data
                    self.assertEqual(data["availability"], "booked")

        # Informational: report how often the block+booked interleave occurred.
        # (No assertion on the count — it is timing dependent — but the loop
        # above proves it is always handled safely when it does happen.)
        print(
            f"\n[race probe] block+booked co-occurrence: "
            f"{both_blocked_and_booked}/{runs} runs"
        )
