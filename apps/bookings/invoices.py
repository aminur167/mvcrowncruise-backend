"""Invoice PDF generation + email delivery.

Issued after a successful payment (PRD §5.6). PDF text is shaped with HarfBuzz
(fpdf2), so Bengali and English both render correctly using the bundled Noto
fonts in backend/assets/fonts.

An issued invoice is an immutable financial record:

- The money it states (total/paid/due/status) is frozen onto the Invoice row
  at issue time, so the PDF and its covering email can never contradict each
  other, and a resend cannot silently restate an old invoice's numbers.
- The line items come from Booking.price_snapshot — the itemisation frozen
  when the booking was priced — never from today's (admin-editable) pricing
  rules, which would re-price an already-paid booking.
"""

import logging
from functools import lru_cache

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone
from django.utils.html import escape
from fontTools.ttLib import TTFont
from fpdf import FPDF

from .branding import draw_header_logo, draw_signature_block, draw_watermark
from .models import Booking, Invoice, Payment
from .pricing import restore_breakdown

logger = logging.getLogger(__name__)

FONTS_DIR = settings.BASE_DIR / "assets" / "fonts"


def booking_rooms(booking):
    """The booking's rooms, ordered by cabin number — the single place every
    invoice/email helper iterates rooms, so they stay in one consistent order."""
    return list(
        booking.rooms.select_related("room__room_type").order_by("room__room_number")
    )


def rooms_label(booking):
    """Comma-joined room numbers, e.g. "201, 202, 305" — for one-line summaries
    (email subject-ish lines, cancellation notice) where a table is overkill."""
    return ", ".join(br.room.room_number for br in booking_rooms(booking))

#: Booking states an invoice may be issued for. An invoice attests to money
#: received: a booking with nothing paid has nothing to attest to, and a
#: cancelled booking's money is owed back, not collected (QA M3).
INVOICEABLE_STATUSES = (Booking.Status.PARTIALLY_PAID, Booking.Status.FULLY_PAID)


class InvoiceNotPayable(Exception):
    """Refused to issue an invoice for a booking with no money on it."""


def create_and_send_invoice(booking, payment=None):
    """Create an Invoice (row + PDF) and email it.

    PDF/email trouble never raises — payment processing must not be disturbed
    by it. But issuing an invoice for a booking that was never paid DOES
    raise: that guard lives here, in the invoice layer, rather than in each
    caller, so no code path can mint an official sealed document for money
    that was never received (QA M3).
    """
    booking.refresh_from_db()
    if booking.status not in INVOICEABLE_STATUSES or booking.paid_amount <= 0:
        raise InvoiceNotPayable(
            f"{booking.booking_code} is '{booking.status}' with "
            f"{booking.paid_amount} paid — there is nothing to invoice."
        )

    invoice = Invoice.objects.create(
        booking=booking,
        payment=payment,
        sent_via=Invoice.SentVia.EMAIL,
        # Freeze the money this invoice attests to. The booking's live totals
        # keep moving as the customer pays; an issued invoice must not (M2).
        total_amount=booking.total_amount,
        paid_amount=booking.paid_amount,
        due_amount=booking.due_amount,
        booking_status=booking.status,
    )
    try:
        pdf_bytes = generate_invoice_pdf(invoice)
        invoice.pdf_file.save(f"{invoice.number}.pdf", ContentFile(pdf_bytes), save=True)
    except Exception:
        logger.exception("Invoice PDF generation failed for %s", booking.booking_code)
        return invoice
    try:
        send_invoice_email(invoice)
    except Exception:
        # sent_at stays NULL — picked up later by `send_unsent_invoices`.
        logger.exception("Invoice email failed for %s", booking.booking_code)
    return invoice


def invoice_number(invoice):
    """The issued number — stored on the row, never re-derived at render time."""
    return invoice.number


def invoice_state_label(invoice):
    """The status this invoice states. Falls back to the booking's live status
    for rows issued before booking_status existed (it is blank on those)."""
    status = invoice.booking_status or invoice.booking.status
    if status == Booking.Status.FULLY_PAID:
        return "PAID IN FULL"
    try:
        return Booking.Status(status).label
    except ValueError:
        return str(status or "—")


def send_invoice_email(invoice):
    """Email the invoice.

    Every figure comes from the INVOICE, not from the live booking. A resend
    of an old invoice must describe *that* invoice: reading the booking here
    is what let one email say "PAID IN FULL" while the PDF it was announcing
    showed an outstanding balance (QA M2).
    """
    booking = invoice.booking
    ship_name = booking.package.ship.name
    subject = f"{ship_name} — Invoice {invoice.number} for booking {booking.booking_code}"
    # Plain-text fallback for clients that don't render HTML.
    body = (
        f"Dear {booking.customer_name},\n\n"
        f"Thank you for your payment. Your invoice is attached.\n\n"
        f"Invoice: {invoice.number}\n"
        f"Booking: {booking.booking_code}\n"
        f"Room(s): {rooms_label(booking)}\n"
        f"Total: {invoice.total_amount} BDT\n"
        f"Paid: {invoice.paid_amount} BDT\n"
        f"Due: {invoice.due_amount} BDT"
        + ("\n\nYour booking is PAID IN FULL." if invoice.fully_paid else "")
        + f"\n\n{ship_name}"
    )
    message = EmailMultiAlternatives(subject=subject, body=body, to=[booking.email])
    message.attach_alternative(_invoice_email_html(invoice), "text/html")
    invoice.pdf_file.open("rb")
    try:
        message.attach(
            f"{invoice.number}.pdf", invoice.pdf_file.read(), "application/pdf"
        )
    finally:
        invoice.pdf_file.close()
    message.send()
    invoice.sent_at = timezone.now()
    invoice.save(update_fields=["sent_at"])


def _invoice_email_html(invoice):
    """Branded HTML body — email-safe markup only (tables + inline styles),
    since email clients ignore stylesheets and block remote assets.

    Reads the invoice's frozen figures, never the live booking (see
    send_invoice_email)."""
    booking = invoice.booking
    package = booking.package
    ship_name = escape(package.ship.name)
    fully_paid = invoice.fully_paid

    label = 'style="padding:9px 0;color:#69737d;font-size:13px;width:38%;"'
    value = 'style="padding:9px 0;color:#28323c;font-size:13px;font-weight:bold;"'
    rows = booking_rooms(booking)
    detail_pairs = [
        ("Booking code", booking.booking_code),
        ("Tour dates", f"{package.start_date:%d %b %Y} – {package.end_date:%d %b %Y}"),
    ]
    # One "Room N" row per cabin, each with its own party — a family taking
    # several cabins sees each one and its guests spelled out.
    for i, br in enumerate(rows, start=1):
        room_label = "Room" if len(rows) == 1 else f"Room {i}"
        summary = (
            f"{br.room.room_number} ({br.room.room_type.name}) — "
            f"{br.adult_count} adult(s), {len(br.kid_details)} kid(s)"
        )
        if br.foreign_guests:
            summary += f", {len(br.foreign_guests)} foreign national(s)"
        detail_pairs.append((room_label, summary))
    # Passport numbers are NOT listed in the email body — email is unencrypted
    # in transit and lands in inboxes we do not control. The attached PDF (and
    # the manifest) carry them; here the customer only needs to see that the
    # surcharge on their invoice corresponds to real guests.
    details = "".join(
        f'<tr style="border-bottom:1px solid #eef1f5;">'
        f"<td {label}>{escape(k)}</td><td {value}>{escape(str(v))}</td></tr>"
        for k, v in detail_pairs
    )

    amount = 'style="padding:7px 14px;font-size:13px;text-align:right;color:#28323c;"'
    money = "".join(
        f'<tr><td style="padding:7px 14px;font-size:13px;color:#69737d;">{k}</td>'
        f"<td {amount}>{v} BDT</td></tr>"
        for k, v in [
            ("Total amount", invoice.total_amount),
            ("Paid so far", invoice.paid_amount),
        ]
    )
    due_row = (
        '<tr style="background:#102e50;">'
        '<td style="padding:9px 14px;font-size:13px;color:#ffffff;font-weight:bold;">'
        "Due amount</td>"
        '<td style="padding:9px 14px;font-size:14px;text-align:right;color:#ffffff;'
        f'font-weight:bold;">{invoice.due_amount} BDT</td></tr>'
    )
    if fully_paid:
        status_banner = (
            '<div style="background:#157333;color:#ffffff;text-align:center;'
            'padding:11px;border-radius:5px;font-size:14px;font-weight:bold;">'
            "PAID IN FULL — সম্পূর্ণ পরিশোধিত</div>"
        )
    else:
        status_banner = (
            '<div style="background:#fdf3e0;color:#8a5a10;text-align:center;'
            'padding:11px;border-radius:5px;font-size:13px;">'
            f"Remaining balance: <strong>{invoice.due_amount} BDT</strong> — "
            "please clear the due amount before your tour date.</div>"
        )

    return f"""\
<div style="background:#eef1f5;padding:28px 12px;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #dfe4ea;
                border-radius:8px;border-collapse:separate;overflow:hidden;">
    <tr>
      <td style="background:#102e50;padding:24px 30px;">
        <div style="color:#ffffff;font-size:22px;font-weight:bold;">{ship_name}</div>
        <div style="color:#c2d0e0;font-size:11px;letter-spacing:2px;margin-top:3px;">
          SHIP TOUR PACKAGE BOOKING</div>
      </td>
    </tr>
    <tr>
      <td style="padding:26px 30px 0;color:#28323c;font-size:14px;line-height:1.65;">
        Dear <strong>{escape(booking.customer_name)}</strong>,<br><br>
        Thank you for your payment. Your official invoice
        <strong>{invoice_number(invoice)}</strong> is attached to this email as a PDF.
      </td>
    </tr>
    <tr>
      <td style="padding:20px 30px 0;">
        <div style="color:#102e50;font-size:12px;font-weight:bold;letter-spacing:1px;
                    border-bottom:2px solid #102e50;padding-bottom:5px;">
          BOOKING DETAILS</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;">{details}</table>
      </td>
    </tr>
    <tr>
      <td style="padding:22px 30px 0;">
        <div style="color:#102e50;font-size:12px;font-weight:bold;letter-spacing:1px;
                    border-bottom:2px solid #102e50;padding-bottom:5px;">
          PAYMENT SUMMARY</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;background:#f7f9fb;">{money}{due_row}
        </table>
      </td>
    </tr>
    <tr><td style="padding:18px 30px 0;">{status_banner}</td></tr>
    <tr>
      <td style="padding:24px 30px 26px;color:#69737d;font-size:12px;line-height:1.6;
                 border-top:1px solid #eef1f5;text-align:center;">
        আপনার ভ্রমণ আনন্দময় হোক — Thank you for sailing with {ship_name}.<br>
        <span style="font-size:11px;">This is an automated email; the attached invoice
        bears the official seal of {ship_name}.</span>
      </td>
    </tr>
  </table>
</div>
"""


def send_balance_reminder_email(booking):
    """One-off nudge before the balance deadline (enforce_due_deadlines).

    Plain transactional email — the customer already has the invoice PDF with
    the full policy; this only needs the number and the date."""
    package = booking.package
    ship_name = package.ship.name
    deadline = timezone.localtime(package.balance_due_at())
    subject = (
        f"{ship_name} — balance due for booking {booking.booking_code}"
    )
    body = (
        f"Dear {booking.customer_name},\n\n"
        f"A friendly reminder: your booking {booking.booking_code} for the "
        f"{package.start_date:%d %b %Y} tour has an outstanding balance of "
        f"{booking.due_amount} BDT (paid so far: {booking.paid_amount} BDT).\n\n"
        f"Please try to settle the balance by {deadline:%d %b %Y, %I:%M %p}. "
        "You can pay it any time before departure from your booking "
        "confirmation page — or simply pay our guide when you board. Your "
        "cabin stays reserved for you either way.\n\n"
        f"{ship_name}"
    )
    EmailMultiAlternatives(subject=subject, body=body, to=[booking.email]).send()


def _cancellation_money_paragraph(booking, paid):
    """What happens to the money, stated in figures wherever we know them.

    A refund row exists by the time this runs whenever the cancellation went
    through the refunds service (the row is created inside the same transaction,
    and this email is dispatched on commit). Then the customer is told the exact
    charge, the exact refund and when to expect it — not "our team will contact
    you". The vague wording is kept only as the genuine fallback: cancellations
    raised straight from the Django admin, where no one has decided the numbers.
    """
    from apps.refunds.models import Refund

    refund = (
        booking.refunds.exclude(status=Refund.Status.VOID)
        .order_by("-created_at")
        .first()
    )
    if refund is None:
        return (
            f"You have paid {paid} BDT against this booking. Our "
            "cancellation-charge schedule determines how much of this is "
            "refundable. Our team will contact you on "
            f"{booking.phone} to arrange any refund — refunds are handled "
            "manually and are not issued automatically.\n\n"
        )

    sla_days = booking.package.ship.refund_sla_days
    lines = [f"Paid:                 {paid} BDT"]
    if refund.cancellation_charge > 0:
        lines.append(f"Cancellation charge:  {refund.cancellation_charge} BDT")
    lines.append(f"Refund due to you:    {refund.amount} BDT")
    body = "\n".join(lines) + "\n\n"
    if refund.account_number:
        body += (
            f"We will send this to your {refund.get_method_display()} account "
            f"ending {refund.account_number[-4:]} within {sla_days} working "
            "days.\n\n"
        )
    else:
        body += (
            f"Our team will contact you on {booking.phone} to arrange the "
            f"payout, within {sla_days} working days.\n\n"
        )
    return body


def send_cancellation_email(booking):
    """Tell the customer their booking is cancelled (QA M3).

    Fired from Booking.save() on every → CANCELLED transition, so the deadline
    cron, the staff API and the Django admin are all covered without anyone
    remembering to opt in. Never raises: a cancellation must not be rolled
    back (or a cron run aborted) by an SMTP problem.

    A cancelled booking's room is immediately back on public sale, so the one
    thing the customer must not be is uninformed — without this their first
    sign is turning up at the jetty.
    """
    try:
        package = booking.package
        ship_name = package.ship.name
        paid = booking.paid_amount
        subject = f"{ship_name} — booking {booking.booking_code} has been cancelled"
        rooms = rooms_label(booking)
        body = (
            f"Dear {booking.customer_name},\n\n"
            f"Your booking {booking.booking_code} (room(s) "
            f"{rooms}) for the "
            f"{package.start_date:%d %b %Y} tour has been cancelled.\n\n"
        )
        if paid > 0:
            body += _cancellation_money_paragraph(booking, paid)
        else:
            body += "No payment was received against this booking.\n\n"
        body += (
            "If you believe this is a mistake, please contact us as soon as "
            f"possible.\n\n{ship_name}"
        )
        EmailMultiAlternatives(subject=subject, body=body, to=[booking.email]).send()
    except Exception:
        logger.exception(
            "Cancellation email failed for %s", booking.booking_code
        )


# Condensed from the company's official Payment & Cancellation Policy —
# printed on every invoice; the full version lives on the website's policy page.
POLICY_POINTS = [
    "Booking confirmation requires a 50% advance payment; the remaining balance "
    "may be settled any time before the journey — online from your booking page, "
    "or paid to our guide on board. Your cabin stays reserved.",
    "Cancellation charges (% of total amount) — individual bookings: 3 weeks "
    "before departure 5%, 2 weeks 15%, 1 week 35%, 3 days 50%, 48 hours 75%, "
    "24 hours 90%, less than 24 hours 100%.",
    "Group bookings: 15%, 20%, 25%, 50%, 70%, 90% and 100% on the same schedule.",
    "All prices exclude VAT & TAX. Additional government revenue charges apply "
    "for foreign guests.",
    "In case of bad weather, technical problems of the ship, or fewer than 30 "
    "passengers, the tour may be cancelled, rescheduled or refunded upon "
    "discussion with the guest.",
]


def _draw_policy_panel(pdf, x, y, width):
    """Compact policy summary beside the totals box. Returns its end y."""
    NAVY = (16, 46, 80)
    GREY = (105, 115, 125)
    RULE = (210, 216, 222)
    pdf.set_xy(x, y)
    pdf.set_font("NotoSans", "B", 8)
    pdf.set_text_color(*NAVY)
    pdf.cell(width, 5, "IMPORTANT POLICIES", new_x="LEFT", new_y="NEXT")
    pdf.set_draw_color(*RULE)
    pdf.line(x, pdf.get_y() + 0.5, x + width, pdf.get_y() + 0.5)
    pdf.ln(2.5)
    pdf.set_font("NotoSans", "", 6.8)
    pdf.set_text_color(*GREY)
    for point in POLICY_POINTS:
        pdf.set_x(x)
        pdf.multi_cell(width, 3.3, f"•  {point}", new_x="LEFT", new_y="NEXT")
        pdf.ln(1.2)
    return pdf.get_y()


#: The fonts embedded in the PDF, in fallback order.
FONT_FILES = {
    "NotoSans": "NotoSans-Regular.ttf",
    "NotoSansBengali": "NotoSansBengali-Regular.ttf",
    "NotoSansArabic": "NotoSansArabic-Regular.ttf",
}


@lru_cache(maxsize=1)
def _covered_codepoints():
    """Every code point the embedded fonts can actually draw.

    fpdf2 drops a glyph it has no font for *silently* — no exception, no
    warning. A guest named in a script we don't embed would get an invoice
    with a blank name field and nobody would ever know (QA H2). We therefore
    read the fonts' cmaps up front and decide explicitly what to do.
    """
    covered = set()
    for filename in FONT_FILES.values():
        path = FONTS_DIR / filename
        if not path.exists():
            continue
        with TTFont(str(path), fontNumber=0, lazy=True) as font:
            for table in font["cmap"].tables:
                covered.update(table.cmap.keys())
    return frozenset(covered)


def uncovered_characters(text):
    """The characters in `text` that no embedded font can draw."""
    covered = _covered_codepoints()
    return {
        ch
        for ch in str(text)
        if not ch.isspace() and ord(ch) not in covered
    }


def render_safe(text):
    """`text` with any un-drawable character replaced by a visible marker.

    Never return an empty string where the caller expected content: a blank
    'Name' on a sealed invoice is worse than a transliterated one, because
    nothing about it looks wrong. The caller logs the substitution so staff
    can follow up.
    """
    text = str(text)
    missing = uncovered_characters(text)
    if not missing:
        return text
    covered = _covered_codepoints()
    out = "".join(
        ch if (ch.isspace() or ord(ch) in covered) else "?" for ch in text
    )
    return out.strip() or "?"


def fitted_text(pdf, text, width, base_size, min_size=6.0):
    """Return (text, font_size) that fits `width` mm in the CURRENT font.

    fpdf2's cell() neither wraps nor truncates — it just keeps drawing, so an
    over-long value runs straight through the neighbouring column and both
    become unreadable (QA H1). We shrink the type first (a slightly smaller
    name is still a correct name), and only ellipsise if even min_size will
    not fit.
    """
    family, style = pdf.font_family, pdf.font_style
    size = base_size
    while size > min_size:
        pdf.set_font(family, style, size)
        if pdf.get_string_width(text) <= width:
            return text, size
        size -= 0.25

    pdf.set_font(family, style, min_size)
    if pdf.get_string_width(text) <= width:
        return text, min_size
    ellipsis = "…" if not uncovered_characters("…") else "..."
    truncated = text
    while truncated and pdf.get_string_width(truncated + ellipsis) > width:
        truncated = truncated[:-1]
    return (truncated + ellipsis) if truncated else ellipsis, min_size


def _fitted_cell(pdf, width, height, text, base_size, **kwargs):
    """cell() that is guaranteed to stay inside `width`."""
    text, size = fitted_text(pdf, text, width - 2, base_size)
    pdf.cell(width, height, text, **kwargs)
    pdf.set_font(pdf.font_family, pdf.font_style, base_size)


def generate_invoice_pdf(invoice):
    booking = invoice.booking
    package = booking.package

    rooms = booking_rooms(booking)

    # Any character we cannot draw is replaced by a visible marker rather than
    # vanishing. Log it so staff can correct the record (QA H2).
    missing = uncovered_characters(
        f"{booking.customer_name}{booking.email}"
        + "".join(br.room.room_number for br in rooms)
    )
    if missing:
        logger.warning(
            "Invoice %s: no embedded font covers %s in booking %s — rendered as "
            "'?'. Add the script's Noto font to assets/fonts.",
            invoice.number,
            "".join(sorted(missing)),
            booking.booking_code,
        )

    NAVY = (16, 46, 80)
    ACCENT = (232, 238, 245)
    ZEBRA = (247, 249, 251)
    GREY = (105, 115, 125)
    GREEN = (21, 115, 51)
    RULE = (210, 216, 222)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.add_font("NotoSans", "", FONTS_DIR / "NotoSans-Regular.ttf")
    pdf.add_font("NotoSans", "B", FONTS_DIR / "NotoSans-Bold.ttf")
    pdf.add_font("NotoSansBengali", "", FONTS_DIR / "NotoSansBengali-Regular.ttf")
    pdf.add_font("NotoSansBengali", "B", FONTS_DIR / "NotoSansBengali-Bold.ttf")
    fallbacks = ["NotoSansBengali"]
    if (FONTS_DIR / FONT_FILES["NotoSansArabic"]).exists():
        pdf.add_font("NotoSansArabic", "", FONTS_DIR / FONT_FILES["NotoSansArabic"])
        fallbacks.append("NotoSansArabic")
    pdf.set_fallback_fonts(fallbacks)
    pdf.set_text_shaping(True)
    epw = pdf.epw  # usable width between margins

    # ── Header — light, logo shown directly for a clean sharp mark ────────
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 3, "F")  # slim brand bar
    draw_header_logo(pdf, pdf.l_margin, 8, 23)
    text_x = pdf.l_margin + 29
    pdf.set_text_color(*NAVY)
    pdf.set_xy(text_x, 10)
    pdf.set_font("NotoSans", "B", 20)
    pdf.cell(epw / 2 - 29, 10, package.ship.name)
    pdf.set_font("NotoSans", "B", 22)
    pdf.cell(epw / 2, 10, "INVOICE", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("NotoSans", "", 9)
    pdf.set_text_color(*GREY)
    pdf.set_x(text_x)
    pdf.cell(epw / 2 - 29, 6, "Ship Tour Package Booking")
    pdf.cell(epw / 2, 6, invoice.number, align="R")
    # Authority contact numbers — top-right corner, under the invoice number.
    # Per-ship, editable from the staff dashboard (falls back to the system
    # default). Skip the line entirely if none are configured.
    phones = package.ship.authority_phone_list
    if phones:
        pdf.set_font("NotoSans", "", 7.5)
        pdf.set_xy(text_x, 24)
        pdf.cell(epw - 29, 4, "Helpline: " + "  ·  ".join(phones), align="R")
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, 34, pdf.l_margin + epw, 34)
    pdf.set_line_width(0.2)

    # ── Meta strip ─────────────────────────────────────────────────────────
    pdf.set_y(40)
    pdf.set_text_color(*GREY)
    pdf.set_font("NotoSans", "", 9)
    issued = f"{timezone.localtime(invoice.created_at):%d %b %Y, %I:%M %p}"
    pdf.cell(epw / 3, 6, f"Invoice date: {issued}")
    pdf.cell(epw / 3, 6, f"Booking: {booking.booking_code}", align="C")
    # "PAID IN FULL" is a claim about the booking's STATUS at issue time, not
    # merely about due==0 — a CANCELLED booking also has due==0 (uncollectable,
    # not settled) and was being stamped "PAID IN FULL" (QA M3).
    pdf.cell(
        epw / 3, 6, f"Status: {invoice_state_label(invoice)}", align="R",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.set_draw_color(*RULE)
    pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.l_margin + epw, pdf.get_y() + 1)
    pdf.ln(6)

    # ── Two-column info blocks ─────────────────────────────────────────────
    col_w = (epw - 8) / 2
    top_y = pdf.get_y()

    def info_block(x, title, rows):
        pdf.set_xy(x, top_y)
        pdf.set_font("NotoSans", "B", 10)
        pdf.set_text_color(*NAVY)
        pdf.set_fill_color(*ACCENT)
        pdf.cell(col_w, 7, f"  {title}", fill=True, new_x="LEFT", new_y="NEXT")
        pdf.set_text_color(40, 40, 40)
        label_w, value_w = col_w * 0.34, col_w * 0.66
        for label, value in rows:
            pdf.set_x(x)
            pdf.set_font("NotoSans", "", 9)
            pdf.set_text_color(*GREY)
            pdf.cell(label_w, 6.5, f"  {label}")
            pdf.set_font("NotoSans", "", 9.5)
            pdf.set_text_color(40, 40, 40)
            # Shrink-to-fit inside value_w. A plain cell() would keep drawing
            # past the column and overprint the block to its right, wrecking
            # both (QA H1) — an 82-char name is well within max_length=100.
            _fitted_cell(
                pdf, value_w, 6.5, render_safe(value), 9.5,
                new_x="LEFT", new_y="NEXT",
            )

    info_block(
        pdf.l_margin,
        "BILLED TO",
        [
            ("Name", booking.customer_name),
            ("Phone", booking.phone),
            ("Email", booking.email),
        ],
    )
    end_left = pdf.get_y()
    trip_rows = [
        ("Tour dates", f"{package.start_date:%d %b %Y} – {package.end_date:%d %b %Y}"),
    ]
    # One line per cabin: number (type) and that cabin's own party. For a single
    # room this reads exactly as before ("Room" / "Guests"); a multi-room family
    # gets "Room 1", "Room 2", … each with its guests.
    for i, br in enumerate(rooms, start=1):
        room_label = "Room" if len(rooms) == 1 else f"Room {i}"
        trip_rows.append(
            (room_label,
             f"{br.room.room_number} ({br.room.room_type.name}), "
             f"{br.adult_count} adult(s), {len(br.kid_details)} kid(s)")
        )
    info_block(pdf.l_margin + col_w + 8, "TRIP DETAILS", trip_rows)
    pdf.set_y(max(end_left, pdf.get_y()) + 6)

    # ── Table helpers ──────────────────────────────────────────────────────
    def table_header(title, columns):
        pdf.set_font("NotoSans", "B", 10)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("NotoSans", "B", 8.5)
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        for label, width, align in columns:
            pdf.cell(width, 7, f" {label}", fill=True, align=align)
        pdf.ln()

    def table_row(cells, shade):
        pdf.set_font("NotoSans", "", 9)
        pdf.set_text_color(40, 40, 40)
        pdf.set_fill_color(*(ZEBRA if shade else (255, 255, 255)))
        for value, width, align in cells:
            _fitted_cell(
                pdf, width, 7, f" {render_safe(value)} ", 9, fill=True, align=align
            )
        pdf.ln()

    # ── Charges table ──────────────────────────────────────────────────────
    # Line items come from the snapshot frozen when the booking was priced —
    # never recomputed from today's (admin-editable) KidPricingRule /
    # adult_price, which would re-price an already-paid booking and leave the
    # itemisation not summing to what was charged (QA M1).
    desc_w, amt_w = epw - 45, 45
    table_header("CHARGES", [("Description", desc_w, "L"), ("Amount (BDT)", amt_w, "R")])
    # Line items come from each room's frozen snapshot — never recomputed from
    # today's (admin-editable) KidPricingRule / adult_price, which would re-price
    # an already-paid booking (QA M1). Every cabin is grouped and itemised in
    # turn; a single-room booking reads as one group, exactly as before.
    charge_rows = []  # (desc, amount) pairs, in draw order
    any_snapshot = False
    for br in rooms:
        breakdown = restore_breakdown(br.price_snapshot)
        if breakdown is None:
            continue
        any_snapshot = True
        charge_rows.append(
            (f"Room {br.room.room_number} — base price", breakdown["room_base"])
        )
        charge_rows.append((
            f"    Adult fare ({breakdown['adult_count']} × {breakdown['adult_price']})",
            breakdown["adults_subtotal"],
        ))
        charge_rows.extend(
            (f"    Kid fare (age {kid['age']})", kid["charge"])
            for kid in breakdown["kids"]
        )
        # Foreign-national surcharge, itemised per fare type. Counts AND rates
        # come from the frozen snapshot, so the line reads "2 × 3000" exactly as
        # charged even after staff change the package's rate. Pre-feature
        # snapshots restore these as 0 and print nothing.
        if breakdown["foreign_adult_count"]:
            charge_rows.append((
                f"    Foreign national surcharge — adult "
                f"({breakdown['foreign_adult_count']} × "
                f"{breakdown['foreigner_adult_surcharge']})",
                breakdown["foreigner_adult_surcharge"]
                * breakdown["foreign_adult_count"],
            ))
        if breakdown["foreign_kid_count"]:
            charge_rows.append((
                f"    Foreign national surcharge — child "
                f"({breakdown['foreign_kid_count']} × "
                f"{breakdown['foreigner_kid_surcharge']})",
                breakdown["foreigner_kid_surcharge"] * breakdown["foreign_kid_count"],
            ))
    if not any_snapshot:
        # Pre-snapshot booking (or snapshots that never got written): the stored
        # total is still the money truth, so show it as a single line rather
        # than inventing an itemisation that may not add up.
        charge_rows = [("Package charges", invoice.total_amount)]
    for i, (desc, amount) in enumerate(charge_rows):
        table_row([(desc, desc_w, "L"), (f"{amount}", amt_w, "R")], shade=i % 2 == 0)
    pdf.ln(5)

    # ── Foreign nationals ──────────────────────────────────────────────────
    # Only drawn when the booking actually has foreign guests, so every
    # domestic invoice — and every invoice issued before this feature — is
    # byte-for-byte the document it was. Passports are printed IN FULL here:
    # invoice PDFs live in the private bucket behind short-lived presigned
    # URLs, and a foreign guest needs the document to match their passport for
    # visa and reimbursement purposes.
    foreign_rows = [
        (br, guest)
        for br in rooms
        for guest in (br.foreign_guests or [])
    ]
    if foreign_rows:
        n_w, p_w = epw * 0.30, epw * 0.22
        c_w = epw * 0.16
        r_w = epw - n_w - p_w - c_w
        table_header(
            "FOREIGN NATIONALS",
            [
                ("Guest", n_w, "L"),
                ("Passport no.", p_w, "L"),
                ("Nationality", c_w, "L"),
                ("Cabin", r_w, "L"),
            ],
        )
        for i, (br, guest) in enumerate(foreign_rows):
            fare = "child" if guest.get("guest_type") == "kid" else "adult"
            table_row(
                [
                    (guest.get("full_name") or f"Foreign {fare}", n_w, "L"),
                    (guest.get("passport_number", "—"), p_w, "L"),
                    (guest.get("nationality") or "—", c_w, "L"),
                    (br.room.room_number, r_w, "L"),
                ],
                shade=i % 2 == 0,
            )
        pdf.ln(5)

    # ── Payments table ─────────────────────────────────────────────────────
    # Only payments that existed when this invoice was ISSUED. Otherwise a
    # re-render (resend, or regeneration after a redeploy) would grow new rows
    # that the invoice's own frozen "Paid so far" does not account for, and the
    # document would stop adding up.
    payments = booking.payments.filter(
        status=Payment.Status.SUCCESS, paid_at__lte=invoice.created_at
    ).order_by("paid_at", "pk")
    if payments:
        d_w, t_w, y_w, a_w = 48, epw - 48 - 28 - 40, 28, 40
        table_header(
            "PAYMENTS RECEIVED",
            [
                ("Date", d_w, "L"),
                ("Transaction ID", t_w, "L"),
                ("Type", y_w, "L"),
                ("Amount (BDT)", a_w, "R"),
            ],
        )
        for i, payment in enumerate(payments):
            paid_at = (
                f"{timezone.localtime(payment.paid_at):%d %b %Y, %I:%M %p}"
                if payment.paid_at
                else "—"
            )
            table_row(
                [
                    (paid_at, d_w, "L"),
                    (payment.transaction_id, t_w, "L"),
                    (payment.get_payment_type_display(), y_w, "L"),
                    (f"{payment.amount}", a_w, "R"),
                ],
                shade=i % 2 == 0,
            )
        pdf.ln(5)

    # ── Important policies (left) + summary box (right) ───────────────────
    box_w = 80
    box_x = pdf.l_margin + epw - box_w
    column_top = pdf.get_y()
    panel_end = _draw_policy_panel(pdf, pdf.l_margin, column_top, epw - box_w - 12)
    pdf.set_y(column_top)

    def summary_row(label, value, bold=False, fill=None, text=None):
        pdf.set_x(box_x)
        pdf.set_font("NotoSans", "B" if bold else "", 9.5 if not bold else 10.5)
        pdf.set_text_color(*(text or (40, 40, 40)))
        pdf.set_fill_color(*(fill or (255, 255, 255)))
        pdf.cell(box_w * 0.5, 8, f" {label}", fill=fill is not None)
        pdf.cell(
            box_w * 0.5, 8, f"{value} ", align="R", fill=fill is not None,
            new_x="LMARGIN", new_y="NEXT",
        )

    # The invoice's own frozen figures — not the booking's live ones, which
    # keep moving as the customer pays (QA M2).
    summary_row("Total amount", invoice.total_amount)
    summary_row("Paid so far", invoice.paid_amount)
    summary_row(
        "Due amount", invoice.due_amount, bold=True, fill=NAVY, text=(255, 255, 255)
    )

    if invoice.fully_paid:
        pdf.ln(3)
        pdf.set_x(box_x)
        pdf.set_font("NotoSans", "B", 11)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(*GREEN)
        pdf.cell(box_w, 9, "PAID IN FULL — সম্পূর্ণ পরিশোধিত", align="C", fill=True,
                 new_x="LMARGIN", new_y="NEXT")
    elif invoice.booking_status == Booking.Status.CANCELLED:
        # A cancelled booking has due==0 because the balance is uncollectable,
        # NOT because it was settled. Say so, rather than implying payment.
        pdf.ln(3)
        pdf.set_x(box_x)
        pdf.set_font("NotoSans", "B", 10)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(150, 40, 40)
        pdf.cell(box_w, 9, "BOOKING CANCELLED", align="C", fill=True,
                 new_x="LMARGIN", new_y="NEXT")

    # ── Authorized signature ───────────────────────────────────────────────
    pdf.ln(5)
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    draw_signature_block(pdf, box_x, box_w, package.ship.name)
    pdf.set_y(max(panel_end, pdf.get_y()))

    # ── Footer ─────────────────────────────────────────────────────────────
    pdf.set_y(-32)
    pdf.set_draw_color(*RULE)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + epw, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("NotoSans", "", 9)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 6, "আপনার ভ্রমণ আনন্দময় হোক — Thank you for sailing with MV Alaska",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("NotoSans", "", 7.5)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 5, "This invoice is issued electronically and bears the official "
                   f"seal of {package.ship.name}.",
             align="C")

    draw_watermark(pdf, package.ship.name)
    return bytes(pdf.output())
