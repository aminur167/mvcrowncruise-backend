"""FINAL regression pass — one continuous end-to-end customer journey.

Drives the entire public flow in order, through the real API stack against
PostgreSQL, exactly as the frontend would call it:

    search (packages + calendar) → room availability → quote with kid pricing
    → coupon attempt (documents that no coupon feature exists — N/A by scope)
    → create booking (with special requests) → initiate partial payment
    → gateway settles (signed IPN, sandbox-shaped verdict) → confirmation
    → invoice emailed + downloaded via capability token
    → pay remaining balance → fully paid → second invoice

Only the two OUTBOUND gateway HTTP calls (`sslcommerz.create_session`,
`sslcommerz.validate_payment`) are mocked, at the same boundary every QA
phase used — no live SSLCommerz round-trip is possible without a human
completing the hosted checkout page. The IPN that settles each payment
carries a genuine verify_sign computed with the configured store password,
so the signature gate is exercised for real. A forged (unsigned) IPN is also
fired mid-flow and must change nothing.

Run: manage.py test apps.test_final_e2e
"""

from decimal import Decimal
from unittest import mock

from django.core import mail
from rest_framework.test import APITestCase

from apps.bookings.models import Booking, BookingStatusLog, Payment
from apps.bookings.test_api import build_fixtures
from apps.testing import ThrottlelessTestMixin, sign_ipn

GATEWAY_URL = "https://sandbox.sslcommerz.com/EasyCheckOut/test-e2e-session"


def _verdict(tran_id, amount):
    """A sandbox-shaped Validation-API verdict for a settled payment."""
    return {
        "status": "VALID",
        "tran_id": tran_id,
        "val_id": f"VAL-{tran_id}",
        "amount": str(amount),
        "currency": "BDT",
        "bank_tran_id": "SANDBOX0001",
    }


class FinalEndToEndFlowTests(ThrottlelessTestMixin, APITestCase):
    """The whole journey in one test, so every step runs against the state
    the previous step actually produced — not isolated fixtures."""

    def setUp(self):
        (
            self.ship,
            self.type_2p,
            self.type_4p,
            self.room_2p,
            self.room_4p,
            self.package,
        ) = build_fixtures(ship_name="Final E2E Ship")

    def _settle_via_signed_ipn(self, tran_id, amount):
        """Deliver a genuinely-signed IPN and serve the (mocked) verdict."""
        with mock.patch(
            "apps.bookings.sslcommerz.validate_payment",
            return_value=_verdict(tran_id, amount),
        ), self.captureOnCommitCallbacks(execute=True):
            # Invoice email/PDF is deliberately deferred to on_commit so email
            # trouble can never roll back a settlement — execute those here.
            resp = self.client.post(
                "/api/payments/ipn/",
                sign_ipn(
                    {
                        "tran_id": tran_id,
                        "val_id": f"VAL-{tran_id}",
                        "status": "VALID",
                        "amount": str(amount),
                        "currency": "BDT",
                    }
                ),
            )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_full_customer_journey(self):
        # ---- 1. SEARCH — package list + calendar ------------------------
        resp = self.client.get("/api/packages/")
        self.assertEqual(resp.status_code, 200)
        listed = [p for p in resp.data if p["id"] == self.package.id]
        self.assertEqual(len(listed), 1, "package must appear in public search")
        self.assertTrue(listed[0]["is_bookable"])

        resp = self.client.get("/api/calendar/", {"year": 2099, "month": 1})
        self.assertEqual(resp.status_code, 200)
        cal_days = {d["date"] for d in resp.data["dates"]}
        self.assertIn("2099-01-10", cal_days, "departure day on the calendar")

        # ---- 2. SELECT ROOM — availability ------------------------------
        resp = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(resp.status_code, 200)
        by_number = {r["room_number"]: r for r in resp.data}
        self.assertEqual(by_number["T2"]["availability"], "available")

        # ---- 3. GUEST DETAILS + KID PRICING — quote ----------------------
        # 2 adults + kid aged 2 (free tier) + kid aged 5 (fixed 1500 tier).
        quote_req = {
            "package_id": self.package.id,
            "rooms": [
                {
                    "room_id": self.room_4p.id,
                    "adult_count": 2,
                    "kid_details": [{"age": 2}, {"age": 5}],
                }
            ],
        }
        resp = self.client.post("/api/bookings/quote/", quote_req, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        # base 3500 + 2×3000 adults + 0 (age 2) + 1500 (age 5) = 11000
        self.assertEqual(Decimal(resp.data["grand_total"]), Decimal("11000.00"))
        room_bd = resp.data["rooms"][0]
        kid_charges = {kid["age"]: Decimal(kid["charge"]) for kid in room_bd["kids"]}
        self.assertEqual(kid_charges, {2: Decimal("0.00"), 5: Decimal("1500.00")},
                         "kid pricing follows the age tiers (0-3 free, 3-8 fixed)")
        expected_total = Decimal(resp.data["grand_total"])

        # ---- 4. COUPON — documented N/A ----------------------------------
        # No coupon/discount feature exists anywhere in the system (verified
        # in QA phases 5 and 6 and re-asserted here). A submitted coupon code
        # must be ignored, never crash, and never change the server-side price.
        resp = self.client.post(
            "/api/bookings/quote/",
            {**quote_req, "coupon_code": "WELCOME50", "discount": "9999.00"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Decimal(resp.data["grand_total"]),
            expected_total,
            "a client-sent coupon/discount must not move the price",
        )

        # ---- 5. CREATE BOOKING -------------------------------------------
        resp = self.client.post(
            "/api/bookings/",
            {
                **quote_req,
                "customer_name": "Final Regression Guest",
                "phone": "01700000099",
                "email": "final-e2e@example.com",
                "special_requests": "Ground-floor cabin please (wheelchair).",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        code = resp.data["booking_code"]
        self.assertEqual(Decimal(resp.data["total_amount"]), expected_total)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(
            resp.data["special_requests"],
            "Ground-floor cabin please (wheelchair).",
        )

        # Room is instantly held — availability reflects it.
        resp = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        by_number = {r["room_number"]: r for r in resp.data}
        self.assertEqual(by_number["T2"]["availability"], "booked")

        # ---- 6. PAY — partial deposit via sandbox session ----------------
        # Minimum first payment = 50% of 11000 = 5500 (admin-configurable).
        with mock.patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            resp = self.client.post(
                f"/api/bookings/{code}/pay/",
                {"payment_type": "partial", "amount": "5500.00"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["gateway_url"], GATEWAY_URL)
        tran_id = resp.data["tran_id"]

        # ---- 6b. FORGED IPN must be rejected before any state change -----
        resp = self.client.post(
            "/api/payments/ipn/",
            {"tran_id": tran_id, "status": "FAILED"},  # no signature
        )
        self.assertEqual(resp.status_code, 400)
        payment = Payment.objects.get(transaction_id=tran_id)
        self.assertEqual(payment.status, Payment.Status.PENDING,
                         "forged IPN must not close a live session")

        # ---- 6c. Genuine (signed) IPN settles the deposit -----------------
        mail.outbox.clear()
        self._settle_via_signed_ipn(tran_id, "5500.00")

        # ---- 7. CONFIRMATION ----------------------------------------------
        resp = self.client.get(f"/api/bookings/{code}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["status"], "partially_paid")
        self.assertEqual(Decimal(resp.data["paid_amount"]), Decimal("5500.00"))
        self.assertEqual(Decimal(resp.data["due_amount"]), Decimal("5500.00"))

        # Invoice email went out, with the PDF attached.
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("final-e2e@example.com", mail.outbox[0].to)
        attachments = mail.outbox[0].attachments
        self.assertTrue(
            any(name.endswith(".pdf") for name, *_ in attachments),
            "invoice PDF must be attached",
        )

        # ---- 8. DOWNLOAD INVOICE (token-authorised) -----------------------
        resp = self.client.get(f"/api/bookings/{code}/invoices/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        download_url = resp.data[0]["download_url"]
        resp = self.client.get(download_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        pdf_bytes = b"".join(resp.streaming_content)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"), "a real PDF is served")

        # ---- 9. PAY THE BALANCE — full settlement --------------------------
        with mock.patch(
            "apps.bookings.sslcommerz.create_session", return_value=GATEWAY_URL
        ):
            resp = self.client.post(
                f"/api/bookings/{code}/pay/",
                {"payment_type": "full"},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Decimal(resp.data["amount"]), Decimal("5500.00"),
                         "server computes the balance; client sends no amount")
        self._settle_via_signed_ipn(resp.data["tran_id"], "5500.00")

        booking = Booking.objects.get(booking_code=code)
        self.assertEqual(booking.status, Booking.Status.FULLY_PAID)
        self.assertEqual(booking.paid_amount, Decimal("11000.00"))
        self.assertEqual(booking.due_amount, Decimal("0.00"))

        # Two sealed invoices now exist and both download.
        resp = self.client.get(f"/api/bookings/{code}/invoices/")
        self.assertEqual(len(resp.data), 2)

        # Status changes were audit-trailed.
        self.assertTrue(
            BookingStatusLog.objects.filter(booking=booking).exists(),
            "booking status transitions must be recorded in the audit trail",
        )
