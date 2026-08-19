"""Relocate legacy invoice PDFs from their guessable filenames to token paths.

0005 gave every invoice an access_token and pointed `upload_to` at it, but the
FILES already on disk kept their old names — invoices/INV-<booking_code>-<pk>.pdf
— which is exactly the enumerable path QA C1 is about. The URLconf blocks
/media/invoices/ so nothing is reachable through Django, but the objects would
still sit at guessable keys under any other static server (nginx, S3), so move
them.

Copy-then-repoint-then-delete: if anything fails half-way the row still points
at a file that exists.
"""

from django.db import migrations


def move_files(apps_registry, schema_editor):
    Invoice = apps_registry.get_model("bookings", "Invoice")
    for invoice in Invoice.objects.exclude(pdf_file="").exclude(access_token=""):
        old_name = invoice.pdf_file.name
        new_name = f"invoices/{invoice.access_token}.pdf"
        if old_name == new_name:
            continue
        storage = invoice.pdf_file.storage
        if not storage.exists(old_name):
            continue
        with storage.open(old_name, "rb") as fh:
            saved = storage.save(new_name, fh)
        invoice.pdf_file.name = saved
        invoice.save(update_fields=["pdf_file"])
        storage.delete(old_name)


def noop(apps_registry, schema_editor):
    # Not reversed: the old names are the vulnerability we are removing.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0005_invoicecounter_booking_price_snapshot_and_more"),
    ]

    operations = [migrations.RunPython(move_files, noop)]
