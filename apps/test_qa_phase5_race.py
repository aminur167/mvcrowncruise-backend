"""QA Phase 5 — real-concurrency payment probes.

TransactionTestCase (not TestCase): the code under test relies on
select_for_update, which is a no-op inside TestCase's single wrapping
transaction. These run against real committed transactions in threads.
"""

import threading
from decimal import Decimal
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from apps.bookings.models import Booking, Payment
from apps.bookings.test_api import build_fixtures
from apps.testing import ThrottlelessTestMixin, create_booking, sign_ipn

GATEWAY_URL = "https://sandbox.sslcommerz.com/gwprocess/testsession"


class PaymentRaceTests(ThrottlelessTestMixin, TransactionTestCase):
    def setUp(self):
        (
            self.ship,
            self.type_2p,
            self.type_4p,
            self.room_2p,
            self.room_4p,
            self.package,
        ) = build_fixtures()

    def make_booking(self):
        # total 9500.00
        return create_booking(
            self.package,
            rooms=[{"room": self.room_4p, "adult_count": 2, "kid_details": []}],
        )

    def initiate(self, booking, payload):
        with patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            return APIClient().post(
                f"/api/bookings/{booking.booking_code}/pay/", payload, format="json"
            )

    def _run(self, fn, n):
        """Run fn(i) in n threads released simultaneously."""
        barrier = threading.Barrier(n)
        results, lock = [], threading.Lock()

        def worker(i):
            try:
                barrier.wait()
                out = fn(i)
                with lock:
                    results.append(out)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results

    # ── 6. Concurrent partial payment initiation ────────────────────────────
    def test_6_concurrent_partial_initiations_cannot_exceed_the_due(self):
        """Two tabs each start an identical 6000 partial on a 9500 booking at
        the same instant. initiate_payment() takes a row lock, so the second
        finds the first's live session and REUSES it (QA H4) — exactly one
        Payment row, exactly one payable checkout page, no orphaned session
        left live at the gateway."""
        b = self.make_booking()

        def go(i):
            r = self.initiate(b, {"payment_type": "partial", "amount": "6000.00"})
            return r.status_code

        codes = self._run(go, 2)
        self.assertEqual(sorted(codes), [200, 200])  # both accepted (each <= due)

        # One row, PENDING, and nothing cancelled — the loser reused it.
        self.assertEqual(Payment.objects.filter(booking=b).count(), 1)
        self.assertEqual(
            Payment.objects.filter(
                booking=b, status=Payment.Status.PENDING
            ).count(),
            1,
        )
        self.assertEqual(
            Payment.objects.filter(
                booking=b, status=Payment.Status.CANCELLED
            ).count(),
            0,
        )

    # ── 6/9. Concurrent settlement of two payments on one booking ───────────
    def test_6b_concurrent_settlement_of_two_payments_sums_correctly(self):
        """Force two SUCCESS-able payments to exist (bypassing the supersede
        guard by creating them directly, as the gateway could still settle a
        superseded-then-reinstated row), then settle both at the same instant.
        refresh_paid_amount() locks the booking row and re-SUMs, so no credit
        can be lost."""
        b = self.make_booking()
        p1 = Payment.objects.create(
            booking=b,
            amount=Decimal("3000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            transaction_id=f"{b.booking_code}-PA",
        )
        p2 = Payment.objects.create(
            booking=b,
            amount=Decimal("2500.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            transaction_id=f"{b.booking_code}-PB",
        )

        # One mock for BOTH threads, keyed by val_id — a per-thread `patch()`
        # context manager would be un-patched by whichever thread exits first
        # and let the other issue a real gateway call.
        verdicts = {
            f"VAL{i}": {
                "status": "VALID",
                "tran_id": p.transaction_id,
                "val_id": f"VAL{i}",
                "amount": str(p.amount),
                "currency": "BDT",
            }
            for i, p in enumerate([p1, p2])
        }

        def go(i):
            p = [p1, p2][i]
            return APIClient().post(
                "/api/payments/ipn/",
                sign_ipn({"tran_id": p.transaction_id, "val_id": f"VAL{i}"}),
            ).status_code

        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            side_effect=lambda val_id: verdicts[val_id],
        ):
            self._run(go, 2)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("5500.00"))  # 3000 + 2500, none lost
        self.assertEqual(b.due_amount, Decimal("4000.00"))
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)

    # ── 9. Concurrent duplicate webhook for the SAME payment ────────────────
    def test_9_concurrent_duplicate_ipn_credits_exactly_once(self):
        """Same tran_id/val_id delivered 4x simultaneously (gateway retry
        storm). The select_for_update + SUCCESS gate must credit once."""
        b = self.make_booking()
        r = self.initiate(b, {"payment_type": "partial", "amount": "5000.00"})
        p = Payment.objects.get(transaction_id=r.data["tran_id"])
        data = {
            "status": "VALID",
            "tran_id": p.transaction_id,
            "val_id": "VAL1",
            "amount": "5000.00",
            "currency": "BDT",
        }

        def go(i):
            with patch(
                "apps.bookings.sslcommerz.validate_payment", return_value=data
            ):
                return APIClient().post(
                    "/api/payments/ipn/",
                    sign_ipn({"tran_id": p.transaction_id, "val_id": "VAL1"}),
                ).status_code

        self._run(go, 4)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("5000.00"))  # not 20000
        self.assertEqual(b.due_amount, Decimal("4500.00"))  # not -10500
        self.assertEqual(
            Payment.objects.filter(booking=b, status=Payment.Status.SUCCESS).count(), 1
        )

    def test_9b_concurrent_duplicate_ipn_full_payment_credits_exactly_once(self):
        b = self.make_booking()
        r = self.initiate(b, {"payment_type": "full"})
        p = Payment.objects.get(transaction_id=r.data["tran_id"])
        data = {
            "status": "VALID",
            "tran_id": p.transaction_id,
            "val_id": "VAL1",
            "amount": "9500.00",
            "currency": "BDT",
        }

        def go(i):
            with patch(
                "apps.bookings.sslcommerz.validate_payment", return_value=data
            ):
                return APIClient().post(
                    "/api/payments/ipn/",
                    sign_ipn({"tran_id": p.transaction_id, "val_id": "VAL1"}),
                ).status_code

        self._run(go, 4)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("9500.00"))
        self.assertEqual(b.due_amount, Decimal("0.00"))
        self.assertEqual(b.status, Booking.Status.FULLY_PAID)
        self.assertEqual(
            Payment.objects.filter(booking=b, status=Payment.Status.SUCCESS).count(), 1
        )
        # And exactly one invoice, so the customer isn't emailed 4 times.
        self.assertEqual(b.invoices.count(), 1)

    # ── 9. Duplicate tran_id can never mint a second Payment row ────────────
    def test_9c_duplicate_transaction_id_is_blocked_at_the_db(self):
        from django.db.utils import IntegrityError

        b = self.make_booking()
        Payment.objects.create(
            booking=b,
            amount=Decimal("5000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            transaction_id=f"{b.booking_code}-DUP",
        )
        with self.assertRaises(IntegrityError):
            Payment.objects.create(
                booking=b,
                amount=Decimal("5000.00"),
                payment_type=Payment.PaymentType.PARTIAL,
                transaction_id=f"{b.booking_code}-DUP",
            )
        connection.close()

    # ── 7. Concurrent expiry cron vs. a settling payment ────────────────────
    def test_7_expiry_cron_racing_a_settling_payment_never_zeroes_the_money(self):
        """The cron scans, then an IPN settles, then the cron writes. The
        re-check under the row lock must abort the cancel. Since the C1 fix
        the guard is stronger still: ANY unresolved PENDING payment spares
        the booking (however old), so the cron can never release this room
        while the payment is in flight."""
        from django.core.management import call_command
        from django.utils import timezone
        from datetime import timedelta

        b = self.make_booking()
        r = self.initiate(b, {"payment_type": "partial", "amount": "5000.00"})
        p = Payment.objects.get(transaction_id=r.data["tran_id"])
        # Age both so the cron would otherwise pick this up.
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        Payment.objects.filter(pk=p.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        data = {
            "status": "VALID",
            "tran_id": p.transaction_id,
            "val_id": "VAL1",
            "amount": "5000.00",
            "currency": "BDT",
        }

        def go(i):
            if i == 0:
                call_command("expire_stale_bookings", verbosity=0)
                return "cron"
            with patch(
                "apps.bookings.sslcommerz.validate_payment", return_value=data
            ):
                APIClient().post(
                    "/api/payments/ipn/",
                    sign_ipn({"tran_id": p.transaction_id, "val_id": "VAL1"}),
                )
            return "ipn"

        self._run(go, 2)
        b.refresh_from_db()
        p.refresh_from_db()
        # Whatever the interleaving, the room was never released...
        self.assertNotEqual(b.status, Booking.Status.CANCELLED)
        # ...and the settled money is all accounted for.
        self.assertEqual(p.status, Payment.Status.SUCCESS)
        self.assertEqual(b.paid_amount, Decimal("5000.00"))
        self.assertEqual(b.due_amount, Decimal("4500.00"))
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
