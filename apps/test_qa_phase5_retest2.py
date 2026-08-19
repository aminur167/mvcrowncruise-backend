"""QA Phase 5 RE-TEST, part 2 — policy vs. behaviour (item 13d / 14)."""

from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.bookings.models import Booking, Payment
from apps.packages.models import Package
from apps.test_qa_phase5 import PaymentQABase


class PolicyContradictionTests(PaymentQABase):
    def _deposit(self, booking, amount="4750.00"):
        r = self.initiate(booking, {"payment_type": "partial", "amount": amount})
        self.settle(Payment.objects.get(transaction_id=r.data["tran_id"]))
        booking.refresh_from_db()
        return booking

    def test_R10_FIXED_customer_is_not_auto_cancelled_before_the_journey(self):
        """Published policy (policy.tsx + every invoice): 'the remaining balance
        may be settled any time BEFORE THE JOURNEY.' Client's H6 ruling: keep
        that policy — never auto-cancel a deposit-paid booking, and let the
        guide collect any balance on board.

        So a customer who has paid the 50% deposit is NOT cancelled as the
        balance deadline passes: the booking stands, the cabin stays reserved,
        and the deposit is untouched.
        """
        booking = self._deposit(self.make_booking())
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(booking.due_amount, Decimal("4750.00"))

        # Departure is 2 days away → the old deadline (noon, start-3d) has
        # passed. Under the previous behaviour this cancelled the booking.
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=2),
            end_date=timezone.localdate() + timedelta(days=4),
        )
        self.package.refresh_from_db()
        self.assertLess(self.package.balance_due_at(), timezone.now())

        call_command("enforce_due_deadlines")
        booking.refresh_from_db()

        # Still reserved — the guide will collect the 4750 balance on board.
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertFalse(booking.refund_required)
        self.assertEqual(booking.paid_amount, Decimal("4750.00"))
        self.assertEqual(booking.due_amount, Decimal("4750.00"))

        # The cabin is NOT on public sale — the customer still holds it.
        resp = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.pk,
                "rooms": [
                    {"room_id": self.room_4p.pk, "adult_count": 2, "kid_details": []}
                ],
                "customer_name": "Walk In",
                "phone": "01799999999", "email": "w@e.com",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409)  # room unavailable — still held

    def test_R11_FIXED_the_deadline_cron_only_reminds_it_never_cancels(self):
        """H6 ruling: the deadline job sends ONE balance reminder and never
        cancels. No cancellation email is sent from this path, because no
        cancellation happens."""
        booking = self._deposit(self.make_booking())
        # Departure 4 days out → inside the reminder window, before the soft
        # deadline, so a reminder is due.
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=4),
            end_date=timezone.localdate() + timedelta(days=6),
            balance_due_days_before_start=3,
        )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            call_command("enforce_due_deadlines")
        booking.refresh_from_db()

        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [booking.email])
        self.assertIn("balance", email.subject.lower())
        self.assertNotIn("cancelled", email.subject.lower())
        self.assertIn("4750.00", email.body)   # the outstanding balance
        self.assertIn("guide", email.body.lower())  # payable on board

    def test_R12_FIXED_admin_cancel_of_a_paid_booking_emails_the_customer(self):
        """Same coverage on the staff path — the fix lives in Booking.save(),
        so every cancel path gets it without opting in."""
        booking = self._deposit(self.make_booking())
        self.client.force_authenticate(self.staff)
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.patch(
                f"/api/staff/bookings/{booking.pk}/",
                {"status": "cancelled"}, format="json",
            )
        self.assertEqual(resp.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertTrue(booking.refund_required)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("cancelled", mail.outbox[0].subject.lower())
        self.assertIn("4750.00", mail.outbox[0].body)
        self.client.force_authenticate(None)

    def test_R12b_FIXED_cancelling_an_unpaid_booking_says_no_payment_received(self):
        """No deposit → no refund conversation to promise."""
        booking = self.make_booking()
        self.client.force_authenticate(self.staff)
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                f"/api/staff/bookings/{booking.pk}/",
                {"status": "cancelled"}, format="json",
            )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("No payment was received", mail.outbox[0].body)
        self.client.force_authenticate(None)

    def test_R13_deadline_cron_never_cancels_so_no_charge_is_needed(self):
        """Under the H6 ruling the deadline job never cancels a deposit-paid
        booking, so the 7-tier cancellation-charge schedule simply does not
        come into play here at all: the booking stands and the guide collects
        the balance on board. (The published schedule still governs a customer
        who ASKS to cancel — that stays a manual staff process.)"""
        booking = self._deposit(self.make_booking())
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=2),
            end_date=timezone.localdate() + timedelta(days=4),
        )
        call_command("enforce_due_deadlines")
        booking.refresh_from_db()
        # No cancellation happened, so no charge/forfeit was computed and the
        # deposit is untouched.
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertFalse(booking.refund_required)
        self.assertEqual(booking.refund_note, "")
