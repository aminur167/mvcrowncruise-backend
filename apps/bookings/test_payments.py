from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.testing import ThrottlelessTestMixin, create_booking, sign_ipn

from .models import Booking, BookingRoom, Payment
from .sslcommerz import GatewayError
from .test_api import build_fixtures

GATEWAY_URL = "https://sandbox.sslcommerz.com/gwprocess/testsession"


class PaymentTestCase(ThrottlelessTestMixin, APITestCase):
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

    def make_booking(self, room=None, adults=2, kids=None, rooms=None):
        # 4P, 2 adults: total = 3500 + 6000 = 9500. Pass rooms=[…] for a
        # multi-room booking; otherwise a single room is built from
        # room/adults/kids.
        if rooms is None:
            rooms = [
                {
                    "room": room or self.room_4p,
                    "adult_count": adults,
                    "kid_details": kids or [],
                }
            ]
        return create_booking(self.package, rooms=rooms)

    def initiate(self, booking, payload):
        with patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            return self.client.post(
                f"/api/bookings/{booking.booking_code}/pay/", payload, format="json"
            )

    def verdict(self, payment, **overrides):
        data = {
            "status": "VALID",
            "tran_id": payment.transaction_id,
            "val_id": "VAL123",
            "amount": str(payment.amount),
            "currency": "BDT",
            "card_type": "VISA",
        }
        data.update(overrides)
        return data

    def send_ipn(self, payment, verdict=None, ipn_status="VALID"):
        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            return_value=verdict or self.verdict(payment),
        ) as mock_validate:
            response = self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {
                        "tran_id": payment.transaction_id,
                        "val_id": "VAL123",
                        "status": ipn_status,
                    }
                ),
            )
        return response, mock_validate


class InitiatePaymentTests(PaymentTestCase):
    def test_full_payment_amount_is_server_side_due(self):
        booking = self.make_booking()
        response = self.initiate(booking, {"payment_type": "full", "amount": "1.00"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["gateway_url"], GATEWAY_URL)
        self.assertEqual(response.data["amount"], "9500.00")  # client "1.00" ignored
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        self.assertEqual(payment.amount, Decimal("9500.00"))
        self.assertEqual(payment.status, Payment.Status.PENDING)
        self.assertEqual(
            payment.transaction_id, f"{booking.booking_code}-P{payment.pk}"
        )

    def test_partial_payment_validations(self):
        booking = self.make_booking()
        cases = [
            ({"payment_type": "partial"}, "amount"),  # missing
            ({"payment_type": "partial", "amount": "0"}, "amount"),  # zero
            ({"payment_type": "partial", "amount": "-5"}, "amount"),  # negative
            ({"payment_type": "partial", "amount": "9500.01"}, "amount"),  # > due
            # Below the package's minimum first deposit (50% of 9500 = 4750).
            ({"payment_type": "partial", "amount": "4749.99"}, "amount"),
            ({"payment_type": "partial", "amount": "0.01"}, "amount"),
        ]
        for payload, field in cases:
            response = self.initiate(booking, payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(field, response.data)
        # Exactly the floor is accepted.
        ok = self.initiate(booking, {"payment_type": "partial", "amount": "4750"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data["amount"], "4750.00")

    def test_top_up_below_the_deposit_floor_is_allowed(self):
        """The min-deposit floor applies to the FIRST payment only — paying
        down an existing balance in small amounts must stay possible."""
        booking = self.make_booking()
        first = self.initiate(booking, {"payment_type": "partial", "amount": "5000"})
        self.assertEqual(first.status_code, 200)
        payment = Payment.objects.get(transaction_id=first.data["tran_id"])
        self.send_ipn(payment)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        top_up = self.initiate(booking, {"payment_type": "partial", "amount": "100"})
        self.assertEqual(top_up.status_code, 200)

    def test_cancelled_booking_cannot_pay(self):
        booking = self.make_booking()
        booking.status = Booking.Status.CANCELLED
        booking.save()
        response = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(response.status_code, 400)

    def test_gateway_failure_marks_payment_failed_and_502(self):
        booking = self.make_booking()
        with patch(
            "apps.bookings.sslcommerz.create_session",
            side_effect=GatewayError("store auth failed"),
        ):
            response = self.client.post(
                f"/api/bookings/{booking.booking_code}/pay/",
                {"payment_type": "full"},
                format="json",
            )
        self.assertEqual(response.status_code, 502)
        payment = Payment.objects.get(booking=booking)
        self.assertEqual(payment.status, Payment.Status.FAILED)

    def test_nothing_due_rejected(self):
        booking = self.make_booking()
        Payment.objects.create(
            booking=booking,
            amount=booking.total_amount,
            payment_type=Payment.PaymentType.FULL,
            status=Payment.Status.SUCCESS,
        )
        response = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(response.status_code, 400)


class ProcessResultTests(PaymentTestCase):
    def start_payment(self, booking, amount):
        response = self.initiate(
            booking, {"payment_type": "partial", "amount": str(amount)}
        )
        return Payment.objects.get(transaction_id=response.data["tran_id"])

    def test_valid_ipn_credits_payment_and_updates_booking(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        response, _ = self.send_ipn(payment)
        self.assertEqual(response.status_code, 200)

        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertIsNotNone(payment.paid_at)
        self.assertEqual(booking.paid_amount, Decimal("5000.00"))
        self.assertEqual(booking.due_amount, Decimal("4500.00"))
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)

    def test_second_partial_payment_reaches_fully_paid(self):
        booking = self.make_booking()
        first = self.start_payment(booking, "5000")
        self.send_ipn(first)
        second = self.start_payment(booking, "4500")
        self.send_ipn(second)

        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("9500.00"))
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)

    def test_duplicate_ipn_credits_only_once(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        verdict = self.verdict(payment)
        total_validations = 0
        for _ in range(5):
            _, mock_validate = self.send_ipn(payment, verdict=verdict)
            total_validations += mock_validate.call_count

        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("5000.00"))
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)
        self.assertEqual(total_validations, 1)  # 4 duplicates gated before validation

    def test_amount_tampering_rejected(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        response, _ = self.send_ipn(
            payment, verdict=self.verdict(payment, amount="1.00")
        )
        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(booking.paid_amount, Decimal("0.00"))
        self.assertEqual(booking.status, Booking.Status.PENDING)

    def test_invalid_verdict_rejected(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        self.send_ipn(payment, verdict=self.verdict(payment, status="INVALID"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("0.00"))

    def test_unknown_tran_id_credits_nothing(self):
        with patch("apps.bookings.sslcommerz.validate_payment") as mock_validate:
            response = self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {"tran_id": "BK-FAKE-P999", "val_id": "V1", "status": "VALID"}
                ),
            )
        self.assertEqual(response.status_code, 200)
        mock_validate.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    def test_failed_ipn_status_closes_pending_payment(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        self.client.post(
            "/api/payments/ipn/",
            sign_ipn(
                {
                    "tran_id": payment.transaction_id,
                    "val_id": "V1",
                    "status": "FAILED",
                }
            ),
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)

    def test_unsigned_failed_ipn_is_rejected(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        response = self.client.post(
            "/api/payments/ipn/",
            {"tran_id": payment.transaction_id, "val_id": "V1", "status": "FAILED"},
        )
        self.assertEqual(response.status_code, 400)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)

    def test_stray_fail_cannot_undo_verified_success(self):
        booking = self.make_booking()
        payment = self.start_payment(booking, "5000")
        self.send_ipn(payment)
        self.client.post(
            "/api/payments/fail/", {"tran_id": payment.transaction_id}
        )
        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(booking.paid_amount, Decimal("5000.00"))


class RedirectViewTests(PaymentTestCase):
    def test_success_redirect_processes_and_points_to_frontend(self):
        booking = self.make_booking()
        payment = Payment.objects.get(
            transaction_id=self.initiate(booking, {"payment_type": "full"}).data[
                "tran_id"
            ]
        )
        with patch(
            "apps.bookings.sslcommerz.validate_payment",
            return_value=self.verdict(payment),
        ):
            response = self.client.post(
                "/api/payments/success/",
                {"tran_id": payment.transaction_id, "val_id": "VAL123"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/payment/success?booking=", response.url)
        self.assertIn(booking.booking_code, response.url)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)

    def test_cancel_redirect_closes_payment_only_on_gateway_confirmation(self):
        booking = self.make_booking()
        payment = Payment.objects.get(
            transaction_id=self.initiate(booking, {"payment_type": "full"}).data[
                "tran_id"
            ]
        )
        # Gateway confirms the attempt was cancelled → payment is closed.
        with patch(
            "apps.bookings.sslcommerz.query_transaction",
            return_value=[{"status": "CANCELLED"}],
        ):
            response = self.client.post(
                "/api/payments/cancel/", {"tran_id": payment.transaction_id}
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/payment/cancel?booking=", response.url)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CANCELLED)

    def test_redirect_without_gateway_confirmation_leaves_payment_pending(self):
        """The fail/cancel redirect is attacker-controllable — with no
        gateway record confirming the session died, nothing changes."""
        booking = self.make_booking()
        payment = Payment.objects.get(
            transaction_id=self.initiate(booking, {"payment_type": "full"}).data[
                "tran_id"
            ]
        )
        with patch(
            "apps.bookings.sslcommerz.query_transaction", return_value=[]
        ):
            response = self.client.post(
                "/api/payments/fail/", {"tran_id": payment.transaction_id}
            )
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.PENDING)


class ExpireStaleBookingsTests(PaymentTestCase):
    def age_booking(self, booking, minutes):
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )

    def test_old_unpaid_pending_cancelled_and_room_freed(self):
        booking = self.make_booking()
        self.age_booking(booking, 60)
        call_command("expire_stale_bookings")
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertEqual(booking.status_logs.count(), 2)  # create + cancel

        freed_room = booking.rooms.first().room
        # Room is free again (cancelled booking's rooms went is_active=False) —
        # rebooking it must not raise the (package, room) unique constraint.
        create_booking(
            self.package,
            rooms=[{"room": freed_room, "adult_count": 1, "kid_details": []}],
            customer_name="Karim",
            phone="01800000000",
            email="karim@example.com",
        )

    def test_partially_paid_and_fresh_bookings_untouched(self):
        paid = self.make_booking()
        payment = self.start_payment_for(paid, "5000")
        self.send_ipn(payment)
        self.age_booking(paid, 60)

        fresh = self.make_booking(room=self.room_2p, adults=2)

        call_command("expire_stale_bookings")
        paid.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(paid.status, Booking.Status.PARTIALLY_PAID)
        self.assertEqual(fresh.status, Booking.Status.PENDING)

    def start_payment_for(self, booking, amount):
        response = self.initiate(
            booking, {"payment_type": "partial", "amount": amount}
        )
        return Payment.objects.get(transaction_id=response.data["tran_id"])


class GatewayCardDataStrippingTests(APITestCase):
    """Cardholder data must never reach Payment.gateway_payload / the staff API
    (Phase 8a, F4). Verified at the real gateway boundary: mock only the HTTP
    round-trip, so the stripping in validate_payment/query_transaction runs."""

    def test_validate_payment_strips_card_fields(self):
        raw = {
            "status": "VALID",
            "tran_id": "BK-DEADBEEF-P1",
            "val_id": "VAL123",
            "amount": "9500.00",
            "currency": "BDT",
            # Cardholder data SSLCommerz returns — must be dropped.
            "card_no": "418117XXXXXX5075",
            "card_issuer": "BRAC BANK",
            "card_brand": "VISA",
            "card_type": "VISA-Dutch Bangla",
        }
        with patch("apps.bookings.sslcommerz.requests.get") as mock_get:
            mock_get.return_value.json.return_value = raw
            mock_get.return_value.raise_for_status.return_value = None
            from apps.bookings import sslcommerz

            data = sslcommerz.validate_payment("VAL123")

        # Operational fields the verdict check relies on survive untouched...
        for key in ("status", "tran_id", "val_id", "amount", "currency"):
            self.assertEqual(data[key], raw[key])
        # ...card fields are gone.
        for key in ("card_no", "card_issuer", "card_brand", "card_type"):
            self.assertNotIn(key, data)

    def test_query_transaction_strips_card_fields_from_each_attempt(self):
        raw = {
            "APIConnect": "DONE",
            "no_of_trans_found": 1,
            "element": {
                "status": "VALID",
                "tran_id": "BK-DEADBEEF-P1",
                "val_id": "VAL123",
                "amount": "9500.00",
                "currency": "BDT",
                "card_no": "418117XXXXXX5075",
                "card_issuer": "BRAC BANK",
            },
        }
        with patch("apps.bookings.sslcommerz.requests.get") as mock_get:
            mock_get.return_value.json.return_value = raw
            mock_get.return_value.raise_for_status.return_value = None
            from apps.bookings import sslcommerz

            attempts = sslcommerz.query_transaction("BK-DEADBEEF-P1")

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "VALID")
        self.assertNotIn("card_no", attempts[0])
        self.assertNotIn("card_issuer", attempts[0])
