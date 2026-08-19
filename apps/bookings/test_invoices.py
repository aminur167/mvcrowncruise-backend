import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from .models import Booking, Invoice, Payment
from .test_payments import PaymentTestCase

TEMP_MEDIA = tempfile.mkdtemp(prefix="mv_alaska_test_media_")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class InvoiceFlowTests(PaymentTestCase):
    def pay(self, booking, amount):
        """Initiate + settle a partial payment, running on_commit hooks."""
        response = self.initiate(
            booking, {"payment_type": "partial", "amount": str(amount)}
        )
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        with self.captureOnCommitCallbacks(execute=True):
            self.send_ipn(payment)
        return payment

    def test_payment_success_creates_and_emails_invoice(self):
        booking = self.make_booking()  # total 9500
        self.pay(booking, "5000")

        invoice = Invoice.objects.get(booking=booking)
        self.assertIsNotNone(invoice.sent_at)
        self.assertTrue(invoice.pdf_file.name)
        with invoice.pdf_file.open("rb") as f:
            self.assertTrue(f.read().startswith(b"%PDF"))

        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [booking.email])
        self.assertIn(booking.booking_code, email.subject)
        self.assertIn("Due: 4500.00 BDT", email.body)
        filename, content, mimetype = email.attachments[0]
        self.assertTrue(filename.endswith(".pdf"))
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(mimetype, "application/pdf")

    def test_each_partial_payment_sends_updated_invoice(self):
        booking = self.make_booking()
        self.pay(booking, "5000")
        self.pay(booking, "4500")

        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 2)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Due: 4500.00 BDT", mail.outbox[0].body)
        self.assertIn("Due: 0.00 BDT", mail.outbox[1].body)
        self.assertIn("PAID IN FULL", mail.outbox[1].body)

    def test_duplicate_ipn_sends_single_invoice(self):
        booking = self.make_booking()
        response = self.initiate(
            booking, {"payment_type": "partial", "amount": "5000"}
        )
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        verdict = self.verdict(payment)
        for _ in range(3):
            with self.captureOnCommitCallbacks(execute=True):
                self.send_ipn(payment, verdict=verdict)

        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_email_failure_keeps_payment_success_and_invoice_unsent(self):
        booking = self.make_booking()
        with patch(
            "apps.bookings.invoices.EmailMultiAlternatives.send",
            side_effect=Exception("smtp down"),
        ):
            payment = self.pay(booking, "5000")

        payment.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertEqual(booking.paid_amount, Decimal("5000.00"))
        invoice = Invoice.objects.get(booking=booking)
        self.assertIsNone(invoice.sent_at)
        self.assertTrue(invoice.pdf_file.name)  # PDF still generated

    def test_send_unsent_invoices_command_retries(self):
        booking = self.make_booking()
        with patch(
            "apps.bookings.invoices.EmailMultiAlternatives.send",
            side_effect=Exception("smtp down"),
        ):
            self.pay(booking, "5000")
        self.assertEqual(len(mail.outbox), 0)

        call_command("send_unsent_invoices")

        invoice = Invoice.objects.get(booking=booking)
        self.assertIsNotNone(invoice.sent_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_bengali_text_renders_without_crash(self):
        booking = self.make_booking()
        booking.customer_name = "à¦°à¦¹à¦¿à¦® à¦‰à¦¦à§à¦¦à¦¿à¦¨"
        booking.save()
        self.pay(booking, str(booking.due_amount))
        invoice = Invoice.objects.get(booking=booking)
        with invoice.pdf_file.open("rb") as f:
            self.assertTrue(f.read().startswith(b"%PDF"))


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class CutoffPaymentPolicyTests(PaymentTestCase):
    def test_existing_booking_can_pay_after_cutoff(self):
        """Locked policy: cutoff gates booking CREATION, never payment of an
        existing booking's due (the guide even collects dues on the ship)."""
        booking = self.make_booking()
        self.package.booking_cutoff_datetime = timezone.now() - timezone.timedelta(
            hours=1
        )
        self.package.save()

        response = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(response.status_code, 200)

    def test_new_booking_after_cutoff_still_rejected(self):
        self.package.booking_cutoff_datetime = timezone.now() - timezone.timedelta(
            hours=1
        )
        self.package.save()
        response = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.id,
                "customer_name": "Karim",
                "phone": "01800000000",
                "email": "karim@example.com",
                "rooms": [
                    {"room_id": self.room_2p.id, "adult_count": 1, "kid_details": []}
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
