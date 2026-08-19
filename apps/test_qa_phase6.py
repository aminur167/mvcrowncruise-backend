"""QA Phase 6 — Invoice / PDF generation probes.

Drives the real invoice code (apps.bookings.invoices) end-to-end and reads the
generated PDFs back with PyMuPDF, so every assertion is about what a customer
actually receives — not about what the code intends.

Probes named *_FIXED_* pin a bug that was found and fixed: each one FAILED
before the fix and passes now, so a regression on any of them reopens the
finding. See qa-reports/phase6-invoice-pdf.md.

    cd backend && ./venv/Scripts/python.exe manage.py test apps.test_qa_phase6
"""

import re
import tempfile
from decimal import Decimal

import fitz  # PyMuPDF
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.bookings.invoices import (
    InvoiceNotPayable,
    create_and_send_invoice,
    generate_invoice_pdf,
    invoice_number,
    uncovered_characters,
)
from apps.bookings.models import Booking, Invoice, Payment
from apps.bookings.test_payments import PaymentTestCase

TEMP_MEDIA = tempfile.mkdtemp(prefix="qa_phase6_media_")

BENGALI_NAME = "রহিম উদ্দিন"
BENGALI_MIXED = "মোঃ Rahim উদ্দিন-চৌধুরী"


def pdf_text(pdf_bytes):
    """All text in the PDF, pages joined."""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def pdf_fonts(pdf_bytes):
    """{basefont_name: type} for every font referenced by the PDF."""
    fonts = {}
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            for entry in page.get_fonts(full=True):
                # (xref, ext, type, basefont, name, encoding, [refname])
                _xref, _ext, ftype, basefont = entry[0], entry[1], entry[2], entry[3]
                fonts[basefont] = ftype
    return fonts


def pdf_bytes_of(invoice):
    with invoice.pdf_file.open("rb") as f:
        return f.read()


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class InvoiceProbeBase(PaymentTestCase):
    """PaymentTestCase gives us make_booking()/initiate()/send_ipn() against the
    real payment path. 4P room, 2 adults => total 9500 (3500 base + 2x3000)."""

    def settle(self, booking, amount=None):
        """Initiate + settle a payment through the real gateway path."""
        amount = booking.due_amount if amount is None else Decimal(str(amount))
        response = self.initiate(
            booking, {"payment_type": "partial", "amount": str(amount)}
        )
        self.assertEqual(response.status_code, 200, response.data)
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        with self.captureOnCommitCallbacks(execute=True):
            self.send_ipn(payment)
        booking.refresh_from_db()
        return payment

    def latest_invoice(self, booking):
        return Invoice.objects.filter(booking=booking).order_by("-pk").first()


# ---------------------------------------------------------------------------
# Item 1 — every field on the PDF cross-checked against the DB row
# ---------------------------------------------------------------------------
class InvoiceContentTests(InvoiceProbeBase):
    def test_1_all_booking_fields_appear_on_the_pdf(self):
        booking = self.make_booking(adults=2, kids=[{"age": 5}])
        # 3500 base + 2x3000 adults + 1500 kid(age 5, FIXED) = 11000
        self.assertEqual(booking.total_amount, Decimal("11000.00"))
        self.settle(booking, "6000")
        invoice = self.latest_invoice(booking)
        text = pdf_text(pdf_bytes_of(invoice))

        booking.refresh_from_db()
        # Identity / contact
        self.assertIn(booking.customer_name, text)
        self.assertIn(booking.phone, text)
        self.assertIn(booking.email, text)
        self.assertIn(booking.booking_code, text)
        self.assertIn(invoice_number(invoice), text)
        # Trip
        booking_room = booking.rooms.first()
        self.assertIn(booking_room.room.room_number, text)
        self.assertIn(booking_room.room.room_type.name, text)
        self.assertIn(f"{booking.package.start_date:%d %b %Y}", text)
        self.assertIn(f"{booking.package.end_date:%d %b %Y}", text)
        # Pax
        self.assertIn("2 adult(s), 1 kid(s)", text)
        # Itemised charges
        self.assertIn("3500.00", text)  # room base
        self.assertIn("6000.00", text)  # adults subtotal (2 x 3000)
        self.assertIn("1500.00", text)  # kid fare
        # Money truth
        self.assertIn("11000.00", text)  # total
        self.assertIn("5000.00", text)  # due = 11000 - 6000
        self.assertEqual(booking.due_amount, Decimal("5000.00"))

    def test_1b_itemised_lines_sum_to_the_stored_total(self):
        """The charges table must reconcile to Booking.total_amount."""
        booking = self.make_booking(adults=3, kids=[{"age": 2}, {"age": 10}])
        self.settle(booking)
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        # Charge lines are the right-hand column of the CHARGES table.
        amounts = [Decimal(m) for m in re.findall(r"\b\d+\.\d{2}\b", text)]
        booking.refresh_from_db()
        self.assertIn(booking.total_amount, amounts)

    def test_1c_BUG_no_discount_line_exists_anywhere(self):
        """The brief asks for 'discount applied' on the invoice. There is no
        discount concept in the model, the pricing service, or the PDF."""
        self.assertFalse(hasattr(Booking, "discount_amount"))
        booking = self.make_booking()
        self.settle(booking)
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking))).lower()
        self.assertNotIn("discount", text)
        self.assertNotIn("coupon", text)

    def test_1d_BUG_paid_so_far_disagrees_with_the_payments_table(self):
        """The PAYMENTS RECEIVED table lists SUCCESS payments; 'Paid so far'
        is booking.paid_amount. A cash payment recorded by staff appears in
        both, so they agree — this probe pins the invariant so a regression in
        either shows up."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        text = pdf_text(pdf_bytes_of(invoice))
        booking.refresh_from_db()
        rows = booking.payments.filter(status=Payment.Status.SUCCESS)
        self.assertEqual(
            sum((p.amount for p in rows), Decimal("0.00")), booking.paid_amount
        )
        self.assertIn("PAYMENTS RECEIVED", text)

    def test_1e_stale_invoice_pdf_is_not_regenerated_when_booking_changes(self):
        """An invoice PDF is a stored artefact — it must NOT silently change
        after the fact. Probe records today's behaviour."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        before = pdf_bytes_of(invoice)
        booking.customer_name = "Someone Else Entirely"
        booking.save()
        after = pdf_bytes_of(invoice)
        self.assertEqual(before, after)  # stored PDF is immutable — correct

    def test_1f_FIXED_resend_email_body_matches_the_pdf_it_attaches(self):
        """M2. Resend used to re-attach the STORED pdf while rebuilding the
        email body from the CURRENT booking: one message said "PAID IN FULL"
        while the invoice attached to it showed 4500 outstanding.

        The invoice now freezes the money it attests to, and BOTH the PDF and
        the email body read those frozen figures — so they cannot disagree."""
        booking = self.make_booking()  # 9500
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)  # this invoice states due 4500
        self.settle(booking, "4500")  # booking is now fully paid, due 0
        mail.outbox.clear()

        staff = User.objects.create_superuser(
            username="qa6staff", email="s@x.com", password="pw"
        )
        client = APIClient()
        client.force_authenticate(staff)
        response = client.post(f"/api/staff/invoices/{invoice.pk}/resend/")
        self.assertEqual(response.status_code, 200)

        email = mail.outbox[0]
        _, attached, _ = email.attachments[0]
        attached_text = pdf_text(attached)

        # The invoice said 4500 due when it was issued, and still says so.
        self.assertEqual(invoice.due_amount, Decimal("4500.00"))
        self.assertIn("Due: 4500.00 BDT", email.body)
        self.assertIn("4500.00", attached_text)
        # It must NOT claim paid-in-full just because the booking since became so.
        self.assertNotIn("PAID IN FULL", email.body)
        self.assertNotIn("PAID IN FULL", attached_text)
        # The booking itself did move on — the invoice simply doesn't lie about it.
        booking.refresh_from_db()
        self.assertEqual(booking.due_amount, Decimal("0.00"))


# ---------------------------------------------------------------------------
# Item 2 — Bengali / special characters
# ---------------------------------------------------------------------------
class UnicodeRenderingTests(InvoiceProbeBase):
    def test_2_bengali_name_renders_as_real_glyphs(self):
        booking = self.make_booking()
        booking.customer_name = BENGALI_NAME
        booking.save()
        self.settle(booking)
        raw = pdf_bytes_of(self.latest_invoice(booking))
        text = pdf_text(raw)
        # Bengali code points survive the round-trip (no mojibake, no tofu).
        self.assertTrue(
            any("ঀ" <= ch <= "৿" for ch in text),
            "no Bengali code points found in the extracted PDF text",
        )
        fonts = pdf_fonts(raw)
        self.assertTrue(
            any("Bengali" in name for name in fonts),
            f"Bengali font not embedded; fonts = {fonts}",
        )
        # Every font must be embedded (TrueType subset), not a name reference.
        self.assertNotIn("Type1", set(fonts.values()))

    def test_2f_bengali_text_LAYER_is_lossy_even_though_the_render_is_correct(self):
        """Visual rendering is genuinely correct (verified by rasterising the
        BILLED TO band). But HarfBuzz emits shaped conjunct glyphs that carry
        no ToUnicode mapping, so the extracted TEXT layer of a Bengali name is
        corrupted with control chars — copy/paste, Ctrl-F and any downstream
        text extraction of a Bengali name are broken. Cosmetic for a printed
        invoice; matters if anything ever parses these PDFs."""
        booking = self.make_booking()
        booking.customer_name = BENGALI_NAME  # রহিম উদ্দিন
        booking.save()
        self.settle(booking)
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        # Bengali glyphs are present and correct on the page...
        self.assertTrue(any("ঀ" <= ch <= "৿" for ch in text))
        # ...but the name does not round-trip as the string we stored.
        self.assertNotIn(BENGALI_NAME, text)
        self.assertTrue(
            any(ch in text for ch in ("\x03", "\x07")),
            "expected unmapped shaped-glyph control chars in the text layer",
        )

    def test_2b_mixed_bengali_latin_name(self):
        booking = self.make_booking()
        booking.customer_name = BENGALI_MIXED
        booking.save()
        self.settle(booking)
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        self.assertIn("Rahim", text)
        self.assertTrue(any("ঀ" <= ch <= "৿" for ch in text))

    def test_2e_FIXED_arabic_name_now_renders_instead_of_a_blank_field(self):
        """H2. An Arabic name used to leave the BILLED TO 'Name' field
        COMPLETELY BLANK on a signed, sealed invoice — fpdf2 drops glyphs it
        has no font for, silently. NotoSansArabic is now embedded."""
        booking = self.make_booking()
        booking.customer_name = "محمد رحيم"
        booking.save()
        self.settle(booking)
        raw = pdf_bytes_of(self.latest_invoice(booking))
        text = pdf_text(raw)

        # The font covers the name, so nothing is dropped...
        self.assertEqual(uncovered_characters(booking.customer_name), set())
        # ...and Arabic code points really are on the page.
        self.assertTrue(
            any("؀" <= ch <= "ۿ" for ch in text),
            "no Arabic code points found in the rendered invoice",
        )
        fonts = pdf_fonts(raw)
        self.assertTrue(
            any("Arabic" in name for name in fonts), f"fonts embedded: {fonts}"
        )
        # The Name field is not blank any more.
        between = text.split("Name", 1)[1].split("Phone", 1)[0]
        self.assertNotEqual(between.strip(), "")

    def test_2c_FIXED_uncovered_script_is_flagged_and_marked_not_dropped(self):
        """H2 (residual). We embed Latin + Bengali + Arabic. A script outside
        those (CJK) still has no glyphs — but it must no longer VANISH
        silently: uncovered characters are detected, logged for staff, and
        rendered as a visible '?' so the invoice never quietly loses a name."""
        booking = self.make_booking()
        booking.customer_name = "李小龍 Rahim"
        booking.save()

        # The gap is detected rather than ignored...
        self.assertEqual(uncovered_characters(booking.customer_name), {"李", "小", "龍"})

        with self.assertLogs("apps.bookings.invoices", level="WARNING") as logs:
            self.settle(booking)
        self.assertTrue(
            any("no embedded font covers" in line for line in logs.output),
            logs.output,
        )

        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        self.assertIn("Rahim", text)
        self.assertNotIn("李小龍", text)  # still cannot be drawn...
        self.assertIn("?", text)  # ...but its absence is visible, not silent

    def test_2d_bengali_in_address_free_text_fields(self):
        """There is no address field on Booking at all."""
        self.assertFalse(hasattr(Booking, "address"))


# ---------------------------------------------------------------------------
# Item 3 — invoice number uniqueness / collisions
# ---------------------------------------------------------------------------
class InvoiceNumberTests(InvoiceProbeBase):
    def test_3_FIXED_number_is_a_stored_gapless_yearly_series(self):
        """The number used to be derived at render time from the pk
        (INV-<booking_code>-<pk>): never stored, so no DB constraint could
        exist, not sequential, and gapped wherever a row was deleted. It is now
        an issued, stored, gapless per-year series."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)

        year = timezone.localdate().year
        self.assertEqual(invoice.number, f"INV-{year}-00001")
        self.assertRegex(invoice.number, r"^INV-\d{4}-\d{5}$")
        # It is a real column, and the DB enforces uniqueness on it.
        self.assertTrue(Invoice._meta.get_field("number").unique)

    def test_3b_numbers_are_unique_and_consecutive(self):
        booking = self.make_booking()
        self.settle(booking, "5000")
        self.settle(booking, "4500")
        numbers = sorted(Invoice.objects.values_list("number", flat=True))
        self.assertEqual(len(numbers), len(set(numbers)))
        year = timezone.localdate().year
        self.assertEqual(numbers, [f"INV-{year}-00001", f"INV-{year}-00002"])

    def test_3e_FIXED_number_allocation_is_locked_not_racy(self):
        """Allocation takes a row lock on the per-year counter, so two invoices
        issued concurrently cannot draw the same number. The DB unique
        constraint is the backstop."""
        import inspect

        from apps.bookings.models import InvoiceCounter

        source = inspect.getsource(Invoice._next_number)
        self.assertIn("select_for_update", source)

        booking = self.make_booking()
        self.settle(booking, "5000")
        numbers = {
            Invoice.objects.create(
                booking=booking,
                total_amount=booking.total_amount,
                paid_amount=booking.paid_amount,
                due_amount=booking.due_amount,
                booking_status=booking.status,
            ).number
            for _ in range(25)
        }
        self.assertEqual(len(numbers), 25)
        self.assertEqual(
            InvoiceCounter.objects.get(year=timezone.localdate().year).last_number, 26
        )

    def test_3f_FIXED_no_invoice_can_be_issued_without_money_behind_it(self):
        """M3. Four invoices could be minted against a booking with NO payment
        at all — each a signed, sealed PDF, all four emailed to the customer.
        create_and_send_invoice now refuses: the guard lives in the invoice
        layer, so no caller can bypass it."""
        booking = self.make_booking()  # PENDING, paid 0.00
        with self.assertRaises(InvoiceNotPayable):
            create_and_send_invoice(booking)
        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_3d_FIXED_number_survives_its_booking_being_deleted(self):
        """The number no longer embeds the booking code, so it cannot be
        recycled when a booking is deleted and a code is reused. It is a
        standalone unique column."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        self.assertNotIn(booking.booking_code, invoice.number)

    def test_3g_FIXED_invoice_records_which_payment_it_attests_to(self):
        """An invoice is now issued against the Payment that settled, so
        'issued invoice' means 'money was received' as a schema-level fact."""
        booking = self.make_booking()
        payment = self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        self.assertEqual(invoice.payment_id, payment.pk)


# ---------------------------------------------------------------------------
# Item 4 — edge cases
# ---------------------------------------------------------------------------
class InvoiceEdgeCaseTests(InvoiceProbeBase):
    def test_4_FIXED_long_guest_name_stays_inside_its_column(self):
        """H1. customer_name is max_length=100 and the BILLED TO value cell was
        a fixed-width pdf.cell() — which neither wraps nor truncates, it just
        keeps drawing. An 82-char name (well within max_length) ran straight
        through the TRIP DETAILS column ON THE SAME BASELINE, leaving both the
        name and the tour dates illegible. Text is now shrink-to-fit."""
        booking = self.make_booking()
        booking.customer_name = (
            "Mohammad " + "Abdur Rahman Chowdhury " * 3 + "Khan"
        )[:100]
        booking.save()
        self.settle(booking)
        raw = pdf_bytes_of(self.latest_invoice(booking))

        with fitz.open(stream=raw, filetype="pdf") as doc:
            page = doc[0]
            spans = [
                (span["text"], span["bbox"])
                for block in page.get_text("dict")["blocks"]
                for line in block.get("lines", [])
                for span in line["spans"]
            ]
        name_spans = [s for s in spans if "Abdur Rahman" in s[0]]
        date_spans = [s for s in spans if "Tour dates" in s[0]]
        self.assertTrue(name_spans, "the guest name is missing from the invoice")
        self.assertTrue(date_spans)
        name_bbox = name_spans[0][1]
        date_bbox = date_spans[0][1]

        # The name must END before the TRIP DETAILS column BEGINS.
        self.assertLess(
            name_bbox[2],
            date_bbox[0],
            f"guest name still overruns into TRIP DETAILS: name ends at "
            f"{name_bbox[2]:.0f}pt, that column starts at {date_bbox[0]:.0f}pt",
        )
        # Nothing else on the name's baseline is overprinted either.
        for text, bbox in spans:
            if bbox is name_bbox or "Abdur Rahman" in text:
                continue
            overlaps_y = (
                min(name_bbox[3], bbox[3]) - max(name_bbox[1], bbox[1])
            ) > 1
            overlaps_x = (
                min(name_bbox[2], bbox[2]) - max(name_bbox[0], bbox[0])
            ) > 1
            self.assertFalse(
                overlaps_y and overlaps_x,
                f"guest name overprints {text!r}",
            )

    def test_4b_zero_discount_booking(self):
        """No discount feature — every booking is a 'zero discount' booking."""
        booking = self.make_booking()
        self.settle(booking)
        booking.refresh_from_db()
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        self.assertIn("9500.00", text)
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertIn("PAID IN FULL", text)

    def test_4c_kid_pricing_tiers_are_itemised_per_kid(self):
        booking = self.make_booking(
            adults=1, kids=[{"age": 2}, {"age": 6}]
        )  # free + fixed 1500
        self.assertEqual(booking.total_amount, Decimal("8000.00"))  # 3500+3000+0+1500
        self.settle(booking)
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        self.assertIn("Kid fare (age 2)", text)
        self.assertIn("Kid fare (age 6)", text)
        self.assertIn("0.00", text)  # free kid
        self.assertIn("1500.00", text)
        self.assertIn("8000.00", text)

    def test_4d_FIXED_multi_room_booking_is_representable(self):
        """A booking now holds MANY rooms (BookingRoom rows) — a family taking
        two cabins is ONE booking, one payment, one invoice that itemises both
        rooms. (Was the reverse: the old single room FK could not represent it.)"""
        booking = self.make_booking(
            rooms=[
                {"room": self.room_4p, "adult_count": 2, "kid_details": []},
                {"room": self.room_2p, "adult_count": 2, "kid_details": []},
            ]
        )
        self.assertEqual(booking.rooms.count(), 2)
        # 4P: 3500 + 2×3000 = 9500; 2P: 2000 + 2×3000 = 8000 → 17500 total.
        self.assertEqual(booking.total_amount, Decimal("17500.00"))
        self.settle(booking)
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        # Both cabins appear on the single invoice.
        self.assertIn(self.room_4p.room_number, text)
        self.assertIn(self.room_2p.room_number, text)

    def test_4e_FIXED_kid_pricing_rule_change_cannot_rewrite_an_issued_invoice(self):
        """M1. The breakdown used to be recomputed at RENDER time from today's
        (admin-editable) rules, not from what the customer was actually
        charged: edit a kid rule after payment and the invoice's line items
        silently re-priced and stopped summing to the total. Line items now
        come from the snapshot frozen when the booking was priced."""
        from apps.packages.models import KidPricingRule

        booking = self.make_booking(adults=1, kids=[{"age": 6}])
        self.assertEqual(booking.total_amount, Decimal("8000.00"))
        self.settle(booking)  # customer paid 8000: 3500 + 3000 + 1500
        invoice = self.latest_invoice(booking)

        rule = KidPricingRule.objects.get(min_age=3, max_age=8)
        rule.amount = Decimal("9999.00")
        rule.save()

        text = pdf_text(generate_invoice_pdf(invoice))
        self.assertNotIn("9999.00", text)  # today's price does NOT appear...
        self.assertIn("1500.00", text)  # ...the price actually charged does
        self.assertIn("8000.00", text)

        # And the line items reconcile to the total, as an invoice must. The
        # booking snapshot is now {"rooms": [<per-room snapshot>, …]}; sum every
        # room's items.
        line_items = Decimal("0.00")
        for room_snap in booking.price_snapshot["rooms"]:
            line_items += (
                Decimal(room_snap["room_base"])
                + Decimal(room_snap["adults_subtotal"])
                + sum(Decimal(k["charge"]) for k in room_snap["kids"])
            )
        self.assertEqual(line_items, booking.total_amount)

    def test_4f_zero_total_booking(self):
        """Adult price 0 + free kid + zero base -> a 0.00 total. No payment can
        ever settle it (initiate rejects due<=0), so no invoice is ever sent."""
        self.package.adult_price = Decimal("0.00")
        self.package.save()
        self.type_4p.base_price = Decimal("0.00")
        self.type_4p.save()
        booking = self.make_booking(adults=1, kids=[{"age": 2}])
        self.assertEqual(booking.total_amount, Decimal("0.00"))
        response = self.initiate(booking, {"payment_type": "full"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 0)


# ---------------------------------------------------------------------------
# Item 5 — invoices only for paid bookings
# ---------------------------------------------------------------------------
class InvoiceIssuanceTests(InvoiceProbeBase):
    def test_5_pending_booking_gets_no_invoice(self):
        booking = self.make_booking()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_5b_failed_payment_gets_no_invoice(self):
        booking = self.make_booking()
        response = self.initiate(booking, {"payment_type": "full"})
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        with self.captureOnCommitCallbacks(execute=True):
            self.send_ipn(payment, verdict=self.verdict(payment, status="FAILED"))
        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_5c_payment_settling_on_a_cancelled_booking_gets_no_invoice(self):
        booking = self.make_booking()
        response = self.initiate(booking, {"payment_type": "full"})
        payment = Payment.objects.get(transaction_id=response.data["tran_id"])
        booking.status = Booking.Status.CANCELLED
        booking.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.send_ipn(payment)
        booking.refresh_from_db()
        self.assertTrue(booking.refund_required)
        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_5d_partial_payment_invoice_says_balance_remaining(self):
        booking = self.make_booking()
        self.settle(booking, "5000")
        text = pdf_text(pdf_bytes_of(self.latest_invoice(booking)))
        self.assertNotIn("PAID IN FULL", text)
        self.assertIn("4500.00", text)
        self.assertIn("Partially paid", text)

    def test_5e_FIXED_an_invoice_cannot_be_minted_for_an_unpaid_booking(self):
        """M3. Nothing in the invoice layer checked the booking's status, so an
        Invoice created from the admin/shell for a PENDING, 0.00-paid booking
        rendered a signed, sealed PDF — an official document for money never
        received. The guard now lives in create_and_send_invoice()."""
        booking = self.make_booking()  # PENDING, paid 0
        with self.assertRaises(InvoiceNotPayable):
            create_and_send_invoice(booking)
        self.assertEqual(Invoice.objects.filter(booking=booking).count(), 0)

        # And the Django admin cannot hand-add one either.
        from apps.bookings.admin import InvoiceAdmin

        self.assertFalse(InvoiceAdmin.has_add_permission(InvoiceAdmin, None))

    def test_5f_FIXED_cancelled_booking_invoice_never_claims_paid_in_full(self):
        """M3. Booking.save() zeroes due_amount on cancel (the balance is
        uncollectable, not settled) — and the invoice's banner keyed off
        due<=0, so a CANCELLED booking that had paid 5000 of 9500 printed the
        green 'PAID IN FULL' stamp. It now keys off the booking's STATUS."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)  # issued while partially_paid

        booking.refresh_from_db()
        booking.status = Booking.Status.CANCELLED
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.due_amount, Decimal("0.00"))
        self.assertEqual(booking.paid_amount, Decimal("5000.00"))

        # The already-issued invoice still states what was true when issued.
        text = pdf_text(generate_invoice_pdf(invoice))
        self.assertNotIn("PAID IN FULL", text)
        self.assertIn("4500.00", text)

        # An invoice issued FOR the cancelled booking says so explicitly.
        cancelled_invoice = Invoice.objects.create(
            booking=booking,
            total_amount=booking.total_amount,
            paid_amount=booking.paid_amount,
            due_amount=booking.due_amount,
            booking_status=booking.status,
        )
        text = pdf_text(generate_invoice_pdf(cancelled_invoice))
        self.assertNotIn("PAID IN FULL", text)
        self.assertIn("BOOKING CANCELLED", text)


# ---------------------------------------------------------------------------
# Item 6 — tampering / access control on the PDF
# ---------------------------------------------------------------------------
class InvoiceTamperingTests(InvoiceProbeBase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser(
            username="qa6admin", email="a@x.com", password="pw"
        )

    def auth(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        return client

    def test_6_amounts_come_from_the_db_not_from_any_request(self):
        """generate_invoice_pdf(invoice) takes no request and no params — there
        is no query-parameter surface to tamper with. Amounts are read from
        the Booking row."""
        import inspect

        sig = inspect.signature(generate_invoice_pdf)
        self.assertEqual(list(sig.parameters), ["invoice"])

    def test_6b_query_params_on_the_staff_invoice_endpoint_change_nothing(self):
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        client = self.auth()
        response = client.get(
            f"/api/staff/invoices/{invoice.pk}/",
            {"total_amount": "1.00", "paid_amount": "99999", "amount": "0"},
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("9500.00"))
        self.assertEqual(booking.paid_amount, Decimal("5000.00"))
        # And the stored PDF still shows the real numbers.
        self.assertIn("4500.00", pdf_text(pdf_bytes_of(invoice)))

    def test_6c_invoice_endpoint_is_write_protected(self):
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        client = self.auth()
        for method in ("put", "patch", "delete"):
            response = getattr(client, method)(f"/api/staff/invoices/{invoice.pk}/", {})
            self.assertEqual(response.status_code, 405, method)

    def test_6d_invoice_api_requires_staff_auth(self):
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)
        anon = APIClient()
        self.assertEqual(anon.get("/api/staff/invoices/").status_code, 401)
        self.assertEqual(
            anon.get(f"/api/staff/invoices/{invoice.pk}/").status_code, 401
        )
        self.assertEqual(
            anon.post(f"/api/staff/invoices/{invoice.pk}/resend/").status_code, 401
        )

    def test_6e_FIXED_pdf_path_is_no_longer_derivable_from_public_data(self):
        """C1. The PDF used to live at invoices/INV-<booking_code>-<pk>.pdf —
        derivable from the customer's own booking code plus a small sequential
        integer, so one customer could walk the pk to everyone else's invoice.
        It now lives behind a 256-bit capability token."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)

        self.assertNotIn(booking.booking_code, invoice.pdf_file.name)
        self.assertNotIn(f"-{invoice.pk}.pdf", invoice.pdf_file.name)
        self.assertIn(invoice.access_token, invoice.pdf_file.name)
        self.assertGreaterEqual(len(invoice.access_token), 40)

    def test_6e2_FIXED_staff_pdf_url_points_at_an_authenticated_route(self):
        """C1. pdf_url used to hand out the raw MEDIA_URL. It now points at the
        staff download endpoint, which is behind IsAdminUser."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)

        client = self.auth()
        data = client.get(f"/api/staff/invoices/{invoice.pk}/").data
        self.assertIn(f"/api/staff/invoices/{invoice.pk}/pdf/", data["pdf_url"])
        self.assertNotIn("/media/", data["pdf_url"])

        # Staff can fetch it...
        response = client.get(f"/api/staff/invoices/{invoice.pk}/pdf/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF"))
        # ...an anonymous caller cannot.
        self.assertEqual(
            APIClient().get(f"/api/staff/invoices/{invoice.pk}/pdf/").status_code, 401
        )

    def test_6f_FIXED_customer_can_fetch_their_own_invoice_but_not_anyone_elses(self):
        """C1. The customer previously had NO way to re-obtain their invoice
        (the UI's "Download Receipt" button just opened a browser print dialog
        on an HTML card). They can now list and download their own — authorised
        by the invoice's capability token, so a booking code never reveals
        another customer's invoice."""
        booking = self.make_booking()
        self.settle(booking, "5000")
        invoice = self.latest_invoice(booking)

        response = self.client.get(f"/api/bookings/{booking.booking_code}/invoices/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        entry = response.data[0]
        self.assertEqual(entry["number"], invoice.number)
        self.assertEqual(entry["due_amount"], "4500.00")

        # The download link works, unauthenticated, for the token holder.
        anon = APIClient()
        pdf = anon.get(entry["download_url"])
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(b"".join(pdf.streaming_content).startswith(b"%PDF"))

        # A wrong/guessed token gets nothing.
        self.assertEqual(
            anon.get("/api/invoices/not-a-real-token/download/").status_code, 404
        )

    def test_6g_FIXED_another_customer_cannot_reach_this_invoice(self):
        """The token is per-invoice and unguessable, so knowing one booking's
        code (or its pk) reveals nothing about anyone else's invoice."""
        victim = self.make_booking(room=self.room_4p)
        self.settle(victim, "5000")
        victim_invoice = self.latest_invoice(victim)

        attacker = self.make_booking(room=self.room_2p, adults=1)
        self.settle(attacker, str(attacker.due_amount))

        # The attacker knows their own booking code and can see their own
        # invoice — but the victim's invoice is not reachable from either.
        response = self.client.get(f"/api/bookings/{attacker.booking_code}/invoices/")
        tokens = [e["download_url"] for e in response.data]
        self.assertEqual(len(tokens), 1)
        self.assertNotIn(victim_invoice.access_token, tokens[0])

        # The old, guessable path is not served at all.
        guess = f"/media/invoices/INV-{victim.booking_code}-{victim_invoice.pk}.pdf"
        self.assertIn(APIClient().get(guess).status_code, (403, 404))
