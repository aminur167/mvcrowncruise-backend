"""Reconciling the refund register against what SSLCommerz actually did.

The register records what STAFF did — they asked the gateway for a refund, and
that is all they can do. Whether the money arrived is decided days later by the
issuing bank, and SSLCommerz never tells us. These tests pin the three answers
it can give, and in particular the one that used to be invisible: a refund the
gateway CANCELLED, which left our books saying a customer was paid when they
were not.
"""

from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.bookings.sslcommerz import GatewayError
from apps.bookings.test_api import build_fixtures
from apps.refunds.gateway import (
    apply_gateway_status,
    pending_gateway_refunds,
    poll_gateway_refunds,
)
from apps.refunds.models import GatewayRefundStatus, PayoutMethod, Refund
from apps.testing import ThrottlelessTestMixin, create_booking

REF = "R260918030540-73051"


class GatewayRefundPollingTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        _, _, _, self.room, self.other_room, self.package = build_fixtures(
            ship_name="Refund Ship"
        )
        self.booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )

    def make_refund(self, **kwargs):
        defaults = dict(
            booking=self.booking,
            reason=Refund.Reason.CUSTOMER_CANCELLATION,
            amount=Decimal("10.00"),
            status=Refund.Status.PAID,
            method=PayoutMethod.GATEWAY,
            reference_no=REF,
        )
        defaults.update(kwargs)
        return Refund.objects.create(**defaults)

    # ---- what gets asked about -------------------------------------------

    def test_a_hand_settled_payout_is_never_asked_about(self):
        """bKash and bank transfers are complete the moment staff record them.
        There is no gateway to ask, and asking would be a wasted request."""
        self.make_refund(method=PayoutMethod.BKASH, reference_no="TRX123")
        self.assertEqual(pending_gateway_refunds().count(), 0)

    def test_a_gateway_refund_with_no_reference_is_skipped(self):
        """Without SSLCommerz's refund_ref_id there is nothing to look up."""
        self.make_refund(reference_no="")
        self.assertEqual(pending_gateway_refunds().count(), 0)

    def test_a_finished_refund_is_not_asked_about_again(self):
        self.make_refund(gateway_refund_status=GatewayRefundStatus.REFUNDED)
        self.assertEqual(pending_gateway_refunds().count(), 0)

    def test_a_cancelled_refund_is_not_asked_about_again(self):
        self.make_refund(gateway_refund_status=GatewayRefundStatus.CANCELLED)
        self.assertEqual(pending_gateway_refunds().count(), 0)

    def test_an_unanswered_refund_is_asked_about(self):
        refund = self.make_refund()
        self.assertEqual(list(pending_gateway_refunds()), [refund])

    # ---- the three answers -----------------------------------------------

    def test_processing_records_the_wait_without_claiming_payment(self):
        refund = self.make_refund()
        apply_gateway_status(refund, {"status": "processing", "refunded_on": ""})
        refund.refresh_from_db()
        self.assertEqual(refund.gateway_refund_status, GatewayRefundStatus.PROCESSING)
        self.assertIsNone(refund.gateway_refunded_at)
        # The register still says PAID — staff did their part — but the money
        # is demonstrably not back yet, and that is now visible.
        self.assertEqual(refund.status, Refund.Status.PAID)
        self.assertTrue(refund.awaiting_gateway)

    def test_refunded_records_when_the_money_actually_landed(self):
        refund = self.make_refund()
        apply_gateway_status(
            refund, {"status": "refunded", "refunded_on": "2026-09-20 14:30:00"}
        )
        refund.refresh_from_db()
        self.assertEqual(refund.gateway_refund_status, GatewayRefundStatus.REFUNDED)
        self.assertIsNotNone(refund.gateway_refunded_at)
        self.assertEqual(
            timezone.localtime(refund.gateway_refunded_at).strftime("%Y-%m-%d %H:%M"),
            "2026-09-20 14:30",
        )
        self.assertFalse(refund.awaiting_gateway)

    def test_refunded_without_a_date_still_counts_as_refunded(self):
        """The gateway is the authority on whether it happened; a missing
        timestamp must not leave a completed refund looking unfinished."""
        refund = self.make_refund()
        apply_gateway_status(refund, {"status": "refunded", "refunded_on": "In processing"})
        refund.refresh_from_db()
        self.assertEqual(refund.gateway_refund_status, GatewayRefundStatus.REFUNDED)
        self.assertIsNotNone(refund.gateway_refunded_at)

    def test_cancelled_is_recorded_loudly(self):
        """The case this whole module exists for: our books say paid, the
        gateway says no."""
        refund = self.make_refund()
        with self.assertLogs("apps.refunds.gateway", level="ERROR") as logs:
            apply_gateway_status(
                refund, {"status": "cancelled", "errorReason": "Insufficient balance"}
            )
        refund.refresh_from_db()
        self.assertEqual(refund.gateway_refund_status, GatewayRefundStatus.CANCELLED)
        self.assertTrue(refund.gateway_cancelled)
        self.assertTrue(refund.awaiting_gateway)
        self.assertIn("CANCELLED", "".join(logs.output))
        self.assertIn(self.booking.booking_code, "".join(logs.output))

    def test_an_unknown_status_is_left_alone_rather_than_guessed(self):
        """A refund wrongly marked complete is worse than one left
        unconfirmed, so anything unrecognised changes nothing but the clock."""
        refund = self.make_refund()
        with self.assertLogs("apps.refunds.gateway", level="WARNING"):
            apply_gateway_status(refund, {"status": "something_new"})
        refund.refresh_from_db()
        self.assertEqual(refund.gateway_refund_status, "")
        self.assertTrue(refund.awaiting_gateway)
        self.assertIsNotNone(refund.gateway_checked_at)

    def test_the_gateways_answer_is_kept_verbatim(self):
        refund = self.make_refund()
        answer = {"status": "processing", "refund_ref_id": REF, "bank_tran_id": "BGT77"}
        apply_gateway_status(refund, answer)
        refund.refresh_from_db()
        self.assertEqual(refund.gateway_payload["bank_tran_id"], "BGT77")

    # ---- the job ---------------------------------------------------------

    def test_the_job_reports_what_it_found(self):
        self.make_refund()
        with patch(
            "apps.refunds.gateway.query_refund_status",
            return_value={"status": "refunded", "refunded_on": "2026-09-20 10:00:00"},
        ):
            checked, refunded, cancelled, failed = poll_gateway_refunds()
        self.assertEqual((checked, refunded, cancelled, failed), (1, 1, 0, 0))

    def test_one_unreachable_refund_does_not_stop_the_rest(self):
        """A gateway that times out on the first of twenty would otherwise
        leave nineteen unasked."""
        first = self.make_refund(reference_no="R-FIRST")
        second_booking = create_booking(
            self.package, [{"room": self.other_room, "adult_count": 1}]
        )
        second = Refund.objects.create(
            booking=second_booking,
            reason=Refund.Reason.OVERPAYMENT,
            amount=Decimal("5.00"),
            status=Refund.Status.PAID,
            method=PayoutMethod.GATEWAY,
            reference_no="R-SECOND",
        )

        def answer(ref_id):
            if ref_id == "R-FIRST":
                raise GatewayError("upstream timeout")
            return {"status": "refunded", "refunded_on": "2026-09-20 10:00:00"}

        with patch("apps.refunds.gateway.query_refund_status", side_effect=answer):
            with self.assertLogs("apps.refunds.gateway", level="WARNING"):
                checked, refunded, cancelled, failed = poll_gateway_refunds()

        self.assertEqual((checked, refunded, failed), (1, 1, 1))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.gateway_refund_status, "")
        self.assertEqual(second.gateway_refund_status, GatewayRefundStatus.REFUNDED)

    def test_a_failure_leaves_the_refund_to_be_asked_about_again(self):
        refund = self.make_refund()
        with patch(
            "apps.refunds.gateway.query_refund_status",
            side_effect=GatewayError("down"),
        ):
            with self.assertLogs("apps.refunds.gateway", level="WARNING"):
                poll_gateway_refunds()
        self.assertIn(refund, list(pending_gateway_refunds()))
