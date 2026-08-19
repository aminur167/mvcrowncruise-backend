"""Guide collection report — PRD §5.7 / Phase 6.

One-page PDF per package listing every active booking's room number,
customer name & mobile, total pax, paid and due amounts, with a totals row.
The guide (no system access) uses the printed/PDF copy on the ship to
collect outstanding dues. Reuses the invoice PDF stack (fpdf2 + Noto fonts).
"""

from decimal import Decimal

from django.utils import timezone
from fpdf import FPDF

from apps.packages.models import PackageRoom

from .branding import draw_header_logo, draw_signature_block, draw_watermark
from .invoices import FONTS_DIR
from .models import Booking, BookingRoom

NAVY = (16, 46, 80)
ZEBRA = (247, 249, 251)
GREY = (105, 115, 125)
RULE = (210, 216, 222)
# Group accent — the vertical bar + badge that marks a multi-room (family)
# booking, so the guide sees at a glance which cabins are one party / one bill.
GROUP_BAR = (198, 160, 74)  # gold
GROUP_TINT = (250, 246, 236)  # faint gold wash behind a group's rows

# Per-ship density profiles for the guide report table. Compact packs more
# rooms on a page; large prints bigger, easier-to-read type (and may run to a
# second page). Only the table body/header scale — the branding header is fixed.
#   row_h   : height of one table row, mm
#   body    : body-text point size
#   head    : column-header point size
#   small   : point size for the group subtotal / secondary labels
DENSITY_PROFILES = {
    "compact": {"row_h": 5.5, "body": 7.5, "head": 7.5, "small": 7.0},
    "normal": {"row_h": 7.0, "body": 9.0, "head": 8.5, "small": 8.5},
    "large": {"row_h": 8.5, "body": 11.0, "head": 10.0, "small": 9.5},
}


def _grouped_booking_rows(package):
    """Active BookingRooms for the package, grouped by booking so a family's
    cabins stay together, then ordered by their lowest room number so the guide
    still walks the ship roughly in sequence.

    Returns a list of groups, each: {"booking", "rooms": [BookingRoom, ...]}.
    Single-room bookings are just a group of one — rendered exactly as before.
    """
    booking_rooms = (
        BookingRoom.objects.filter(package=package)
        .exclude(booking__status=Booking.Status.CANCELLED)
        .select_related("booking", "room")
        .order_by("room__room_number")
    )
    groups = {}
    for br in booking_rooms:
        groups.setdefault(br.booking_id, {"booking": br.booking, "rooms": []})
        groups[br.booking_id]["rooms"].append(br)
    # Order groups by each booking's lowest room number (numeric-aware), so a
    # 2-cabin family sits at its first cabin's position in the walk-through.
    def sort_key(group):
        first = group["rooms"][0].room.room_number
        return (len(first), first)  # short-then-lexical ≈ numeric for room nos.

    return sorted(groups.values(), key=sort_key)


def _room_num_key(room_number):
    """Numeric-aware sort key for room numbers ("9" before "10")."""
    return (len(room_number), room_number)


def _unbooked_rooms(package, booked_room_ids):
    """Package rooms with NO active booking, split into (available, blocked),
    each ordered by room number.

    Blocked rooms (admin holds) are called out separately so the sheet shows
    which cabins are deliberately withheld — a held cabin is not "available to
    walk-up", and printing it in the available list would mislead the guide."""
    rooms = (
        PackageRoom.objects.filter(package=package)
        .exclude(room_id__in=booked_room_ids)
        .select_related("room__room_type", "blocked_by")
    )
    available, blocked = [], []
    for pr in rooms:
        (blocked if pr.is_blocked else available).append(pr)
    key = lambda pr: _room_num_key(pr.room.room_number)
    return sorted(available, key=key), sorted(blocked, key=key)


def generate_guide_report_pdf(package, scope="booked"):
    """Guide collection report PDF.

    scope="booked" (default): only cabins that are actually booked — the sheet
    the guide collects dues from.
    scope="all": every cabin on the sailing — booked ones first (with their
    balances), then an "Available (unbooked)" section so staff see the whole
    ship's occupancy at a glance.
    """
    # One row per cabin, but a family's cabins are kept TOGETHER and banded so
    # the guide reads them as one party with one bill — not scattered across the
    # sheet by room number with a blank balance on the second cabin (which read
    # as "nothing owed"). paid/due belong to the whole booking, so a multi-room
    # booking prints them once, centred on its middle row; a single-room booking
    # is unchanged. Totals sum per booking, never per room.
    groups = _grouped_booking_rows(package)
    booked_room_ids = {br.room_id for g in groups for br in g["rooms"]}
    # Blocked (admin-held) rooms are always listed — even on the default
    # "booked" dues sheet — so staff/guide see which cabins are deliberately
    # held. The available (walk-up) list stays all-scope only.
    available_rooms, blocked_rooms = _unbooked_rooms(package, booked_room_ids)
    if scope != "all":
        available_rooms = []

    # Per-ship table density (compact/normal/large) — scales the table's row
    # height and font sizes; unknown/blank falls back to normal.
    prof = DENSITY_PROFILES.get(package.ship.guide_report_density, DENSITY_PROFILES["normal"])
    row_h = prof["row_h"]
    body_pt, head_pt, small_pt = prof["body"], prof["head"], prof["small"]

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("NotoSans", "", FONTS_DIR / "NotoSans-Regular.ttf")
    pdf.add_font("NotoSans", "B", FONTS_DIR / "NotoSans-Bold.ttf")
    pdf.add_font("NotoSansBengali", "", FONTS_DIR / "NotoSansBengali-Regular.ttf")
    pdf.add_font("NotoSansBengali", "B", FONTS_DIR / "NotoSansBengali-Bold.ttf")
    pdf.set_fallback_fonts(["NotoSansBengali"])
    pdf.set_text_shaping(True)
    epw = pdf.epw

    # Header — light, logo shown directly for a clean sharp mark.
    #
    # Layout: title (with tour dates) on the left, and directly under it the
    # ship's helpline numbers; the package / generated-time / scope meta sits in
    # the top-right corner, right-aligned, so the two blocks read as a clean
    # left-title / right-meta header.
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 2.5, "F")  # slim brand bar
    draw_header_logo(pdf, pdf.l_margin, 6, 17)
    title_x = pdf.l_margin + 22

    # ── Left: title + tour dates ──
    pdf.set_xy(title_x, 9)
    pdf.set_text_color(*NAVY)
    pdf.set_font("NotoSans", "B", 15)
    pdf.cell(epw / 2, 8, f"{package.ship.name} — Guide Collection Report",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(title_x)
    pdf.set_font("NotoSans", "", 8)
    pdf.set_text_color(*GREY)
    pdf.cell(epw / 2, 5,
             f"{package.start_date:%d %b %Y} – {package.end_date:%d %b %Y}",
             new_x="LMARGIN", new_y="NEXT")
    # Helpline numbers directly under the title (was the top-right corner).
    # Per-ship, editable from the staff dashboard; skip the line if none set.
    phones = package.ship.authority_phone_list
    if phones:
        pdf.set_x(title_x)
        pdf.set_font("NotoSans", "", 7.5)
        pdf.cell(epw / 2, 4, "Helpline: " + "  ·  ".join(phones),
                 new_x="LMARGIN", new_y="NEXT")

    # ── Right: package / generated / scope, right-aligned in the top corner ──
    title = package.marketing_title or "Ship tour package"
    generated = f"{timezone.localtime(timezone.now()):%d %b %Y, %I:%M %p}"
    scope_label = "All rooms (booked + available)" if scope == "all" else "Booked rooms only"
    meta_x = pdf.l_margin + epw / 2
    pdf.set_text_color(*GREY)
    pdf.set_font("NotoSans", "", 8.5)
    pdf.set_xy(meta_x, 10)
    pdf.cell(epw / 2, 5, f"Package: {title}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(meta_x)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(epw / 2, 5, f"Generated: {generated}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(meta_x)
    pdf.cell(epw / 2, 5, f"Scope: {scope_label}", align="R", new_x="LMARGIN", new_y="NEXT")

    # Divider rule below whichever block is taller.
    rule_y = max(pdf.get_y(), 27)
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, rule_y, pdf.l_margin + epw, rule_y)
    pdf.set_line_width(0.2)
    pdf.set_y(rule_y + 3)

    # Table header — Pax is split into Adults and Kids so the guide can see the
    # party composition per room at a glance (not just a combined count).
    col = {
        "room": 16, "name": epw - 16 - 30 - 15 - 15 - 30 - 30,
        "phone": 30, "adults": 15, "kids": 15, "paid": 30, "due": 30,
    }
    pdf.set_font("NotoSans", "B", head_pt)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(col["room"], row_h, " Room", fill=True)
    pdf.cell(col["name"], row_h, " Customer name", fill=True)
    pdf.cell(col["phone"], row_h, " Mobile", fill=True)
    pdf.cell(col["adults"], row_h, "Adults", fill=True, align="C")
    pdf.cell(col["kids"], row_h, "Kids", fill=True, align="C")
    pdf.cell(col["paid"], row_h, "Paid (BDT) ", fill=True, align="R")
    pdf.cell(col["due"], row_h, "Due (BDT) ", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    # Rows
    total_paid = total_due = Decimal("0.00")
    total_adults = total_kids = 0
    # x where the group accent bar is drawn (just inside the left margin).
    bar_x = pdf.l_margin
    zebra_i = 0  # zebra alternates per printed row, across groups
    pdf.set_font("NotoSans", "", body_pt)
    pdf.set_text_color(40, 40, 40)

    def money_row(label_cells, paid, due, *, bold_due, fill_rgb, top_border=False):
        """Draw one table row; label_cells is [(width, text, align), ...] that
        together span room+name+phone+adults+kids."""
        border = "T" if top_border else 0
        pdf.set_fill_color(*fill_rgb)
        for width, text, align in label_cells:
            pdf.cell(width, row_h, text, fill=True, align=align, border=border)
        pdf.cell(col["paid"], row_h, paid, fill=True, align="R", border=border)
        pdf.set_font("NotoSans", "B" if bold_due else "", body_pt)
        pdf.cell(col["due"], row_h, due, fill=True, align="R", border=border,
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("NotoSans", "", body_pt)

    for group in groups:
        booking = group["booking"]
        rooms = group["rooms"]
        is_group = len(rooms) > 1
        group_top_y = pdf.get_y()
        # For a group, the combined paid/due prints ONCE, vertically centred on
        # the group's middle cabin row — no separate subtotal line, so a 2-cabin
        # family is 2 rows, not 3 (saves vertical space on a busy sheet).
        balance_row = len(rooms) // 2

        for r_idx, br in enumerate(rooms):
            adults = br.adult_count
            kids = len(br.kid_details)
            # A multi-room booking gets a faint gold wash so its cabins read as
            # one block; single-room bookings keep the plain zebra striping.
            fill_rgb = (
                GROUP_TINT if is_group else (ZEBRA if zebra_i % 2 == 0 else (255, 255, 255))
            )
            # First cabin of a group carries a "· N rooms" tag after the name so
            # the guide instantly sees the party spans several cabins.
            name = booking.customer_name
            if is_group and r_idx == 0:
                name = f"{name}  · {len(rooms)} rooms"
            # Foreign nationals in THIS cabin. The guide checks passports on
            # boarding, so the marker has to sit on the cabin row itself — the
            # manifest at the end of the sheet is the detail, this is the
            # "stop at this door" cue.
            if br.foreign_guests:
                name = f"{name}  · {len(br.foreign_guests)} FN"
            label_cells = [
                (col["room"], f" {br.room.room_number}", "L"),
                (col["name"], f" {name}", "L"),
                (col["phone"], f" {booking.phone}", "L"),
                (col["adults"], str(adults), "C"),
                (col["kids"], str(kids), "C"),
            ]
            if is_group:
                # Combined balance only on the group's middle row; blank on the
                # rest so the number reads as one balance for the whole party.
                on_balance_row = r_idx == balance_row
                money_row(
                    label_cells,
                    f"{booking.paid_amount} " if on_balance_row else "",
                    f"{booking.due_amount} " if on_balance_row else "",
                    bold_due=on_balance_row and booking.due_amount > 0,
                    fill_rgb=fill_rgb,
                )
            else:
                # Single room: balance sits right on the row, exactly as before.
                money_row(
                    label_cells,
                    f"{booking.paid_amount} ",
                    f"{booking.due_amount} ",
                    bold_due=booking.due_amount > 0,
                    fill_rgb=fill_rgb,
                )
                zebra_i += 1
            total_adults += adults
            total_kids += kids

        if is_group:
            # Gold accent bar down the left edge spanning every row of the group.
            group_bottom_y = pdf.get_y()
            pdf.set_fill_color(*GROUP_BAR)
            pdf.rect(bar_x, group_top_y, 1.4, group_bottom_y - group_top_y, "F")

        total_paid += booking.paid_amount
        total_due += booking.due_amount

    if not groups:
        pdf.set_font("NotoSans", "", body_pt)
        pdf.cell(epw, 10, "No active bookings for this package.", align="C",
                 new_x="LMARGIN", new_y="NEXT")

    # Totals row (booked rooms only — the money the guide collects)
    total_h = row_h + 1
    pdf.set_draw_color(*RULE)
    pdf.set_font("NotoSans", "B", body_pt)
    pdf.set_text_color(40, 40, 40)
    pdf.cell(col["room"] + col["name"] + col["phone"], total_h, " TOTAL", border="T")
    pdf.cell(col["adults"], total_h, str(total_adults), border="T", align="C")
    pdf.cell(col["kids"], total_h, str(total_kids), border="T", align="C")
    pdf.cell(col["paid"], total_h, f"{total_paid} ", border="T", align="R")
    pdf.set_text_color(*NAVY)
    pdf.cell(col["due"], total_h, f"{total_due} ", border="T", align="R",
             new_x="LMARGIN", new_y="NEXT")

    # ── Blocked (admin-held) rooms — shown in BOTH scopes ──────────────────
    # Cabins staff deliberately withheld from sale. Listed with their internal
    # reason so the guide/office knows the cabin is held on purpose (not a data
    # gap) and is not to be sold on board.
    if blocked_rooms:
        pdf.ln(4)
        pdf.set_font("NotoSans", "B", body_pt)
        pdf.set_text_color(*NAVY)
        pdf.cell(
            epw, row_h,
            f"Blocked by admin — {len(blocked_rooms)} "
            f"room{'s' if len(blocked_rooms) != 1 else ''}  ·  held, not for sale",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.set_draw_color(*RULE)
        # Room + a wide "held / reason" span across the remaining columns.
        reason_w = col["name"] + col["phone"] + col["adults"] + col["kids"] + \
            col["paid"] + col["due"]
        for i, pr in enumerate(blocked_rooms):
            room = pr.room
            reason = pr.block_reason or "Held by admin"
            pdf.set_font("NotoSans", "", body_pt)
            pdf.set_text_color(40, 40, 40)
            pdf.set_fill_color(*(ZEBRA if i % 2 == 0 else (255, 255, 255)))
            pdf.cell(col["room"], row_h, f" {room.room_number}", fill=True, border="B")
            pdf.cell(reason_w, row_h, f" Blocked by admin — {reason}", fill=True,
                     border="B", new_x="LMARGIN", new_y="NEXT")

    # ── Available (unbooked) rooms — only in the all-rooms report ──────────
    # Same table columns as the booked rows above, but only the room number is
    # filled — the guide prints the sheet and writes the customer, pax and
    # amounts in by hand as walk-up cabins are taken on board.
    if scope == "all":
        pdf.ln(4)
        pdf.set_font("NotoSans", "B", body_pt)
        pdf.set_text_color(*NAVY)
        pdf.cell(
            epw, row_h,
            f"Available (unbooked) — {len(available_rooms)} "
            f"room{'s' if len(available_rooms) != 1 else ''}  ·  fill in on board",
            new_x="LMARGIN", new_y="NEXT",
        )
        if available_rooms:
            pdf.set_draw_color(*RULE)
            for i, pr in enumerate(available_rooms):
                room = pr.room
                pdf.set_font("NotoSans", "", body_pt)
                pdf.set_text_color(40, 40, 40)
                pdf.set_fill_color(*(ZEBRA if i % 2 == 0 else (255, 255, 255)))
                # Room number only; every other cell left blank (bordered) so
                # the guide can hand-write into it.
                pdf.cell(col["room"], row_h, f" {room.room_number}", fill=True, border="B")
                pdf.cell(col["name"], row_h, "", fill=True, border="B")
                pdf.cell(col["phone"], row_h, "", fill=True, border="B")
                pdf.cell(col["adults"], row_h, "", fill=True, border="B", align="C")
                pdf.cell(col["kids"], row_h, "", fill=True, border="B", align="C")
                pdf.cell(col["paid"], row_h, "", fill=True, border="B", align="R")
                pdf.cell(col["due"], row_h, "", fill=True, border="B", align="R",
                         new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_font("NotoSans", "", small_pt)
            pdf.set_text_color(*GREY)
            msg = (
                "  Every room on this sailing is booked or blocked."
                if blocked_rooms
                else "  Every room on this sailing is booked."
            )
            pdf.cell(epw, row_h, msg, new_x="LMARGIN", new_y="NEXT")

    # ── Foreign nationals — immigration manifest ───────────────────────────
    # Printed in BOTH scopes and only when the sailing actually carries foreign
    # guests, so a domestic voyage's sheet is unchanged. Bangladeshi river
    # cruises are asked for a foreign-passenger list at the port; the guide has
    # no system access, so this printed block is that list. Passports appear in
    # full — the sheet is an internal document handed to the authorities.
    manifest = [
        (br, guest)
        for group in groups
        for br in group["rooms"]
        for guest in (br.foreign_guests or [])
    ]
    if manifest:
        pdf.ln(4)
        if pdf.get_y() > pdf.h - 50:
            pdf.add_page()
        pdf.set_font("NotoSans", "B", body_pt)
        pdf.set_text_color(*NAVY)
        pdf.cell(
            epw, row_h,
            f"Foreign nationals — {len(manifest)} "
            f"guest{'s' if len(manifest) != 1 else ''}  ·  passport check on "
            "boarding",
            new_x="LMARGIN", new_y="NEXT",
        )
        m_room, m_pass, m_nat = col["room"], 34, 24
        m_name = epw - m_room - m_pass - m_nat - 30
        m_type = 30
        pdf.set_font("NotoSans", "B", head_pt)
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(m_room, row_h, " Room", fill=True)
        pdf.cell(m_name, row_h, " Guest name", fill=True)
        pdf.cell(m_pass, row_h, " Passport no.", fill=True)
        pdf.cell(m_nat, row_h, " Nat.", fill=True)
        pdf.cell(m_type, row_h, " Fare", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*RULE)
        for i, (br, guest) in enumerate(manifest):
            fare = "Child" if guest.get("guest_type") == "kid" else "Adult"
            pdf.set_font("NotoSans", "", body_pt)
            pdf.set_text_color(40, 40, 40)
            pdf.set_fill_color(*(ZEBRA if i % 2 == 0 else (255, 255, 255)))
            pdf.cell(m_room, row_h, f" {br.room.room_number}", fill=True, border="B")
            # Name is optional on a guest — the passport is the identifier, so
            # a nameless row still works as a manifest line.
            pdf.cell(m_name, row_h, f" {guest.get('full_name') or '—'}",
                     fill=True, border="B")
            pdf.set_font("NotoSans", "B", body_pt)
            pdf.cell(m_pass, row_h, f" {guest.get('passport_number', '—')}",
                     fill=True, border="B")
            pdf.set_font("NotoSans", "", body_pt)
            pdf.cell(m_nat, row_h, f" {guest.get('nationality') or '—'}",
                     fill=True, border="B")
            pdf.cell(m_type, row_h, f" {fare}", fill=True, border="B",
                     new_x="LMARGIN", new_y="NEXT")

    # Authorized signature (right side, below the totals)
    pdf.ln(8)
    if pdf.get_y() > pdf.h - 58:
        pdf.add_page()
    sig_w = 70
    draw_signature_block(pdf, pdf.l_margin + epw - sig_w, sig_w, package.ship.name)

    # Footer note — placed right below the table (not pinned to page bottom,
    # which would trigger an unwanted blank second page).
    pdf.ln(4)
    pdf.set_font("NotoSans", "", 7.5)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 5, "Bookings with due = 0.00 are fully paid. Collect the due amount "
                   "from each room and record it in the dashboard.", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Rooms marked with a gold bar and \"· N rooms\" are ONE family "
                   "booking — their paid/due is the combined balance, shown once, "
                   "not per room.",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"{package.ship.name} · computer-generated report", align="C")

    draw_watermark(pdf, package.ship.name)
    return bytes(pdf.output())
