"""QA Phase 5 THIRD PASS — payment flow.

Passes 1 (qa-reports/phase5-payment.md) and 2 (phase5-payment-retest.md) found
C1-C5/H1-H3/M1-M2 and C6/H4-H6/M3-M4. H4, H5, M3 and M4 have since been fixed
in code. This suite re-checks C6 (still open?) and probes the edges NEITHER
earlier pass covered: re-pricing under a live session, staff-payment races,
the min-deposit floor vs. the ceiling, COMPLETED/cancelled state machine holes,
and the reconcile/expiry interaction with escalated payments.

Probes named *_BUG_* assert defective behaviour observed today.
"""

import os
import pkgutil
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.conf import settings
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.bookings.models import Booking, Payment
from apps.bookings import payment_service
from apps.packages.models import Package
from apps.test_qa_phase5 import GATEWAY_URL, PaymentQABase
from apps.testing import sign_ipn


class SchedulerTests(PaymentQABase):
    """C6 — the payment crons must actually be scheduled, in the right order."""

    def test_T1_FIXED_the_payment_jobs_are_scheduled_and_ordered(self):
        """C6 fix: run_payment_jobs runs the whole set IN ORDER in one process,
        so the safety-critical ordering (reconcile a payment before releasing
        its room) cannot be broken by cron timing. railway.json + DEPLOYMENT.md
        commit the schedule as code."""
        from apps.bookings.management.commands.run_payment_jobs import (
            DAILY_JOBS,
            QUICK_JOBS,
        )

        # Reconcile MUST precede expiry: releasing a room before asking the
        # gateway about its payment is exactly how a paid cabin gets resold.
        self.assertEqual(
            [name for name, _ in QUICK_JOBS],
            ["reconcile_pending_payments", "expire_stale_bookings"],
        )
        self.assertEqual(
            [name for name, _ in DAILY_JOBS],
            [
                "enforce_due_deadlines",
                "close_sailed_bookings",
                "send_unsent_invoices",
            ],
        )

        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.assertTrue(
            os.path.exists(os.path.join(root, "backend", "DEPLOYMENT.md")),
            "the cron schedule must be committed as code, not tribal knowledge",
        )

    def test_T1b_FIXED_run_payment_jobs_executes_every_job(self):
        """The wrapper really drives all five commands."""
        booking = self.make_booking()
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        with patch(
            "apps.bookings.sslcommerz.query_transaction", return_value=[]
        ):
            call_command("run_payment_jobs")
        booking.refresh_from_db()
        # The abandoned hold was reaped — i.e. expire_stale_bookings ran.
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

    def test_T1c_FIXED_one_failing_job_does_not_stop_the_others(self):
        """A broken email backend must never stop rooms being reclaimed."""
        booking = self.make_booking()
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=3)
        )
        with patch(
            "apps.bookings.management.commands.enforce_due_deadlines."
            "Command.handle",
            side_effect=RuntimeError("smtp is down"),
        ):
            with self.assertRaises(SystemExit):  # non-zero exit → cron alerts
                call_command("run_payment_jobs")
        booking.refresh_from_db()
        # ...and the room was still released despite the other job failing.
        self.assertEqual(booking.status, Booking.Status.CANCELLED)


class RepricingUnderLiveSessionTests(PaymentQABase):
    """C7 — a booking's price must be frozen while money is in flight against
    it, not merely once money has landed."""

    def test_T2_FIXED_a_live_session_freezes_the_price(self):
        booking = self.make_booking()  # total 9500
        self.assertEqual(booking.total_amount, Decimal("9500.00"))

        # Customer is on the SSLCommerz page paying the full 9500.
        payment = self.pay(booking, "full")
        self.assertEqual(payment.amount, Decimal("9500.00"))

        # Admin edits the package price mid-flight (adult_price 3000 -> 4000).
        Package.objects.filter(pk=self.package.pk).update(
            adult_price=Decimal("4000.00")
        )

        # A full_clean()+save (the Django admin path) no longer re-prices it:
        # a PENDING payment is money in flight and freezes the total.
        booking.refresh_from_db()
        booking.full_clean()
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("9500.00"))

        # The customer pays the 9500 they were quoted and are FULLY paid.
        self.settle(payment)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("9500.00"))
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)
        self.assertFalse(booking.refund_required)

    def test_T2b_FIXED_a_price_drop_cannot_500_the_ipn_or_strand_the_money(self):
        """The mirror case: the price DROPS while a session is live. Previously
        due = total - paid went negative, the non-negative CheckConstraint fired
        as an unhandled IntegrityError inside the IPN, SSLCommerz got a 500 and
        retried forever while the customer's money sat captured and uncredited.
        Now the price is frozen, so it cannot arise at all — and even if it did,
        due is clamped and the IPN fails safe (see T2c)."""
        booking = self.make_booking(room=self.room_2p, adults=2)
        original = booking.total_amount
        payment = self.pay(booking, "full")

        Package.objects.filter(pk=self.package.pk).update(
            adult_price=Decimal("1000.00")
        )
        booking.refresh_from_db()
        booking.full_clean()
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, original)  # frozen

        r = self.settle(payment)  # no 500, no IntegrityError
        self.assertEqual(r.status_code, 200)
        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(booking.paid_amount, original)
        self.assertEqual(booking.due_amount, Decimal("0.00"))

    def test_T2c_FIXED_due_is_clamped_and_overpayment_is_flagged_not_500(self):
        """The backstop, independent of the freeze: if paid ever exceeds total
        by any route, due clamps to 0.00 and the excess is flagged as owed back
        — it never becomes a negative due that collides with the DB constraint
        on the money path."""
        booking = self.make_booking()
        p = self.pay(booking, "full")
        self.settle(p)
        booking.refresh_from_db()

        # Force the pathological state directly (bypassing every guard).
        Booking.objects.filter(pk=booking.pk).update(total_amount=Decimal("5000.00"))
        booking.refresh_from_db()
        booking.save()  # would previously raise IntegrityError

        booking.refresh_from_db()
        self.assertEqual(booking.due_amount, Decimal("0.00"))  # clamped, not -4500
        self.assertTrue(booking.refund_required)
        self.assertIn("overpaid", booking.refund_note.lower())

    def test_T2d_FIXED_the_ipn_never_500s_even_if_processing_blows_up(self):
        """Whatever goes wrong, SSLCommerz must not get an error response: it
        would retry the same poisoned notification forever while real money sits
        captured. Fail safe — 200 back, payment flagged for a human."""
        booking = self.make_booking()
        payment = self.pay(booking, "partial", "4750.00")

        with patch(
            "apps.bookings.payment_service.process_payment_result",
            side_effect=RuntimeError("boom"),
        ):
            r = self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {
                        "tran_id": payment.transaction_id,
                        "val_id": "VAL-X",
                        "status": "VALID",
                    }
                ),
            )
        self.assertEqual(r.status_code, 200)  # not 500

        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertTrue(payment.needs_manual_review)
        self.assertTrue(booking.refund_required)  # a human is told

    def test_T2e_FIXED_price_edit_is_refused_on_a_package_with_active_bookings(self):
        """Prevention, not just containment: the staff API refuses to re-price a
        sailing whose customers were quoted the current price."""
        self.make_booking()
        self.client.force_authenticate(self.staff)
        r = self.client.patch(
            f"/api/staff/packages/{self.package.pk}/",
            {"adult_price": "4000.00"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("active booking", str(r.data))
        self.package.refresh_from_db()
        self.assertEqual(self.package.adult_price, Decimal("3000.00"))


class StaffPaymentRaceTests(PaymentQABase):
    """The staff manual-collection path: does the viewset really re-verify
    the ceiling under a row lock, as the serializer docstring claims?"""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.staff)

    def test_T3_staff_payment_ceiling_is_reverified_under_lock(self):
        import inspect

        from apps.staff import views as staff_views

        src = inspect.getsource(staff_views.StaffPaymentViewSet)
        self.assertIn("select_for_update", src)

    def test_T4_BUG_staff_payment_on_a_refund_owed_booking_is_accepted(self):
        """A booking flagged refund_required (money is owed BACK to the
        customer) still accepts new collections without any warning."""
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        booking.refresh_from_db()
        booking.refund_required = True
        booking.refund_note = "Owed 4750 back"
        booking.save(update_fields=["refund_required", "refund_note"])

        r = self.client.post(
            "/api/staff/payments/",
            {
                "booking": booking.pk,
                "amount": "4750.00",
                "payment_type": "partial",
                "gateway": "cash",
            },
            format="json",
        )
        booking.refresh_from_db()
        # Records fine, and the refund flag is silently still set on a now
        # FULLY_PAID booking.
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)
        self.assertTrue(booking.refund_required)


class CancelledBookingPaymentTests(PaymentQABase):
    """A cancelled booking has due_amount forced to 0.00. What does that do
    to the payment gates, which all key on due_amount?"""

    def test_T5_cancelled_booking_cannot_be_paid(self):
        booking = self.make_booking()
        booking.status = Booking.Status.CANCELLED
        booking.save()
        r = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(r.status_code, 400)

    def test_T6_BUG_reactivating_a_cancelled_booking_leaves_due_at_zero_forever(self):
        """Staff can un-cancel a booking (the serializer explicitly supports
        it). Cancelling zeroed due_amount; save() recomputes it from
        total - paid, so it should come back... unless paid_amount is stale."""
        booking = self.make_booking()  # total 9500
        booking.status = Booking.Status.CANCELLED
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.due_amount, Decimal("0.00"))

        self.client.force_authenticate(self.staff)
        r = self.client.patch(
            f"/api/staff/bookings/{booking.pk}/",
            {"status": "pending"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        booking.refresh_from_db()
        # due is restored correctly
        self.assertEqual(booking.due_amount, Decimal("9500.00"))
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_T7_FIXED_uncancelling_a_paid_booking_rederives_status_from_the_money(
        self,
    ):
        """Un-cancelling: the money decides the status, not the client's value.
        Reactivating a booking with a 4750 BDT deposit as "pending" would leave
        real money in a status claiming none was paid — and PENDING is scanned
        by neither enforce_due_deadlines nor close_sailed_bookings, so it would
        drop out of every job that manages it."""
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)

        booking.status = Booking.Status.CANCELLED
        booking.save()

        self.client.force_authenticate(self.staff)
        r = self.client.patch(
            f"/api/staff/bookings/{booking.pk}/",
            {"status": "pending"},  # staff ask for pending...
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        booking.refresh_from_db()
        # ...but the 4750 on it says partially_paid, and the money wins.
        self.assertEqual(booking.paid_amount, Decimal("4750.00"))
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(booking.due_amount, Decimal("4750.00"))


class EscalatedPaymentTests(PaymentQABase):
    """H7 — a payment the gateway will not resolve must not lock its cabin out
    of inventory forever. Escalation has to come with a way out."""

    def _escalate(self):
        booking = self.make_booking()
        payment = self.pay(booking, "partial", "4750.00")
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(days=10)
        )
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            side_effect=requests.Timeout("gateway down"),
        ):
            for _ in range(settings.PAYMENT_MAX_RECONCILE_ATTEMPTS + 1):
                call_command("reconcile_pending_payments", "--grace-minutes", "0")
        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertTrue(payment.needs_manual_review)
        self.assertTrue(booking.refund_required)  # a human is told
        return booking, payment

    def test_T8_FIXED_a_recovered_gateway_auto_resolves_an_escalated_payment(self):
        """Escalated payments are backed off, not abandoned. Excluding them
        outright meant a cabin stayed out of inventory forever even after the
        gateway came back up. Outages end — so keep asking, just rarely."""
        booking, payment = self._escalate()

        # The back-off holds it for now (it was just queried).
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            return_value=[{"status": "FAILED"}],
        ) as q:
            call_command("reconcile_pending_payments", "--grace-minutes", "0")
        q.assert_not_called()

        # Once the back-off window passes, the gateway is asked again — and it
        # is back up, so the payment resolves and the cabin returns by itself.
        Payment.objects.filter(pk=payment.pk).update(
            last_reconcile_at=timezone.now()
            - timedelta(minutes=settings.PAYMENT_ESCALATED_RETRY_MINUTES + 1)
        )
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            return_value=[{"status": "FAILED"}],
        ) as q:
            call_command("reconcile_pending_payments", "--grace-minutes", "0")
        q.assert_called()

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)

        # ...and now the expiry job can finally reclaim the room.
        call_command("expire_stale_bookings", "--minutes", "1")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)

    def test_T9_FIXED_staff_can_resolve_a_stuck_payment_and_free_the_cabin(self):
        """The gateway may never answer. Staff, having checked the SSLCommerz
        merchant panel, close the payment — and the cabin comes back."""
        booking, payment = self._escalate()

        self.client.force_authenticate(self.staff)
        # It appears in the manual-review queue.
        r = self.client.get("/api/staff/payments/?needs_manual_review=true")
        self.assertEqual(r.status_code, 200)
        self.assertIn(
            payment.transaction_id,
            [p["transaction_id"] for p in r.data["results"]],
        )

        r = self.client.post(
            f"/api/staff/payments/{payment.pk}/resolve/",
            {"status": "failed", "note": "merchant panel shows no capture"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertFalse(payment.needs_manual_review)
        # Audited: who resolved it, when, and what they saw.
        self.assertEqual(
            payment.gateway_payload["manual_resolution"]["by"], self.staff.username
        )
        self.assertIn("no capture", payment.gateway_payload["manual_resolution"]["note"])

        # The cabin is released on the next expiry run and is resellable.
        call_command("expire_stale_bookings", "--minutes", "1")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        second = self.make_booking()  # same room, same package -> succeeds
        self.assertEqual(
            second.rooms.first().room_id, booking.rooms.first().room_id
        )

    def test_T9b_FIXED_staff_can_settle_a_stuck_payment_that_did_capture_money(self):
        """The other half: the merchant panel shows the money WAS taken. Staff
        settle it, and the customer is credited exactly as a gateway settlement
        would have credited them."""
        booking, payment = self._escalate()

        self.client.force_authenticate(self.staff)
        r = self.client.post(
            f"/api/staff/payments/{payment.pk}/resolve/",
            {"status": "success", "note": "captured, confirmed in merchant panel"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)

        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertFalse(payment.needs_manual_review)
        self.assertIsNotNone(payment.paid_at)
        # Credited through the same SUM-based path as any gateway settlement.
        self.assertEqual(booking.paid_amount, Decimal("4750.00"))
        self.assertEqual(booking.due_amount, Decimal("4750.00"))
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)

    def test_T9c_FIXED_resolve_is_staff_only_and_not_double_appliable(self):
        booking, payment = self._escalate()

        # Anonymous cannot touch it.
        r = self.client.post(
            f"/api/staff/payments/{payment.pk}/resolve/",
            {"status": "failed"},
            format="json",
        )
        self.assertIn(r.status_code, (401, 403))

        self.client.force_authenticate(self.staff)
        r1 = self.client.post(
            f"/api/staff/payments/{payment.pk}/resolve/",
            {"status": "failed"},
            format="json",
        )
        self.assertEqual(r1.status_code, 200)
        # Resolving an already-terminal payment is refused, not silently redone.
        r2 = self.client.post(
            f"/api/staff/payments/{payment.pk}/resolve/",
            {"status": "success"},
            format="json",
        )
        self.assertEqual(r2.status_code, 400)


class MinDepositEdgeTests(PaymentQABase):
    """M5 — the deposit floor is load-bearing for room inventory, so its value
    must be bounded. 0% would let one paisa hold a cabin forever (the C2 bug
    this field exists to prevent); >100% makes partial payment impossible."""

    def test_T10_FIXED_min_deposit_percent_of_zero_is_rejected(self):
        from django.core.exceptions import ValidationError as DjangoVE

        self.package.min_deposit_percent = Decimal("0.00")
        with self.assertRaises(DjangoVE):
            self.package.full_clean()
        self.package.refresh_from_db()

    def test_T10b_FIXED_the_staff_api_rejects_an_out_of_range_deposit_floor(self):
        self.client.force_authenticate(self.staff)
        for bad in ("0", "0.00", "150.00", "-10.00"):
            r = self.client.patch(
                f"/api/staff/packages/{self.package.pk}/",
                {"min_deposit_percent": bad},
                format="json",
            )
            self.assertEqual(r.status_code, 400, f"{bad} was accepted: {r.data}")
        self.package.refresh_from_db()
        self.assertEqual(self.package.min_deposit_percent, Decimal("50.00"))

    def test_T10c_FIXED_a_valid_floor_is_still_settable_and_enforced(self):
        self.client.force_authenticate(self.staff)
        r = self.client.patch(
            f"/api/staff/packages/{self.package.pk}/",
            {"min_deposit_percent": "25.00"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.client.force_authenticate(None)

        booking = self.make_booking()  # total 9500 -> floor 2375
        too_small = self.initiate(
            booking, {"payment_type": "partial", "amount": "2000.00"}
        )
        self.assertEqual(too_small.status_code, 400)
        ok = self.initiate(booking, {"payment_type": "partial", "amount": "2375.00"})
        self.assertEqual(ok.status_code, 200, ok.data)


class BalanceBeforeJourneyTests(PaymentQABase):
    """H6 — client policy: the balance may be settled ANY time before the
    journey; a deposit-paid customer is never auto-cancelled, and whatever is
    still owed the guide collects on board. So the balance deadline is a soft
    reminder date, not a hard gate, and online payment stays open until the
    ship sails."""

    def _partly_paid_near_departure(self):
        """A partially-paid booking whose soft balance deadline has passed but
        whose ship has not yet sailed."""
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=3),
            balance_due_days_before_start=5,  # soft deadline already passed
        )
        self.package.refresh_from_db()
        self.assertLess(self.package.balance_due_at(), timezone.now())
        booking.refresh_from_db()
        return booking

    def test_T12_FIXED_the_balance_can_still_be_paid_after_the_soft_deadline(self):
        """Past the soft deadline but before departure, the customer can still
        pay the balance online — it is NOT blocked (the old H8 gate contradicted
        the 'before the journey' policy the client chose to keep)."""
        booking = self._partly_paid_near_departure()
        r = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(r.status_code, 200, r.data)
        self.settle(Payment.objects.get(transaction_id=r.data["tran_id"]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)
        self.assertEqual(booking.due_amount, Decimal("0.00"))

    def test_T12b_FIXED_the_deadline_cron_never_cancels_a_partially_paid_booking(
        self,
    ):
        """enforce_due_deadlines only reminds now — it must never cancel a
        deposit-paid booking or free its cabin (client policy, H6)."""
        booking = self._partly_paid_near_departure()
        call_command("enforce_due_deadlines")
        booking.refresh_from_db()
        # Still reserved, still partially paid, deposit intact — the guide will
        # collect the balance on board.
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(booking.paid_amount, Decimal("4750.00"))
        self.assertFalse(booking.refund_required)

    def test_T12c_FIXED_online_balance_payment_stops_only_once_the_ship_sails(self):
        """The one hard limit: the ship has departed. After that the balance is
        the guide's to collect, not the website's."""
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() - timedelta(days=1),  # already sailed
            end_date=timezone.localdate() + timedelta(days=1),
        )
        booking.refresh_from_db()
        r = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("departed", str(r.data).lower())

    def test_T12d_FIXED_the_deadline_date_is_visible_to_the_customer(self):
        """The soft deadline is shown as a date so the customer can plan — it
        just no longer cancels them if they miss it."""
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        r = self.client.get(f"/api/bookings/{booking.booking_code}/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.data["balance_due_at"])

    def test_T13_FIXED_paying_the_balance_leaves_a_fully_paid_booking_untouched(self):
        """A customer who cleared the balance is fully paid; the reminder cron
        never touches them."""
        booking = self.make_booking()
        self.settle(self.pay(booking, "partial", "4750.00"))
        self.settle(self.pay(booking, "full"))
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)

        Package.objects.filter(pk=self.package.pk).update(
            start_date=timezone.localdate() + timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=3),
            balance_due_days_before_start=5,
        )
        call_command("enforce_due_deadlines")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)


class NotificationTests(PaymentQABase):
    def test_T14_balance_reminder_states_the_due_amount(self):
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        mail.outbox.clear()

        self.package.start_date = timezone.localdate() + timedelta(days=4)
        self.package.end_date = self.package.start_date + timedelta(days=2)
        self.package.balance_due_days_before_start = 3
        self.package.save()

        call_command("enforce_due_deadlines")
        self.assertEqual(len(mail.outbox), 1, [m.subject for m in mail.outbox])
        body = mail.outbox[0].body
        self.assertIn("4750", body)

    def test_T15_FIXED_reaping_an_abandoned_cart_emails_nobody(self):
        """M6: the expiry cron reaping a never-paid abandoned checkout must not
        email the visitor "your booking has been cancelled", citing a deposit
        and a cancellation-charge schedule. They never made a booking as far as
        they are concerned — and at one mail per abandoned cart it is a
        deliverability risk on the domain we send real invoices from."""
        booking = self.make_booking()
        mail.outbox.clear()
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )
        with self.captureOnCommitCallbacks(execute=True):
            call_command("expire_stale_bookings", "--minutes", "30")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(mail.outbox, [])

    def test_T15b_FIXED_a_deliberate_cancellation_still_emails_the_customer(self):
        """The M3 behaviour is preserved: staff cancelling a real booking — paid
        or not — still tells the customer. Only the abandoned-cart reaping is
        silent."""
        booking = self.make_booking()
        p = self.pay(booking, "partial", "4750.00")
        self.settle(p)
        mail.outbox.clear()

        self.client.force_authenticate(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch(
                f"/api/staff/bookings/{booking.pk}/",
                {"status": "cancelled"},
                format="json",
            )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(booking.booking_code, mail.outbox[0].body)


class CouponTests(PaymentQABase):
    def test_T17_no_coupon_feature_exists(self):
        """Item 15 — still N/A. Nothing named coupon/discount/promo anywhere."""
        import os

        backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for root, dirs, files in os.walk(os.path.join(backend, "apps")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if not f.endswith(".py") or f.startswith("test"):
                    continue
                text = open(os.path.join(root, f), encoding="utf-8").read().lower()
                if "coupon" in text or "promo_code" in text:
                    hits.append(f)
        self.assertEqual(hits, [])


class DjangoAdminRepricingTests(PaymentQABase):
    """The one REAL code path that calls full_clean() on an existing booking is
    the Django admin's ModelForm (BookingAdmin is a plain ModelAdmin over an
    editable customer_name/phone/email). It must not re-price a live booking
    either — the staff REST API being safe is not enough."""

    def test_T16_FIXED_django_admin_edit_cannot_reprice_a_live_booking(self):
        from django.contrib.admin.sites import AdminSite

        from apps.bookings.admin import BookingAdmin

        booking = self.make_booking()  # total 9500
        payment = self.pay(booking, "full")  # 9500 live at the gateway
        self.assertEqual(payment.amount, Decimal("9500.00"))

        # The package price changes (forced past the staff-API guard).
        Package.objects.filter(pk=self.package.pk).update(
            adult_price=Decimal("4000.00")
        )

        # Staff fix a typo in the customer's phone number in Django admin. The
        # ModelForm calls full_clean() -> Booking.clean(); the live PENDING
        # payment now freezes the price, so this no longer re-prices anything.
        model_admin = BookingAdmin(Booking, AdminSite())
        FormClass = model_admin.get_form(None, booking, change=True)
        # room/adult_count/kid_details moved to the BookingRoom inline, so the
        # Booking admin form no longer carries them — the rooms are edited on
        # their own inline formset (not exercised here).
        data = {
            "customer_name": booking.customer_name,
            "phone": "01800000000",
            "email": booking.email,
            "package": booking.package_id,
            "status": booking.status,
            "refund_required": False,
            "refund_note": "",
            # Empty inline management form so the change form validates without
            # touching the rooms.
            "rooms-TOTAL_FORMS": "0",
            "rooms-INITIAL_FORMS": "0",
            "rooms-MIN_NUM_FORMS": "0",
            "rooms-MAX_NUM_FORMS": "1000",
            "payments-TOTAL_FORMS": "0",
            "payments-INITIAL_FORMS": "0",
            "status_logs-TOTAL_FORMS": "0",
            "status_logs-INITIAL_FORMS": "0",
        }
        form = FormClass(data, instance=Booking.objects.get(pk=booking.pk))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("9500.00"))  # frozen
        self.assertEqual(booking.phone, "01800000000")  # the edit still applied

        # The customer pays the 9500 they authorised and is fully paid.
        self.settle(payment)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("9500.00"))
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)

        self.package.adult_price = Decimal("3000.00")
        self.package.save()

    def test_T16b_staff_booking_api_patch_does_NOT_reprice(self):
        """The staff REST API is safe: DRF never calls model clean(), so a
        contact-detail PATCH leaves total_amount alone. Only the Django admin
        (and BookingCreateSerializer, at create time) call full_clean()."""
        booking = self.make_booking()
        self.pay(booking, "full")
        self.package.adult_price = Decimal("4000.00")
        self.package.save()

        self.client.force_authenticate(self.staff)
        r = self.client.patch(
            f"/api/staff/bookings/{booking.pk}/",
            {"phone": "01800000000"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("9500.00"))  # unchanged

        self.package.adult_price = Decimal("3000.00")
        self.package.save()
