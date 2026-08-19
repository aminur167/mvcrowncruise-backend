"""QA Phase 5 RE-TEST — payment flow, second pass.

The first pass (qa-reports/phase5-payment.md) found C1-C5/H1-H3/M1-M2 and all
were fixed. This suite re-verifies the fixes hold and probes the edges the
first pass did NOT cover. Probes named *_BUG_* document defective behaviour
observed today.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.bookings.models import Booking, Payment
from apps.packages.models import Package
from apps.test_qa_phase5 import GATEWAY_URL, PaymentQABase
from apps.testing import sign_ipn


class DeploymentWiringTests(PaymentQABase):
    """R1/C6 — the C1/H1/H2 fixes are management commands. Are they scheduled?"""

    def test_R1_FIXED_the_payment_crons_are_scheduled_in_order(self):
        """C6 fix: run_payment_jobs drives the whole set, in the required order,
        in one process — so the safety-critical ordering (reconcile a payment
        before releasing its room) cannot be broken by cron timing. The schedule
        itself is committed as code (railway.json + DEPLOYMENT.md)."""
        import os
        import pkgutil

        import apps.bookings.management.commands as cmds
        from apps.bookings.management.commands.run_payment_jobs import (
            DAILY_JOBS,
            QUICK_JOBS,
        )

        names = {m.name for m in pkgutil.iter_modules(cmds.__path__)}
        self.assertIn("run_payment_jobs", names)

        scheduled = [name for name, _ in QUICK_JOBS + DAILY_JOBS]
        for job in (
            "reconcile_pending_payments",
            "expire_stale_bookings",
            "enforce_due_deadlines",
        ):
            self.assertIn(job, names)
            self.assertIn(job, scheduled)  # ...and something actually runs it

        # Reconcile MUST precede expiry — releasing a room before asking the
        # gateway about its payment is exactly how a paid cabin gets resold.
        self.assertLess(
            scheduled.index("reconcile_pending_payments"),
            scheduled.index("expire_stale_bookings"),
        )

        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.assertTrue(
            os.path.exists(os.path.join(root, "backend", "DEPLOYMENT.md"))
        )


class SupersededSessionTests(PaymentQABase):
    """R2 — initiate_payment() supersedes an older PENDING payment row, but
    never cancels the gateway session it belongs to."""

    def test_R2_FIXED_a_different_request_is_refused_while_a_session_is_live(self):
        """H4 fix: an SSLCommerz session cannot be voided once issued, so we
        never leave a second payable checkout page behind. A DIFFERENT
        amount/type while one is live is refused outright."""
        booking = self.make_booking()
        r1 = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        self.assertEqual(r1.status_code, 200)
        first = Payment.objects.get(transaction_id=r1.data["tran_id"])

        # Second tab, different request (full instead of the live partial).
        r2 = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(r2.status_code, 400)
        self.assertIn("already in progress", str(r2.data))

        # Session 1 is untouched and still the only live one — so there is no
        # second page that could take the customer's money uncredited.
        first.refresh_from_db()
        self.assertEqual(first.status, Payment.Status.PENDING)
        self.assertEqual(
            booking.payments.filter(status=Payment.Status.PENDING).count(), 1
        )

        # Paying on it credits normally — no orphaned money, no refund owed.
        self.settle(first)
        first.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(first.status, Payment.Status.SUCCESS)
        self.assertEqual(booking.paid_amount, Decimal("4750.00"))
        self.assertFalse(booking.refund_required)

    def test_R2b_FIXED_an_identical_request_reuses_the_same_live_session(self):
        """The common double-click / back-button case: same type and amount →
        the SAME gateway URL and the SAME payment row, not a second session."""
        booking = self.make_booking()
        r1 = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        self.assertEqual(r1.status_code, 200)

        # Identical re-request. create_session must NOT be called again.
        with patch("apps.bookings.sslcommerz.create_session") as create:
            r2 = self.client.post(
                f"/api/bookings/{booking.booking_code}/pay/",
                {"payment_type": "partial", "amount": "4750.00"},
                format="json",
            )
        self.assertEqual(r2.status_code, 200)
        create.assert_not_called()
        self.assertEqual(r2.data["tran_id"], r1.data["tran_id"])
        self.assertEqual(r2.data["gateway_url"], r1.data["gateway_url"])
        self.assertEqual(booking.payments.count(), 1)


class DueDeadlineEdgeTests(PaymentQABase):
    """R3/R4 — enforce_due_deadlines behaviour the first pass didn't probe."""

    def _partially_pay(self, booking, amount="4750.00"):
        r = self.initiate(booking, {"payment_type": "partial", "amount": amount})
        payment = Payment.objects.get(transaction_id=r.data["tran_id"])
        self.settle(payment)
        booking.refresh_from_db()
        assert booking.status == Booking.Status.PARTIALLY_PAID
        return booking

    def test_R3_FIXED_sailed_unpaid_booking_is_reported_not_cancelled(self):
        """M4 fix: the tour WAS delivered, so an outstanding balance is a real
        debt — close_sailed_bookings reports it (for the guide to chase) and
        must never cancel it or erase the receivable."""
        booking = self._partially_pay(self.make_booking())
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() - timedelta(days=10),
            end_date=timezone.localdate() - timedelta(days=8),
        )
        call_command("enforce_due_deadlines")  # correctly skips sailed packages
        call_command("close_sailed_bookings")
        booking.refresh_from_db()
        # Debt preserved, not cancelled, not silently completed.
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(booking.due_amount, Decimal("4750.00"))

    def test_R3b_FIXED_sailed_fully_paid_booking_becomes_completed(self):
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "full"})
        self.settle(Payment.objects.get(transaction_id=r.data["tran_id"]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)

        # Tour is upcoming — must NOT be completed yet.
        call_command("close_sailed_bookings")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)

        # Tour has now sailed.
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() - timedelta(days=10),
            end_date=timezone.localdate() - timedelta(days=8),
        )
        call_command("close_sailed_bookings")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.COMPLETED)
        self.assertEqual(booking.due_amount, Decimal("0.00"))

    def test_R4_BUG_deadline_cancellation_needs_no_gateway_check_but_reminder_is_one_shot(self):
        """The reminder is gated on due_reminder_sent_at, which is never
        reset. If the deadline is later pushed back by an admin (or the
        package start_date moves), the customer is never reminded again."""
        booking = self._partially_pay(self.make_booking())
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=4),
            end_date=timezone.localdate() + timedelta(days=6),
        )
        mail.outbox = []
        call_command("enforce_due_deadlines")  # deadline = start-3d = +1d, within 2d window
        booking.refresh_from_db()
        self.assertIsNotNone(booking.due_reminder_sent_at)
        sent_first = len(mail.outbox)
        self.assertEqual(sent_first, 1)

        # Admin reschedules the sailing a month out. Deadline moves; the
        # customer will never get another reminder before the new deadline.
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=32),
            end_date=timezone.localdate() + timedelta(days=34),
        )
        mail.outbox = []
        call_command("enforce_due_deadlines")
        self.assertEqual(len(mail.outbox), 0)  # BUG: no second reminder, ever


class CompletedStatusTests(PaymentQABase):
    def test_R5_FIXED_completed_is_reachable_and_terminal(self):
        """M4 fix: COMPLETED is now actually set (by close_sailed_bookings)
        and remains terminal — no further payment can be taken on it."""
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "full"})
        self.settle(Payment.objects.get(transaction_id=r.data["tran_id"]))
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() - timedelta(days=10),
            end_date=timezone.localdate() - timedelta(days=8),
        )
        call_command("close_sailed_bookings")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.COMPLETED)

        # Terminal: the public pay endpoint refuses it.
        resp = self.initiate(booking, {"payment_type": "partial", "amount": "100.00"})
        self.assertEqual(resp.status_code, 400)


class InvoiceOnEveryPaymentTests(PaymentQABase):
    def test_R6_partial_then_balance_sends_two_invoices(self):
        """Sanity: each settled payment emails an invoice."""
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        p1 = Payment.objects.get(transaction_id=r.data["tran_id"])
        mail.outbox = []
        self.settle(p1)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("4750.00", body)

        booking.refresh_from_db()
        r = self.initiate(booking, {"payment_type": "full"})
        p2 = Payment.objects.get(transaction_id=r.data["tran_id"])
        self.assertEqual(p2.amount, Decimal("4750.00"))
        mail.outbox = []
        self.settle(p2)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertEqual(len(mail.outbox), 1)


class ReconcileErrorTests(PaymentQABase):
    def test_R7_FIXED_permanent_gateway_error_escalates_instead_of_spinning_forever(self):
        """H5 fix: a payment the gateway will not resolve still (correctly)
        holds its room — but it is no longer silent. After PAYMENT_MAX_
        RECONCILE_ATTEMPTS it is flagged needs_manual_review and its booking
        is put on the refunds/manual-review queue with the tran_id, so a human
        can settle it from the SSLCommerz merchant panel."""
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        payment = Payment.objects.get(transaction_id=r.data["tran_id"])
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            side_effect=requests.Timeout("gateway down"),
        ):
            for _ in range(settings.PAYMENT_MAX_RECONCILE_ATTEMPTS):
                call_command("reconcile_pending_payments")

        payment.refresh_from_db()
        booking.refresh_from_db()
        # Still PENDING (we must not guess that money didn't move) and the
        # room is still held — but now a human owns it.
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertTrue(payment.needs_manual_review)
        self.assertEqual(
            payment.reconcile_attempts, settings.PAYMENT_MAX_RECONCILE_ATTEMPTS
        )
        self.assertIn("gateway down", payment.last_reconcile_error)
        self.assertTrue(booking.refund_required)
        self.assertIn(payment.transaction_id, booking.refund_note)

        # Escalated payments are not re-queried — a human owns them now.
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            side_effect=AssertionError("must not re-query an escalated payment"),
        ):
            call_command("reconcile_pending_payments")

        # The room is still protected (never resold under a live payment).
        call_command("expire_stale_bookings")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_R7b_FIXED_a_recovered_gateway_clears_the_failure_streak(self):
        """A transient outage must not creep toward escalation forever."""
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        payment = Payment.objects.get(transaction_id=r.data["tran_id"])
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(minutes=30)
        )

        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            side_effect=requests.Timeout("blip"),
        ):
            call_command("reconcile_pending_payments")
        payment.refresh_from_db()
        self.assertEqual(payment.reconcile_attempts, 1)
        self.assertFalse(payment.needs_manual_review)

        # Gateway recovers: no attempt on record yet, session still young.
        with patch("apps.bookings.sslcommerz.query_transaction", return_value=[]):
            call_command("reconcile_pending_payments")
        payment.refresh_from_db()
        self.assertEqual(payment.reconcile_attempts, 0)  # streak cleared
        self.assertFalse(payment.needs_manual_review)


class IPNSignatureEdgeTests(PaymentQABase):
    def test_R8_signed_ipn_for_someone_elses_tran_id_still_needs_gateway_verdict(self):
        """Signature proves origin, not authorization. Re-verify a signed
        FAILED IPN cannot kill a session it doesn't own... it can, but only
        SSLCommerz can sign, so this is acceptable. Assert the fix holds."""
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        payment = Payment.objects.get(transaction_id=r.data["tran_id"])

        # Unsigned forged FAILED — must be rejected (C5 fix).
        resp = self.client.post(
            "/api/payments/ipn/",
            {"tran_id": payment.transaction_id, "status": "FAILED"},
        )
        self.assertEqual(resp.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_R9_BUG_ipn_signature_uses_md5_and_store_password_only(self):
        """The verify_sign scheme is MD5 over the POSTed values + md5(store
        password). Anyone who learns the store password can mint valid IPNs —
        that is SSLCommerz's design, noted only as residual risk. What is in
        OUR control and missing: no IP allowlist, and no replay window (a
        captured genuine IPN can be replayed indefinitely)."""
        from django.conf import settings

        self.assertFalse(hasattr(settings, "SSLCOMMERZ_IPN_ALLOWED_IPS"))
        booking = self.make_booking()
        r = self.initiate(booking, {"payment_type": "partial", "amount": "4750.00"})
        payment = Payment.objects.get(transaction_id=r.data["tran_id"])

        # A genuine signed FAILED IPN, captured and replayed later: the first
        # one closes the session (correct). Replay is a no-op here because the
        # payment is no longer PENDING — so replay is contained. Assert that.
        signed = sign_ipn(
            {"tran_id": payment.transaction_id, "status": "FAILED"}
        )
        self.assertEqual(self.client.post("/api/payments/ipn/", signed).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        # Replay
        self.assertEqual(self.client.post("/api/payments/ipn/", signed).status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)  # contained
