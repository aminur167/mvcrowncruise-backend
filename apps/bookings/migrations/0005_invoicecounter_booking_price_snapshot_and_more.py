"""Phase 6 QA fixes — invoices become immutable, numbered financial records.

Schema:
- Booking.price_snapshot — the itemisation frozen at pricing time, so an
  invoice's line items no longer get recomputed from today's (admin-editable)
  pricing rules and stop summing to what was charged (M1).
- Invoice.{number, access_token} — the number is issued and stored (gapless
  per-year series) instead of being derived from the pk at render time; the
  token is an unguessable capability that names the PDF on disk and authorises
  the download, replacing a path derivable from the booking code + a sequential
  integer (C1).
- Invoice.{total,paid,due,booking_status} — the money the invoice attests to,
  frozen at issue time, so the PDF and its covering email can never disagree
  (M2) and a cancelled booking's invoice cannot claim "PAID IN FULL" (M3).
- Invoice.payment — what the invoice is an invoice *for*.

The unique fields are added non-unique, backfilled, and only then constrained:
existing rows would all collide on "" otherwise.
"""

import secrets
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models

import apps.bookings.models


def backfill(apps_registry, schema_editor):
    Booking = apps_registry.get_model("bookings", "Booking")
    Invoice = apps_registry.get_model("bookings", "Invoice")
    InvoiceCounter = apps_registry.get_model("bookings", "InvoiceCounter")

    # --- Bookings: reconstruct the price snapshot from what was charged.
    # The live pricing rules may already have moved, so we cannot re-price:
    # anything we cannot derive exactly is left empty and the invoice falls
    # back to a single "Package charges" line for the stored total, which is
    # always correct even if it is not itemised.
    for booking in Booking.objects.select_related("room__room_type", "package"):
        if booking.price_snapshot:
            continue
        try:
            room_base = booking.room.room_type.base_price
            adult_price = booking.package.adult_price
            adults_subtotal = adult_price * booking.adult_count
            kids_subtotal = booking.total_amount - room_base - adults_subtotal
        except Exception:
            continue
        # Only trust the reconstruction if it reconciles to the stored total
        # and the residual kid charge is sane.
        if kids_subtotal < 0:
            continue
        kid_details = booking.kid_details or []
        if kid_details and kids_subtotal == 0:
            kids = [{"age": kid.get("age"), "charge": "0.00"} for kid in kid_details]
        elif len(kid_details) == 1:
            kids = [{"age": kid_details[0].get("age"), "charge": str(kids_subtotal)}]
        elif not kid_details and kids_subtotal == 0:
            kids = []
        else:
            # Several kids sharing an unknown split — don't invent per-kid
            # figures on a financial document.
            continue
        booking.price_snapshot = {
            "room_base": str(room_base),
            "adult_price": str(adult_price),
            "adult_count": booking.adult_count,
            "adults_subtotal": str(adults_subtotal),
            "kids": kids,
            "kids_subtotal": str(kids_subtotal),
            "total": str(booking.total_amount),
        }
        booking.save(update_fields=["price_snapshot"])

    # --- Invoices: issue a number + token, and freeze the money. The booking's
    # CURRENT figures are the best available estimate of what an old invoice
    # said; they are exact for the common case (the latest invoice).
    counters = {}
    for invoice in Invoice.objects.select_related("booking").order_by("pk"):
        changed = []
        if not invoice.access_token:
            invoice.access_token = secrets.token_urlsafe(32)
            changed.append("access_token")
        if not invoice.number:
            year = invoice.created_at.year
            if year not in counters:
                counter, _ = InvoiceCounter.objects.get_or_create(year=year)
                counters[year] = counter
            counter = counters[year]
            counter.last_number += 1
            counter.save(update_fields=["last_number"])
            invoice.number = f"INV-{year}-{counter.last_number:05d}"
            changed.append("number")
        booking = invoice.booking
        invoice.total_amount = booking.total_amount
        invoice.paid_amount = booking.paid_amount
        invoice.due_amount = booking.due_amount
        invoice.booking_status = booking.status
        changed += ["total_amount", "paid_amount", "due_amount", "booking_status"]
        invoice.save(update_fields=changed)


def noop(apps_registry, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0004_payment_gateway_url_and_reconcile_escalation"),
    ]

    operations = [
        migrations.CreateModel(
            name="InvoiceCounter",
            fields=[
                (
                    "year",
                    models.PositiveIntegerField(primary_key=True, serialize=False),
                ),
                ("last_number", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.AddField(
            model_name="booking",
            name="price_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        # Added WITHOUT unique so existing rows (all "") don't collide.
        migrations.AddField(
            model_name="invoice",
            name="access_token",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="invoice",
            name="number",
            field=models.CharField(blank=True, editable=False, max_length=40),
        ),
        migrations.AddField(
            model_name="invoice",
            name="booking_status",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="invoice",
            name="due_amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="paid_amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="total_amount",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="payment",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="invoices",
                to="bookings.payment",
            ),
        ),
        migrations.AlterField(
            model_name="invoice",
            name="pdf_file",
            field=models.FileField(
                blank=True, upload_to=apps.bookings.models.invoice_pdf_path
            ),
        ),
        migrations.RunPython(backfill, noop),
        # Now that every row has a value, enforce uniqueness.
        migrations.AlterField(
            model_name="invoice",
            name="access_token",
            field=models.CharField(
                blank=True, editable=False, max_length=64, unique=True
            ),
        ),
        migrations.AlterField(
            model_name="invoice",
            name="number",
            field=models.CharField(
                blank=True, editable=False, max_length=40, unique=True
            ),
        ),
    ]
