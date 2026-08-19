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

    Room total = base_price + (adults × adult_price) + Σ kid tier charges
                 + foreign-national surcharges.

    `foreign_adults` / `foreign_kids` are the counts of guests already included
    in adult_count / kid_ages who are foreign nationals — a SUBSET, not extra
    people. Each pays a fixed per-person surcharge from the global
    ForeignerSurcharge policy on top of their ordinary fare, so a foreign child
    on the free age tier still carries the kid surcharge if one is configured.
    Callers pass the counts rather than the guest list because pricing has no
    business reading passport data.
    """
    adults_subtotal = package.adult_price * adult_count
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
    return {
        "room_base": room_type.base_price,
        "adult_price": package.adult_price,
        "adult_count": adult_count,
        "adults_subtotal": adults_subtotal,
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
        "total": (
            room_type.base_price + adults_subtotal + kids_subtotal + foreigner_subtotal
        ),
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
    return {"rooms": room_breakdowns, "grand_total": grand_total}


def snapshot_booking_breakdown(breakdown):
    """booking_price_breakdown → JSON-safe dict (for the API `price_breakdown`
    field and, if ever needed, a booking-level snapshot). Decimals → strings."""
    return {
        "rooms": [
            snapshot_breakdown(bd, room_number=bd.get("room_number"))
            for bd in breakdown["rooms"]
        ],
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
        "adults_subtotal": str(breakdown["adults_subtotal"]),
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
        "total": str(breakdown["total"]),
    }
    if room_number is not None:
        snap["room_number"] = room_number
    return snap


def restore_breakdown(snapshot):
    """A room's price_snapshot → breakdown with Decimals back (inverse of
    snapshot_breakdown). Returns None for an empty/absent snapshot.

    Every foreigner key is read with a DEFAULT, never subscripted: snapshots
    frozen before the foreign-national surcharge existed simply do not carry
    them, and those bookings are paid, invoiced and still re-rendered on demand
    (resend, regeneration after a redeploy). A KeyError here would 500 the
    invoice of every pre-feature booking — the snapshot is a historical record,
    so readers must tolerate older shapes forever.
    """
    if not snapshot:
        return None
    return {
        "room_base": Decimal(snapshot["room_base"]),
        "adult_price": Decimal(snapshot["adult_price"]),
        "adult_count": snapshot["adult_count"],
        "adults_subtotal": Decimal(snapshot["adults_subtotal"]),
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
