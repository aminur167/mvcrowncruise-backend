"""Retry invoices whose email never went out (sent_at is NULL).

Run periodically alongside expire_stale_bookings (Railway cron). Regenerates
the PDF if the file is missing (e.g. after a redeploy on ephemeral storage).
"""

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from apps.bookings.invoices import generate_invoice_pdf, send_invoice_email
from apps.bookings.models import Invoice


class Command(BaseCommand):
    help = "Send invoice emails that previously failed (sent_at IS NULL)."

    def handle(self, *args, **options):
        pending = Invoice.objects.filter(
            sent_at__isnull=True, sent_via=Invoice.SentVia.EMAIL
        ).select_related("booking")
        sent = failed = 0
        for invoice in pending:
            try:
                if not invoice.pdf_file or not invoice.pdf_file.storage.exists(
                    invoice.pdf_file.name
                ):
                    # Regenerating is safe: the PDF renders from the invoice's
                    # own frozen figures + the booking's price snapshot, so a
                    # re-render years later is byte-for-byte the same document.
                    invoice.pdf_file.save(
                        f"{invoice.number}.pdf",
                        ContentFile(generate_invoice_pdf(invoice)),
                        save=True,
                    )
                send_invoice_email(invoice)
                sent += 1
                self.stdout.write(f"sent {invoice.number}")
            except Exception as exc:  # keep going; cron retries next run
                failed += 1
                self.stderr.write(f"failed {invoice.number}: {exc}")
        self.stdout.write(self.style.SUCCESS(f"{sent} sent, {failed} failed."))
