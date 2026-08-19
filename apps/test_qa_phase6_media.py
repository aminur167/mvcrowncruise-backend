"""QA Phase 6 — invoice PDF access control (media serving).

The invoice PDF is a FileField under MEDIA_ROOT. Whether it is reachable — and
whether reaching it requires any authorisation — is decided entirely by the
`if settings.DEBUG` branch in config/urls.py, which is evaluated at import time.
These probes resolve the media path against a URLconf rebuilt under each
setting, which is the only way to observe both branches in-process.
"""

import importlib
import tempfile

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import Resolver404, clear_url_caches, get_resolver, reverse

from apps.bookings.models import Invoice, Payment
from apps.bookings.test_payments import PaymentTestCase

TEMP_MEDIA = tempfile.mkdtemp(prefix="qa_phase6_media2_")

MEDIA_PATH = "media/invoices/INV-BK-ABCD1234-7.pdf"


def resolve_under_debug(debug):
    """Rebuild config.urls with the given DEBUG and resolve the media path."""
    original = settings.DEBUG
    try:
        settings.DEBUG = debug
        import config.urls

        importlib.reload(config.urls)
        clear_url_caches()
        try:
            return get_resolver("config.urls").resolve(f"/{MEDIA_PATH}")
        except Resolver404:
            return None
    finally:
        settings.DEBUG = original
        import config.urls

        importlib.reload(config.urls)
        clear_url_caches()


class InvoiceMediaRoutingTests(SimpleTestCase):
    def test_6e_FIXED_invoices_are_never_served_as_static_media(self):
        """C1. Under DEBUG the invoice PDF resolved to django.views.static.serve
        — a bare static file view with no permission check of ANY kind — and
        without DEBUG it 404'd, so the staff link was simply dead. There was no
        configuration in which it was both functional and access-controlled.

        invoices/ is now shadowed with a 404 ahead of the static handler in
        BOTH configurations, so the only way to a PDF is an authenticated
        (staff) or token-bearing (customer) endpoint."""
        for debug in (True, False):
            match = resolve_under_debug(debug)
            if match is None:
                continue  # no route at all — also fine
            view = match.func
            self.assertNotEqual(
                f"{view.__module__}.{view.__name__}",
                "django.views.static.serve",
                f"invoice PDFs are served as unguarded static media (DEBUG={debug})",
            )

    def test_6e2_FIXED_the_authenticated_routes_exist_in_every_config(self):
        """The two real ways to an invoice PDF, neither of which depends on
        DEBUG: the staff endpoint (IsAdminUser) and the customer's token link."""
        self.assertTrue(reverse("staff-invoice-pdf", kwargs={"pk": 1}))
        self.assertTrue(reverse("invoice-download", kwargs={"token": "abc"}))


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class InvoicePdfUrlTests(PaymentTestCase):
    def settle(self, booking, amount):
        response = self.initiate(
            booking, {"payment_type": "partial", "amount": str(amount)}
        )
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        with self.captureOnCommitCallbacks(execute=True):
            self.send_ipn(payment)
        return payment

    def test_6e3_FIXED_pdf_path_carries_entropy_and_pdf_url_is_an_api_route(self):
        """C1. The stored path was invoices/INV-<booking_code>-<invoice_pk>.pdf:
        booking_code is not a secret (it is in the customer's own confirmation
        URL, their email, the gateway redirect and their browser history) and
        invoice_pk is a small sequential integer — so the file was both
        constructable and enumerable across customers.

        The path now carries the invoice's 256-bit capability token, and
        pdf_url points at the authenticated staff endpoint rather than at the
        file."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = Invoice.objects.filter(booking=booking).latest("pk")

        self.assertEqual(
            invoice.pdf_file.name, f"invoices/{invoice.access_token}.pdf"
        )
        self.assertNotIn(booking.booking_code, invoice.pdf_file.name)
        self.assertNotIn(str(invoice.pk), invoice.pdf_file.name.replace(".pdf", ""))

        from apps.staff.serializers import StaffInvoiceSerializer

        url = StaffInvoiceSerializer(invoice).data["pdf_url"]
        self.assertEqual(url, f"/api/staff/invoices/{invoice.pk}/pdf/")
