"""QA Phase 5 — payment flow probes (full + partial payment).

Originally written as read-only probes documenting the behaviour observed
during the Phase 5 QA pass (see qa-reports/phase5-payment.md). Every bug
found there (C1–C5, H1–H3, M1–M2) has since been fixed, and the probes that
documented defective behaviour now assert the FIXED behaviour instead — so a
regression on any of them is a reopened QA finding.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.core import mail
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import User
from apps.bookings.models import Booking, BookingRoom, Payment
from apps.bookings.test_api import build_fixtures
from apps.testing import ThrottlelessTestMixin, create_booking, sign_ipn

GATEWAY_URL = "https://sandbox.sslcommerz.com/gwprocess/testsession"


class PaymentQABase(ThrottlelessTestMixin, APITestCase):
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
            username="qastaff", password="x", is_staff=True
        )

    def make_booking(self, room=None, adults=2):
        # 4P/2 adults -> 3500 + 2*3000 = 9500.00 (min deposit 4750)
        return create_booking(
            self.package,
            rooms=[{"room": room or self.room_4p, "adult_count": adults, "kid_details": []}],
        )

    def initiate(self, booking, payload):
        with patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            return self.client.post(
                f"/api/bookings/{booking.booking_code}/pay/", payload, format="json"
            )

    def verdict(self, payment, **over):
        d = {
            "status": "VALID",
            "tran_id": payment.transaction_id,
            "val_id": "VAL-" + payment.transaction_id,
            "amount": str(payment.amount),
            "currency": "BDT",
        }
        d.update(over)
        return d

    def settle(self, payment, **over):
        """Drive a signature-valid IPN with a mocked gateway validation
        verdict (the signature is SSLCommerz's own MD5 scheme, computed with
        the configured store password — the view really verifies it)."""
        data = self.verdict(payment, **over)
        with patch(
            "apps.bookings.sslcommerz.validate_payment", return_value=data
        ):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(
                    "/api/payments/ipn/",
                    sign_ipn(
                        {"tran_id": payment.transaction_id, "val_id": data["val_id"]}
                    ),
                )

    def fail_at_gateway(self, payment, gateway_status="FAILED"):
        """Close a payment via the fail redirect, with the gateway confirming
        the attempt is dead (the redirect alone never changes state)."""
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            return_value=[{"status": gateway_status}],
        ):
            return self.client.post(
                "/api/payments/fail/", {"tran_id": payment.transaction_id}
            )

    def pay(self, booking, payment_type, amount=None):
        payload = {"payment_type": payment_type}
        if amount is not None:
            payload["amount"] = str(amount)
        r = self.initiate(booking, payload)
        assert r.status_code == 200, r.data
        return Payment.objects.get(transaction_id=r.data["tran_id"])


# ── 1. Full payment: status only after gateway confirms ─────────────────────
class FullPaymentTests(PaymentQABase):
    def test_1_full_payment_confirms_only_after_server_side_validation(self):
        b = self.make_booking()
        p = self.pay(b, "full")
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)  # not yet paid
        self.assertEqual(b.paid_amount, Decimal("0.00"))

        self.settle(p)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.FULLY_PAID)
        self.assertEqual(b.paid_amount, Decimal("9500.00"))
        self.assertEqual(b.due_amount, Decimal("0.00"))

    def test_1b_success_redirect_without_valid_val_id_credits_nothing(self):
        """Client-controlled redirect POST must not confirm a booking."""
        b = self.make_booking()
        p = self.pay(b, "full")
        # Attacker replays the success redirect with a bogus val_id; the
        # Validation API says NOT VALID.
        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            return_value={"status": "INVALID_TRANSACTION"},
        ):
            self.client.post(
                "/api/payments/success/",
                {"tran_id": p.transaction_id, "val_id": "forged"},
            )
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)
        self.assertEqual(b.paid_amount, Decimal("0.00"))


# ── 2/4/6. Partial payment mechanics ────────────────────────────────────────
class PartialPaymentTests(PaymentQABase):
    def test_2_partial_payment_sets_partially_paid_with_exact_due(self):
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        self.settle(p)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(b.paid_amount, Decimal("5000.00"))
        self.assertEqual(b.due_amount, Decimal("4500.00"))

    def test_4_paying_off_balance_lands_exactly_zero(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        p2 = self.pay(b, "full")  # server picks the remaining due
        self.assertEqual(p2.amount, Decimal("4500.00"))
        self.settle(p2)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.FULLY_PAID)
        self.assertEqual(b.due_amount, Decimal("0.00"))
        self.assertEqual(b.due_amount.as_tuple().exponent, -2)  # no float dust

    def test_6_three_installments_recalculate_due_each_time(self):
        b = self.make_booking()
        for amount, expect_due in [
            ("5000.00", "4500.00"),  # first payment must clear the 4750 floor
            ("2500.00", "2000.00"),
            ("2000.00", "0.00"),
        ]:
            self.settle(self.pay(b, "partial", amount))
            b.refresh_from_db()
            self.assertEqual(b.due_amount, Decimal(expect_due))
        self.assertEqual(b.status, Booking.Status.FULLY_PAID)
        self.assertEqual(b.paid_amount, Decimal("9500.00"))


# ── 3. Minimum partial payment enforcement (C2 — fixed) ─────────────────────
class MinimumDepositTests(PaymentQABase):
    def test_3_below_minimum_first_deposit_is_rejected(self):
        """C2 fix: the invoice policy ("confirmation requires a 50% advance")
        is enforced server-side from Package.min_deposit_percent — a 1-paisa
        deposit can no longer lock a cabin."""
        b = self.make_booking()
        for amount in ("0.01", "1.00", "4749.99"):
            r = self.initiate(b, {"payment_type": "partial", "amount": amount})
            self.assertEqual(r.status_code, 400, amount)
            self.assertIn("4750", str(r.data["amount"]))
        self.assertFalse(Payment.objects.filter(booking=b).exists())

    def test_3b_zero_and_negative_are_rejected(self):
        b = self.make_booking()
        for amount in ("0", "0.00", "-100.00"):
            r = self.initiate(b, {"payment_type": "partial", "amount": amount})
            self.assertEqual(r.status_code, 400, amount)

    def test_3c_deposit_at_the_floor_holds_the_room_and_top_ups_are_free(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "4750.00"))  # exactly the floor
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
        # Top-ups toward the balance have no floor.
        r = self.initiate(b, {"payment_type": "partial", "amount": "100.00"})
        self.assertEqual(r.status_code, 200, r.data)

    def test_3d_floor_is_admin_configurable_data_not_a_constant(self):
        self.package.min_deposit_percent = Decimal("25.00")
        self.package.save(update_fields=["min_deposit_percent"])
        try:
            b = self.make_booking()
            r = self.initiate(b, {"payment_type": "partial", "amount": "2375.00"})
            self.assertEqual(r.status_code, 200, r.data)  # 25% of 9500
        finally:
            self.package.min_deposit_percent = Decimal("50.00")
            self.package.save(update_fields=["min_deposit_percent"])


# ── 5. Overpayment ──────────────────────────────────────────────────────────
class OverpaymentTests(PaymentQABase):
    def test_5_public_api_rejects_paying_more_than_due(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        r = self.initiate(b, {"payment_type": "partial", "amount": "6000.00"})
        self.assertEqual(r.status_code, 400)

    def test_5b_gateway_verdict_with_inflated_amount_is_rejected(self):
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        self.settle(p, amount="9500.00")  # gateway says more than we asked
        p.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.FAILED)
        self.assertEqual(b.paid_amount, Decimal("0.00"))

    def test_5c_staff_manual_overpayment_is_rejected(self):
        """C3 fix: the staff/manual collection path enforces the same
        server-side ceiling as the public API — no negative due can reach
        the dashboard aggregates or the guide's printed collection sheet."""
        b = self.make_booking()
        self.client.force_authenticate(self.staff)
        r = self.client.post(
            "/api/staff/payments/",
            {
                "booking": b.pk,
                "amount": "20000.00",  # total is 9500
                "payment_type": "full",
                "gateway": "cash",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("amount", r.data)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("0.00"))
        self.assertEqual(b.due_amount, Decimal("9500.00"))
        self.assertFalse(Payment.objects.filter(booking=b).exists())

    def test_5d_staff_negative_amount_is_rejected_and_db_blocked(self):
        """C4 fix: a negative 'payment' (which would silently erase settled
        money from the ledger) is rejected by the serializer AND blocked by
        a DB CHECK constraint on every ORM path."""
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        self.client.force_authenticate(self.staff)
        r = self.client.post(
            "/api/staff/payments/",
            {
                "booking": b.pk,
                "amount": "-5000.00",
                "payment_type": "partial",
                "gateway": "cash",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("5000.00"))  # money intact

        # Belt and braces: no ORM path can write a non-positive payment.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(
                    booking=b,
                    amount=Decimal("-5000.00"),
                    payment_type=Payment.PaymentType.PARTIAL,
                    status=Payment.Status.SUCCESS,
                    transaction_id=f"{b.booking_code}-NEG",
                )

    def test_5e_staff_cannot_record_payment_on_a_cancelled_booking(self):
        b = self.make_booking()
        b.status = Booking.Status.CANCELLED
        b.save()
        self.client.force_authenticate(self.staff)
        r = self.client.post(
            "/api/staff/payments/",
            {"booking": b.pk, "amount": "5000.00", "payment_type": "partial"},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("booking", r.data)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("0.00"))


# ── 7/12/17. Room release rules ─────────────────────────────────────────────
class RoomReleaseTests(PaymentQABase):
    def _expire(self, b, minutes=999):
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )
        Payment.objects.filter(booking=b).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )
        call_command("expire_stale_bookings", verbosity=0)
        b.refresh_from_db()

    def test_7_failed_first_payment_releases_the_room(self):
        b = self.make_booking()
        p = self.pay(b, "full")
        self.fail_at_gateway(p)  # gateway confirms the attempt failed
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.FAILED)
        self._expire(b)
        self.assertEqual(b.status, Booking.Status.CANCELLED)

    def test_7b_deposit_paid_then_failed_top_up_keeps_the_room(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        p2 = self.pay(b, "full")
        self.fail_at_gateway(p2)
        self._expire(b)
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(b.paid_amount, Decimal("5000.00"))

    def test_17_four_failed_due_payments_do_not_degrade_the_booking(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        for _ in range(4):
            p = self.pay(b, "full")
            self.fail_at_gateway(p)
            b.refresh_from_db()
            self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
            self.assertEqual(b.due_amount, Decimal("4500.00"))
        self._expire(b)
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)

    def test_12_abandoned_pending_booking_is_released_after_the_hold(self):
        b = self.make_booking()
        self._expire(b)
        self.assertEqual(b.status, Booking.Status.CANCELLED)

    def test_12b_abandoned_mid_checkout_session_protects_the_hold(self):
        b = self.make_booking()
        self.pay(b, "full")  # live gateway session, customer walked away
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        call_command("expire_stale_bookings", verbosity=0)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)  # spared, session live


# ── 8. Gateway timeout / reconciliation (H1 — fixed) ────────────────────────
class GatewayTimeoutTests(PaymentQABase):
    def test_8_session_creation_timeout_fails_the_payment_cleanly(self):
        b = self.make_booking()
        with patch(
            "apps.bookings.sslcommerz.create_session",
            side_effect=requests.Timeout("gateway timed out"),
        ):
            r = self.client.post(
                f"/api/bookings/{b.booking_code}/pay/",
                {"payment_type": "full"},
                format="json",
            )
        self.assertEqual(r.status_code, 502)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)
        self.assertEqual(Payment.objects.get(booking=b).status, Payment.Status.FAILED)

    def _strand_payment(self, b):
        """Full payment whose only IPN hits a Validation API timeout —
        the payment is deliberately left PENDING."""
        p = self.pay(b, "full")
        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            side_effect=requests.Timeout("validation timed out"),
        ):
            self.client.post(
                "/api/payments/ipn/",
                sign_ipn({"tran_id": p.transaction_id, "val_id": "VAL1"}),
            )
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.PENDING)
        return p

    def test_8b_reconciliation_job_settles_a_stranded_pending_payment(self):
        """H1 fix: reconcile_pending_payments queries the gateway by tran_id
        and drives the stranded payment to its true state — the promised
        're-verify' now actually exists and is scheduled before expiry."""
        b = self.make_booking()
        p = self._strand_payment(b)

        verdict = self.verdict(p, val_id="VAL-RECON")
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            return_value=[{"status": "VALID", "val_id": "VAL-RECON"}],
        ):
            with patch(
                "apps.bookings.sslcommerz.validate_payment", return_value=verdict
            ):
                with self.captureOnCommitCallbacks(execute=True):
                    call_command(
                        "reconcile_pending_payments", "--grace-minutes=0",
                        verbosity=0,
                    )
        p.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.SUCCESS)  # money visible
        self.assertEqual(b.paid_amount, Decimal("9500.00"))
        self.assertEqual(b.status, Booking.Status.FULLY_PAID)

    def test_8b2_reconciliation_closes_a_dead_unattempted_session(self):
        b = self.make_booking()
        p = self._strand_payment(b)
        Payment.objects.filter(pk=p.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        with patch(
            "apps.bookings.sslcommerz.query_transaction", return_value=[]
        ):
            call_command(
                "reconcile_pending_payments", "--grace-minutes=0", verbosity=0
            )
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.FAILED)
        # ...and only now, with the session confirmed dead, may expiry
        # reclaim the room.
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        call_command("expire_stale_bookings", verbosity=0)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CANCELLED)

    def test_8c_expiry_never_releases_a_room_with_an_unresolved_payment(self):
        """C1/H1 fix: however old the booking and its PENDING payment get,
        the room is not released until the gateway has definitively said no
        money is coming (via the reconciliation job)."""
        b = self.make_booking()
        self._strand_payment(b)
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        Payment.objects.filter(booking=b).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        call_command("expire_stale_bookings", verbosity=0)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)  # room kept


# ── 9. Duplicate webhook ────────────────────────────────────────────────────
class DuplicateWebhookTests(PaymentQABase):
    def test_9_duplicate_ipn_full_payment_is_idempotent(self):
        b = self.make_booking()
        p = self.pay(b, "full")
        for _ in range(3):
            self.settle(p)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("9500.00"))
        self.assertEqual(b.due_amount, Decimal("0.00"))
        self.assertEqual(
            Payment.objects.filter(booking=b, status=Payment.Status.SUCCESS).count(), 1
        )

    def test_9b_duplicate_ipn_partial_payment_does_not_double_subtract(self):
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        for _ in range(3):
            self.settle(p)
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("5000.00"))
        self.assertEqual(b.due_amount, Decimal("4500.00"))  # not 9500 subtracted

    def test_9c_duplicate_ipn_sends_only_one_invoice(self):
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        mail.outbox.clear()
        for _ in range(3):
            self.settle(p)
        self.assertEqual(len(mail.outbox), 1)

    def test_9d_ipn_then_success_redirect_credits_once(self):
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        self.settle(p)
        data = self.verdict(p)
        with patch("apps.bookings.sslcommerz.validate_payment", return_value=data):
            self.client.post(
                "/api/payments/success/",
                {"tran_id": p.transaction_id, "val_id": data["val_id"]},
            )
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("5000.00"))


# ── 10. Amount tampering ────────────────────────────────────────────────────
class AmountTamperingTests(PaymentQABase):
    def test_10_client_amount_ignored_on_full_payment(self):
        b = self.make_booking()
        r = self.initiate(b, {"payment_type": "full", "amount": "1.00"})
        p = Payment.objects.get(transaction_id=r.data["tran_id"])
        self.assertEqual(p.amount, Decimal("9500.00"))  # server-decided

    def test_10b_low_gateway_amount_on_a_full_payment_is_rejected(self):
        b = self.make_booking()
        p = self.pay(b, "full")
        self.settle(p, amount="1.00")  # attacker paid 1 BDT at the gateway
        p.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.FAILED)
        self.assertEqual(b.status, Booking.Status.PENDING)
        self.assertEqual(b.paid_amount, Decimal("0.00"))

    def test_10c_currency_swap_is_rejected(self):
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        self.settle(p, currency="USD", amount="5000.00")
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.FAILED)

    def test_10d_cross_transaction_val_id_replay_is_rejected(self):
        """A val_id belonging to a different (cheap) transaction cannot be
        replayed onto an expensive booking — tran_id is cross-checked."""
        victim = self.make_booking()
        p_victim = self.pay(victim, "full")
        attacker = self.make_booking(room=self.room_2p, adults=1)
        # attacker total: 2000 + 3000 = 5000; min deposit 2500
        p_attacker = self.pay(attacker, "partial", "2500.00")
        cheap = self.verdict(p_attacker)  # a real, VALID 2500 BDT verdict
        with patch("apps.bookings.sslcommerz.validate_payment", return_value=cheap):
            self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {"tran_id": p_victim.transaction_id, "val_id": cheap["val_id"]}
                ),
            )
        p_victim.refresh_from_db()
        victim.refresh_from_db()
        self.assertEqual(p_victim.status, Payment.Status.FAILED)
        self.assertEqual(victim.paid_amount, Decimal("0.00"))


# ── 11. Webhook / callback endpoint security (C5 — fixed) ───────────────────
class WebhookSecurityTests(PaymentQABase):
    def test_11_unsigned_ipn_is_rejected_before_any_processing(self):
        """C5 fix: an IPN without a valid verify_sign is 400'd before any
        state change — the Validation API is not even consulted."""
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        with patch("apps.bookings.sslcommerz.validate_payment") as validate:
            r = self.client.post(
                "/api/payments/ipn/",
                {
                    "tran_id": p.transaction_id,
                    "val_id": "attacker",
                    "status": "VALID",
                    "amount": "5000.00",
                    "currency": "BDT",
                },
            )
        self.assertEqual(r.status_code, 400)
        validate.assert_not_called()
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("0.00"))

    def test_11a_signed_ipn_with_bogus_val_id_still_credits_nothing(self):
        """Even a signature-valid IPN is only a trigger — the verdict comes
        from the outbound authenticated Validation API."""
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            return_value={"status": "INVALID_TRANSACTION"},
        ) as validate:
            self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {
                        "tran_id": p.transaction_id,
                        "val_id": "attacker",
                        "status": "VALID",
                    }
                ),
            )
        validate.assert_called_once()  # POST body never trusted
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("0.00"))

    def test_11b_unsigned_failed_status_cannot_kill_a_live_session(self):
        """C5 fix: the forged status=FAILED IPN is rejected, the session
        stays live, and the customer's real money settles normally."""
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")

        # Attacker, unauthenticated, no signature:
        r = self.client.post(
            "/api/payments/ipn/",
            {"tran_id": p.transaction_id, "status": "FAILED"},
        )
        self.assertEqual(r.status_code, 400)
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.PENDING)  # session alive

        # The customer's real money settles fine.
        self.settle(p)
        p.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.SUCCESS)
        self.assertEqual(b.paid_amount, Decimal("5000.00"))
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)

    def test_11c_fail_redirect_without_gateway_confirmation_changes_nothing(self):
        """C5 fix: the browser-facing fail/cancel redirects are presentation-
        only. With no gateway record confirming a dead attempt, the payment
        stays PENDING."""
        b = self.make_booking()
        p = self.pay(b, "full")
        with patch(
            "apps.bookings.sslcommerz.query_transaction", return_value=[]
        ):
            self.client.post("/api/payments/fail/", {"tran_id": p.transaction_id})
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.PENDING)

    def test_11d_signature_verification_helper_exists_and_fails_closed(self):
        from apps.bookings import sslcommerz as ssl

        self.assertTrue(callable(ssl.verify_ipn_signature))
        self.assertFalse(ssl.verify_ipn_signature({}))  # no signature → reject
        self.assertFalse(
            ssl.verify_ipn_signature(
                {"tran_id": "X", "verify_key": "tran_id", "verify_sign": "bogus"}
            )
        )
        self.assertTrue(ssl.verify_ipn_signature(sign_ipn({"tran_id": "X"})))


# ── 13. Cancellation & manual-refund process ────────────────────────────────
class CancellationTests(PaymentQABase):
    def test_13a_public_api_exposes_no_cancel_or_status_write(self):
        b = self.make_booking()
        for method, url in [
            ("patch", f"/api/bookings/{b.booking_code}/"),
            ("put", f"/api/bookings/{b.booking_code}/"),
            ("delete", f"/api/bookings/{b.booking_code}/"),
        ]:
            r = getattr(self.client, method)(
                url, {"status": "cancelled"}, format="json"
            )
            self.assertEqual(r.status_code, 405, f"{method} {url}")
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)

    def test_13a2_staff_cancel_requires_staff_auth(self):
        b = self.make_booking()
        r = self.client.patch(
            f"/api/staff/bookings/{b.pk}/", {"status": "cancelled"}, format="json"
        )
        self.assertIn(r.status_code, (401, 403))
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)

    def test_13b_admin_cancel_of_a_deposit_booking_releases_the_room(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        self.client.force_authenticate(self.staff)
        r = self.client.patch(
            f"/api/staff/bookings/{b.pk}/", {"status": "cancelled"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.CANCELLED)

        # Room is genuinely back on sale for the same package:
        self.client.force_authenticate(None)
        r = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.pk,
                "rooms": [
                    {"room_id": self.room_4p.pk, "adult_count": 2, "kid_details": []}
                ],
                "customer_name": "New Guest",
                "phone": "01800000000",
                "email": "new@example.com",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_13c_cancelled_booking_zeroes_due_and_flags_the_refund(self):
        """M1 + H3 fix: cancellation keeps paid_amount (the refund
        conversation), zeroes the phantom receivable, and raises the
        refund_required flag automatically."""
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        self.client.force_authenticate(self.staff)
        self.client.patch(
            f"/api/staff/bookings/{b.pk}/", {"status": "cancelled"}, format="json"
        )
        b.refresh_from_db()
        self.assertEqual(b.paid_amount, Decimal("5000.00"))  # preserved
        self.assertEqual(b.due_amount, Decimal("0.00"))  # no phantom receivable
        self.assertTrue(b.refund_required)  # staff must call the customer

    def test_13c2_refund_flag_fields_exist_on_booking(self):
        names = {f.name for f in Booking._meta.get_fields()}
        self.assertIn("refund_required", names)
        self.assertIn("refund_note", names)

    def test_13c3_staff_can_filter_and_see_refund_owed_bookings(self):
        """H3 fix: cancelled-with-deposit is a first-class, filterable state
        in the staff API — not a paid_amount nuance nobody notices."""
        paid = self.make_booking()
        self.settle(self.pay(paid, "partial", "5000.00"))
        never_paid = self.make_booking(room=self.room_2p, adults=2)
        for bk in (paid, never_paid):
            bk.refresh_from_db()  # pick up the settled paid_amount
            bk.status = Booking.Status.CANCELLED
            bk.save()
        self.client.force_authenticate(self.staff)

        r = self.client.get("/api/staff/bookings/?status=cancelled")
        rows = {row["booking_code"]: row for row in r.data["results"]}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[paid.booking_code]["refund_required"])
        self.assertFalse(rows[never_paid.booking_code]["refund_required"])

        r = self.client.get("/api/staff/bookings/?refund_required=true")
        codes = {row["booking_code"] for row in r.data["results"]}
        self.assertEqual(codes, {paid.booking_code})

        # The refunds-owed queue is on the dashboard landing page too.
        r = self.client.get("/api/staff/overview/")
        self.assertEqual(r.data["refunds_owed_count"], 1)
        self.assertEqual(r.data["refunds_owed_paid_total"], Decimal("5000.00"))

        # Staff clear the flag once the customer has been refunded.
        r = self.client.patch(
            f"/api/staff/bookings/{paid.pk}/",
            {"refund_required": False, "refund_note": "Refunded via bKash 11 Jul."},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        paid.refresh_from_db()
        self.assertFalse(paid.refund_required)

    def test_13d_frontend_exposes_no_refund_trigger(self):
        """Manual-refund design honoured: no refund route anywhere in urlconf."""
        from django.urls import get_resolver

        patterns = str(get_resolver().url_patterns)
        self.assertNotIn("refund", patterns.lower())


# ── 14. Due-payment deadline (H2 — fixed) ───────────────────────────────────
class DueDeadlineTests(PaymentQABase):
    def _move_package(self, start_in_days):
        today = timezone.localdate()
        self.package.start_date = today + timedelta(days=start_in_days)
        self.package.end_date = self.package.start_date + timedelta(days=2)
        self.package.save()
        # Keep the booking window open regardless of the wall clock (the
        # auto-derived cutoff for a near departure may already be past noon),
        # so the rebooking assertions test room availability, not the cutoff.
        self.package.booking_cutoff_datetime = timezone.now() + timedelta(hours=1)
        self.package.save()

    def test_14_overdue_balance_is_never_auto_cancelled(self):
        """H6 (client ruling): the balance may be settled any time before the
        journey, so enforce_due_deadlines must NEVER cancel a deposit-paid
        booking or free its cabin. Any balance still owed at sailing is
        collected on board by the guide. It only ever reminds."""
        from django.core.management import get_commands

        self.assertIn("enforce_due_deadlines", get_commands())

        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        # Departure is tomorrow; the old deadline (noon, 3 days before) passed.
        self._move_package(start_in_days=1)
        call_command("enforce_due_deadlines", verbosity=0)
        b.refresh_from_db()
        # Booking stands, deposit intact, nothing owed back — cabin still held.
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(b.paid_amount, Decimal("5000.00"))
        self.assertEqual(b.due_amount, Decimal("4500.00"))
        self.assertFalse(b.refund_required)

        # The room is NOT on public sale — the customer still holds it.
        r = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.pk,
                "rooms": [
                    {"room_id": self.room_4p.pk, "adult_count": 2, "kid_details": []}
                ],
                "customer_name": "Standby Guest",
                "phone": "01800000001",
                "email": "standby@example.com",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 409, r.data)  # room still unavailable

    def test_14b_reminder_email_goes_out_once_before_the_deadline(self):
        b = self.make_booking()
        self.settle(self.pay(b, "partial", "5000.00"))
        # Deadline is tomorrow noon → inside the 2-day reminder window.
        self._move_package(start_in_days=4)
        mail.outbox.clear()
        call_command("enforce_due_deadlines", verbosity=0)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)  # not cancelled
        self.assertIsNotNone(b.due_reminder_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("balance", mail.outbox[0].subject.lower())
        self.assertIn("4500.00", mail.outbox[0].body)

        call_command("enforce_due_deadlines", verbosity=0)  # idempotent
        self.assertEqual(len(mail.outbox), 1)

    def test_14c_fully_paid_and_fresh_bookings_are_untouched(self):
        b = self.make_booking()
        self.settle(self.pay(b, "full"))
        self._move_package(start_in_days=1)
        call_command("enforce_due_deadlines", verbosity=0)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.FULLY_PAID)


# ── 15. Coupon / discount ───────────────────────────────────────────────────
class CouponTests(PaymentQABase):
    def test_15_no_coupon_or_discount_feature_exists(self):
        from apps.packages import models as pkg_models

        self.assertFalse(
            [n for n in dir(pkg_models) if "coupon" in n.lower() or "discount" in n.lower()]
        )
        names = {f.name for f in Booking._meta.get_fields()}
        self.assertNotIn("discount_amount", names)
        self.assertNotIn("coupon", names)


# ── 16. Notification accuracy ───────────────────────────────────────────────
class NotificationTests(PaymentQABase):
    def test_16_partial_payment_email_states_paid_and_due(self):
        b = self.make_booking()
        mail.outbox.clear()
        self.settle(self.pay(b, "partial", "5000.00"))
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        body = msg.body
        html = msg.alternatives[0][0]
        self.assertIn("Paid: 5000.00 BDT", body)
        self.assertIn("Due: 4500.00 BDT", body)
        self.assertNotIn("PAID IN FULL", body)
        self.assertIn("Remaining balance", html)
        self.assertNotIn("PAID IN FULL", html)

    def test_16b_full_payment_email_says_paid_in_full(self):
        b = self.make_booking()
        mail.outbox.clear()
        self.settle(self.pay(b, "full"))
        body = mail.outbox[0].body
        self.assertIn("PAID IN FULL", body)
        self.assertIn("Due: 0.00 BDT", body)

    def test_16c_no_sms_channel_is_implemented(self):
        from apps.bookings.models import Invoice

        self.assertEqual(
            [c[0] for c in Invoice.SentVia.choices], ["email", "whatsapp"]
        )
        # WhatsApp is declared but there is no sender for it.
        import apps.bookings.invoices as inv

        self.assertFalse([n for n in dir(inv) if "whatsapp" in n.lower()])


# ── 7/12 (critical). Money in flight vs. room release (C1 — fixed) ──────────
class MoneyInFlightTests(PaymentQABase):
    def test_7c_room_is_never_resold_while_a_payment_is_in_flight(self):
        """C1 fix, deterministic form of the cron-vs-IPN race: however stale
        the hold gets, the expiry cron refuses to release a room with an
        unresolved PENDING payment — so the room cannot be resold and the
        customer's money, when it settles, lands on a live booking."""
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")

        # Age far past both the hold window and any session grace.
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        Payment.objects.filter(pk=p.pk).update(
            created_at=timezone.now() - timedelta(minutes=999)
        )
        call_command("expire_stale_bookings", verbosity=0)
        b.refresh_from_db()
        self.assertEqual(b.status, Booking.Status.PENDING)  # room NOT released

        # Nobody else can buy the room out from under the paying customer.
        r = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.pk,
                "rooms": [
                    {"room_id": self.room_4p.pk, "adult_count": 2, "kid_details": []}
                ],
                "customer_name": "Second Guest",
                "phone": "01800000000",
                "email": "second@example.com",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 409)

        # The first customer's 5000 BDT settles onto a LIVE booking.
        self.settle(p)
        p.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.SUCCESS)
        self.assertEqual(b.paid_amount, Decimal("5000.00"))
        self.assertEqual(b.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(
            BookingRoom.objects.filter(
                package=self.package, room=self.room_4p
            ).count(),
            1,  # exactly one booking for the room — never double-sold
        )

    def test_7d_money_on_a_dead_session_is_flagged_and_visible_to_staff(self):
        """H3 fix: when verified money lands on a closed session it is not
        credited (correct), and the condition is now first-class state the
        staff dashboard can see — not a JSON blob plus a log line."""
        b = self.make_booking()
        p = self.pay(b, "partial", "5000.00")
        # The session dies on a VERIFIED trigger (signed gateway IPN)...
        self.client.post(
            "/api/payments/ipn/",
            sign_ipn({"tran_id": p.transaction_id, "status": "FAILED"}),
        )
        p.refresh_from_db()
        self.assertEqual(p.status, Payment.Status.FAILED)
        # ...and the customer's money arrives anyway.
        self.settle(p)
        p.refresh_from_db()
        b.refresh_from_db()
        self.assertTrue(p.gateway_payload.get("requires_refund"))
        self.assertEqual(b.paid_amount, Decimal("0.00"))  # not credited
        self.assertTrue(b.refund_required)  # but never invisible
        self.assertIn("refund", b.refund_note.lower())

        self.client.force_authenticate(self.staff)
        r = self.client.get(f"/api/staff/payments/?booking={b.pk}")
        row = r.data["results"][0]
        self.assertTrue(row["gateway_payload"].get("requires_refund"))
        r = self.client.get("/api/staff/bookings/?refund_required=true")
        self.assertIn(b.booking_code, {x["booking_code"] for x in r.data["results"]})
