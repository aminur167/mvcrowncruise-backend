"""Booking price calculation — the single pricing authority.

Consumers: Booking.clean() (admin + API create), the public quote endpoint,
and later payment verification (Phase 4) / invoice breakdown (Phase 5).
Amounts must never be trusted from the client; they are always recomputed here.
All arithmetic is Decimal — never float.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.packages.models import ForeignerSurcharge, KidPricingRule

ZERO = Decimal("0.00")


def price_breakdown(
    room_type, package, adult_count, kid_ages, *, foreign_adults=0, foreign_kids=0
):
    """Full price breakdown, every amount a Decimal.

    Cabin total = base_price
                + (adult capacity × adult_price)   ← the whole cabin, not heads
                − (empty berths × meal allowance)
                + Σ kid tier charges
                + foreign-national surcharges
                − the sailing's offer

    `foreign_adults` / `foreign_kids` are the counts of guests already included
    in adult_count / kid_ages who are foreign nationals — a SUBSET, not extra
    people. Each pays a fixed per-person surcharge from the global
    ForeignerSurcharge policy on top of their ordinary fare, so a foreign child
    on the free age tier still carries the kid surcharge if one is configured.
    Callers pass the counts rather than the guest list because pricing has no
    business reading passport data.
    """
    # Two pricing models, chosen per ship (see Ship.meal_allowance).
    #
    # Selling cabins whole: the room leaves inventory whether one person takes
    # it or four, so the fare is its full adult capacity. What a missing guest
    # genuinely saves the operator is their food for the trip, and that much
    # comes back — once per empty berth, for the whole package rather than per
    # night. The two figures stay separate in the breakdown rather than folded
    # into one adjusted rate, because the invoice has to show the customer both
    # halves of that bargain.
    #
    # Selling per head: the original model, and what every ship does until
    # staff deliberately switch it on. `meal_allowance` being NULL is what says
    # so — null is not zero here.
    if package.ship.sells_whole_cabins:
        charged_adults = max(adult_count, room_type.max_adults)
        meal_allowance = package.ship.meal_allowance
    else:
        charged_adults = adult_count
        meal_allowance = ZERO
    empty_berths = charged_adults - adult_count
    empty_berth_discount = meal_allowance * empty_berths
    adults_subtotal = package.adult_price * charged_adults
    # Load every kid-pricing tier ONCE, not one query per child: the rules are a
    # tiny, rarely-changing admin table, and pricing a 2-kid booking used to fire
    # a separate KidPricingRule query per child (QA phase8b F4). Resolve each age
    # against the in-memory set instead.
    rules = list(KidPricingRule.objects.all())
    kids = [{"age": age, "charge": kid_charge(age, package, rules)} for age in kid_ages]
    kids_subtotal = sum((kid["charge"] for kid in kids), ZERO)
    # One global policy row, not a per-sailing price. Only read when there is
    # actually a foreign guest to charge, so a domestic booking — the vast
    # majority — costs no extra query.
    if foreign_adults or foreign_kids:
        surcharge = ForeignerSurcharge.get_solo()
        adult_surcharge = surcharge.adult_amount or ZERO
        kid_surcharge = surcharge.kid_amount or ZERO
    else:
        adult_surcharge = kid_surcharge = ZERO
    foreigner_subtotal = (
        adult_surcharge * foreign_adults + kid_surcharge * foreign_kids
    )
    # The sailing's offer is applied HERE, inside the one function every money
    # path goes through — the quote, Booking.reprice() and the invoice. Applied
    # at the call sites instead, there would be a path by which a customer is
    # shown an offer on the card and charged without it.
    #
    # Floored at zero BEFORE the offer: a berth allowance larger than the cabin
    # must give a free cabin, never a negative one, and a percentage off a
    # negative number would hand money back.
    subtotal = max(
        ZERO,
        room_type.base_price
        + adults_subtotal
        + kids_subtotal
        + foreigner_subtotal
        - empty_berth_discount,
    )
    discount = package.discount_on(subtotal)
    return {
        "room_base": room_type.base_price,
        "adult_price": package.adult_price,
        # How many are actually travelling, and how many berths were charged
        # for. They differ whenever a cabin is taken under capacity, and the
        # invoice needs both: one is who boards, the other is what was billed.
        "adult_count": adult_count,
        "charged_adults": charged_adults,
        "adults_subtotal": adults_subtotal,
        "empty_berth_count": empty_berths,
        # The rate is frozen alongside the amount, like every other rate here:
        # it is admin-editable, and the invoice must still print the figure the
        # customer was actually given.
        "meal_allowance": meal_allowance,
        "empty_berth_discount": empty_berth_discount,
        "kids": kids,
        "kids_subtotal": kids_subtotal,
        # Rates are carried alongside the counts so the invoice can print
        # "2 × 3000" from the frozen snapshot alone — the package's rate is
        # admin-editable and would otherwise drift away from what was charged.
        "foreign_adult_count": foreign_adults,
        "foreign_kid_count": foreign_kids,
        "foreigner_adult_surcharge": adult_surcharge,
        "foreigner_kid_surcharge": kid_surcharge,
        "foreigner_subtotal": foreigner_subtotal,
        # What the cabin came to before the offer, the offer's own name, and
        # what it took off — all three frozen onto the booking so the invoice
        # can show the customer the bargain they were given, months later,
        # after the offer itself has been edited or deleted.
        "subtotal": subtotal,
        "offer_label": package.offer_label if discount else "",
        "discount": discount,
        "total": subtotal - discount,
    }


def calculate_total(room_type, package, adult_count, kid_ages, **foreign):
    return price_breakdown(room_type, package, adult_count, kid_ages, **foreign)[
        "total"
    ]


def booking_price_breakdown(package, rooms):
    """Aggregate breakdown for a whole (multi-room) booking.

    `rooms` is an iterable of dicts {"room": Room, "adult_count", "kid_ages"}
    with optional "foreign_adults" / "foreign_kids" counts.
    Returns each room's own breakdown (carrying its room_number so the caller
    can label it) plus the grand total the customer is charged — one payment,
    one invoice for the whole family.
    """
    room_breakdowns = []
    grand_total = ZERO
    grand_subtotal = ZERO
    total_discount = ZERO
    offer_label = ""
    for entry in rooms:
        room = entry["room"]
        bd = price_breakdown(
            room.room_type,
            package,
            entry["adult_count"],
            entry["kid_ages"],
            foreign_adults=entry.get("foreign_adults", 0),
            foreign_kids=entry.get("foreign_kids", 0),
        )
        bd["room_number"] = room.room_number
        room_breakdowns.append(bd)
        grand_total += bd["total"]
        grand_subtotal += bd["subtotal"]
        total_discount += bd["discount"]
        offer_label = offer_label or bd["offer_label"]
    # The booking-level offer figures are the sum of the rooms', not a second
    # calculation — a summary line that recomputed the discount could disagree
    # with the rooms it is summarising.
    return {
        "rooms": room_breakdowns,
        "subtotal": grand_subtotal,
        "offer_label": offer_label,
        "discount": total_discount,
        "grand_total": grand_total,
    }


def snapshot_booking_breakdown(breakdown):
    """booking_price_breakdown → JSON-safe dict (for the API `price_breakdown`
    field and, if ever needed, a booking-level snapshot). Decimals → strings."""
    return {
        "rooms": [
            snapshot_breakdown(bd, room_number=bd.get("room_number"))
            for bd in breakdown["rooms"]
        ],
        # Defaults for the same reason restore_breakdown carries them: a
        # booking-level breakdown built before offers existed has no such keys.
        "subtotal": str(breakdown.get("subtotal", breakdown["grand_total"])),
        "offer_label": breakdown.get("offer_label", ""),
        "discount": str(breakdown.get("discount", ZERO)),
        "grand_total": str(breakdown["grand_total"]),
    }


def snapshot_breakdown(breakdown, room_number=None):
    """Breakdown → JSON-safe dict for a room's price_snapshot.

    Decimals become strings ("1500.00"), never floats — a float would round
    money the moment it is stored. `restore_breakdown` is the exact inverse.

    `room_number` is stored when known (a BookingRoom knows its cabin) so the
    invoice and guide report can label each room's line items; it is optional so
    a bare price preview (quote) can still snapshot without a room.
    """
    snap = {
        "room_base": str(breakdown["room_base"]),
        "adult_price": str(breakdown["adult_price"]),
        "adult_count": breakdown["adult_count"],
        "charged_adults": breakdown.get("charged_adults", breakdown["adult_count"]),
        "adults_subtotal": str(breakdown["adults_subtotal"]),
        "empty_berth_count": breakdown.get("empty_berth_count", 0),
        "meal_allowance": str(breakdown.get("meal_allowance", ZERO)),
        "empty_berth_discount": str(breakdown.get("empty_berth_discount", ZERO)),
        "kids": [
            {"age": kid["age"], "charge": str(kid["charge"])}
            for kid in breakdown["kids"]
        ],
        "kids_subtotal": str(breakdown["kids_subtotal"]),
        "foreign_adult_count": breakdown.get("foreign_adult_count", 0),
        "foreign_kid_count": breakdown.get("foreign_kid_count", 0),
        "foreigner_adult_surcharge": str(
            breakdown.get("foreigner_adult_surcharge", ZERO)
        ),
        "foreigner_kid_surcharge": str(breakdown.get("foreigner_kid_surcharge", ZERO)),
        "foreigner_subtotal": str(breakdown.get("foreigner_subtotal", ZERO)),
        # Frozen so the invoice can still show what the offer took off long
        # after the offer itself has been edited or deleted off the package.
        "subtotal": str(breakdown.get("subtotal", breakdown["total"])),
        "offer_label": breakdown.get("offer_label", ""),
        "discount": str(breakdown.get("discount", ZERO)),
        "total": str(breakdown["total"]),
    }
    if room_number is not None:
        snap["room_number"] = room_number
    return snap


def restore_breakdown(snapshot):
    """A room's price_snapshot → breakdown with Decimals back (inverse of
    snapshot_breakdown). Returns None for an empty/absent snapshot.

    Every foreigner and offer key is read with a DEFAULT, never subscripted:
    snapshots frozen before those features existed simply do not carry them,
    and those bookings are paid, invoiced and still re-rendered on demand
    (resend, regeneration after a redeploy). A KeyError here would 500 the
    invoice of every pre-feature booking — the snapshot is a historical record,
    so readers must tolerate older shapes forever.

    `subtotal` falls back to the total, which is what it was before any offer
    existed: a booking with no discount is one where the two are equal.
    """
    if not snapshot:
        return None
    return {
        "room_base": Decimal(snapshot["room_base"]),
        "adult_price": Decimal(snapshot["adult_price"]),
        "adult_count": snapshot["adult_count"],
        # A snapshot frozen before cabins were sold whole charged exactly the
        # heads that travelled: no berths were empty, so none were allowed for.
        "charged_adults": snapshot.get("charged_adults", snapshot["adult_count"]),
        "adults_subtotal": Decimal(snapshot["adults_subtotal"]),
        "empty_berth_count": snapshot.get("empty_berth_count", 0),
        "meal_allowance": Decimal(snapshot.get("meal_allowance", "0.00")),
        "empty_berth_discount": Decimal(snapshot.get("empty_berth_discount", "0.00")),
        "kids": [
            {"age": kid["age"], "charge": Decimal(kid["charge"])}
            for kid in snapshot["kids"]
        ],
        "kids_subtotal": Decimal(snapshot["kids_subtotal"]),
        "foreign_adult_count": snapshot.get("foreign_adult_count", 0),
        "foreign_kid_count": snapshot.get("foreign_kid_count", 0),
        "foreigner_adult_surcharge": Decimal(
            snapshot.get("foreigner_adult_surcharge", "0.00")
        ),
        "foreigner_kid_surcharge": Decimal(
            snapshot.get("foreigner_kid_surcharge", "0.00")
        ),
        "foreigner_subtotal": Decimal(snapshot.get("foreigner_subtotal", "0.00")),
        "subtotal": Decimal(snapshot.get("subtotal", snapshot["total"])),
        "offer_label": snapshot.get("offer_label", ""),
        "discount": Decimal(snapshot.get("discount", "0.00")),
        "total": Decimal(snapshot["total"]),
        "room_number": snapshot.get("room_number"),
    }


def kid_charge(age, package, rules=None):
    """Charge for one child of `age`.

    `rules` is an optional preloaded list of every KidPricingRule (as loaded by
    price_breakdown) so a multi-kid booking resolves in memory instead of one
    query per child. When omitted, falls back to a single indexed lookup.
    """
    if rules is None:
        rule = KidPricingRule.rule_for_age(age)
    else:
        rule = next(
            (r for r in rules if r.min_age <= age < r.max_age), None
        )
    if rule is None:
        raise ValidationError(
            f"No kid pricing rule covers age {age}. "
            "Configure KidPricingRules in the admin panel."
        )
    if rule.charge_type == KidPricingRule.ChargeType.FREE:
        return ZERO
    if rule.charge_type == KidPricingRule.ChargeType.FIXED:
        return rule.amount
    return package.adult_price
