"""Cancellation-charge calculation — the single authority on refund amounts.

Consumers: the public preview/request endpoints, the staff approval action, the
operator-cancellation bulk job and the reports. Nothing else may compute a
refund figure; a client never submits one.

Two ideas carry the whole module:

1. **The window.** A booking can only be cancelled by its customer while the
   sailing is still ahead of it. Once the ship has departed there is nothing to
   cancel — the last tier already charges 100%, so a self-service "cancel" would
   quote a 0.00 refund AND release a cabin that is physically occupied. After the
   sailing, money can still move (an overpayment is still not ours), but only a
   human raises it, under an explicit reason.

2. **The freeze.** Every number this module produces is stored with the request
   that produced it. Approving a request never recomputes: an admin editing the
   schedule, or staff taking two days to click, must not change what a customer
   was quoted.

All arithmetic is Decimal, quantised once at the end (ROUND_HALF_UP). Rounding
mid-calculation loses money a paisa at a time.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.bookings.models import Booking

from .models import CancellationRequest, CancellationRule

ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def q2(amount):
    """Money to 2dp, half-up. Applied once, at the end of a calculation."""
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- Windows ---------------------------------------------------------------

WINDOW_UPCOMING = "upcoming"
WINDOW_IN_PROGRESS = "in_progress"
WINDOW_SAILED = "sailed"


def package_window(package, today=None):
    """Where a sailing sits relative to today.

    Dates are DateFields, so the boundary is local midnight: a sailing that
    starts today is already 'in_progress' even if the ship leaves at 09:00. That
    costs the customer nothing — the final tier charges 100% either way — and it
    keeps the rule legible to staff on the phone.
    """
    today = today or timezone.localdate()
    if today < package.start_date:
        return WINDOW_UPCOMING
    if today <= package.end_date:
        return WINDOW_IN_PROGRESS
    return WINDOW_SAILED


# Why a booking cannot be cancelled by its customer right now. The value is a
# stable code; the frontend maps it to copy, so wording changes need no API
# change.
BLOCK_ALREADY_CANCELLED = "already_cancelled"
BLOCK_COMPLETED = "completed"
BLOCK_IN_PROGRESS = "in_progress"
BLOCK_SAILED = "sailed"
BLOCK_PENDING_REQUEST = "pending_request"
BLOCK_PAYMENT_IN_PROGRESS = "payment_in_progress"
BLOCK_NO_POLICY = "no_policy"


@dataclass
class CancellationQuote:
    """What cancelling this booking would cost, right now."""

    allowed: bool
    block_reason: str | None = None
    window: str = WINDOW_UPCOMING
    days_until_start: int = 0
    booking_type: str = Booking.BookingType.INDIVIDUAL
    tier_label: str = ""
    charge_percent: Decimal = ZERO
    total_amount: Decimal = ZERO
    paid_amount: Decimal = ZERO
    cancellation_charge: Decimal = ZERO
    refund_amount: Decimal = ZERO
    forfeited_amount: Decimal = ZERO
    shortfall_amount: Decimal = ZERO
    #: True when money has actually been received, so a human must approve.
    #: A booking nobody has paid for is cancelled on the spot.
    requires_approval: bool = False
    rule_id: int | None = None
    computed_on: str = ""
    _extra: dict = field(default_factory=dict)

    def to_snapshot(self):
        """JSON-safe record of how this quote was reached.

        Decimals become strings — a float would round money the moment it is
        stored (same rule the price snapshots follow). Stored on the request and
        copied onto the refund, so a schedule edit years later can never move a
        settled figure.
        """
        return {
            "computed_on": self.computed_on,
            "window": self.window,
            "days_until_start": self.days_until_start,
            "booking_type": self.booking_type,
            "tier_label": self.tier_label,
            "charge_percent": str(self.charge_percent),
            "rule_id": self.rule_id,
            "total_amount": str(self.total_amount),
            "paid_amount": str(self.paid_amount),
            "cancellation_charge": str(self.cancellation_charge),
            "refund_amount": str(self.refund_amount),
            "forfeited_amount": str(self.forfeited_amount),
            "shortfall_amount": str(self.shortfall_amount),
            **self._extra,
        }


# --- Schedule resolution ---------------------------------------------------


def schedule_for(ship):
    """The active tiers that apply to a ship, harshest-first (highest days
    first, i.e. the order the policy table is printed in).

    A ship with rows of its own uses ONLY those; otherwise the default
    (ship=NULL) schedule. Schedules are never merged — a half-inherited charge
    table cannot be reasoned about when it decides how much money someone gets
    back.
    """
    own = list(
        CancellationRule.objects.filter(ship=ship, is_active=True).order_by(
            "-days_before_start"
        )
    )
    if own:
        return own
    return list(
        CancellationRule.objects.filter(ship__isnull=True, is_active=True).order_by(
            "-days_before_start"
        )
    )


def resolve_rule(ship, days_until_start, schedule=None):
    """The tier that applies `days_until_start` days before departure.

    The applicable tier is the one with the largest ``days_before_start`` still
    <= the days remaining. Cancelling far in advance therefore lands on the
    topmost tier (5% on the standard schedule) rather than escaping the table:
    there is no free window unless the admin creates one.

    Returns None when the schedule is empty — the caller must then refuse to
    quote rather than guess. Guessing 0% gives away money; guessing 100% steals
    it.
    """
    if schedule is None:
        schedule = schedule_for(ship)
    days = max(days_until_start, 0)
    for rule in schedule:  # already ordered high → low
        if rule.days_before_start <= days:
            return rule
    return None


def describe_schedule(ship):
    """The schedule as the public policy page renders it."""
    return [
        {
            "days_before_start": rule.days_before_start,
            "label": rule.label,
            "individual_percent": str(rule.individual_percent),
            "group_percent": str(rule.group_percent),
        }
        for rule in schedule_for(ship)
    ]


# --- The quote -------------------------------------------------------------


def quote_cancellation(booking, *, today=None, ignore_pending=False):
    """What cancelling `booking` would cost its customer today.

    Pure: reads the booking and the schedule, writes nothing. `ignore_pending`
    is for re-quoting a request that already exists (the approval screen), where
    that request's own existence must not block the quote.
    """
    today = today or timezone.localdate()
    package = booking.package
    window = package_window(package, today)
    days_until_start = (package.start_date - today).days

    def blocked(reason):
        return CancellationQuote(
            allowed=False,
            block_reason=reason,
            window=window,
            days_until_start=days_until_start,
            booking_type=booking.booking_type,
            total_amount=booking.total_amount,
            paid_amount=booking.paid_amount,
            computed_on=today.isoformat(),
        )

    if booking.status == Booking.Status.CANCELLED:
        return blocked(BLOCK_ALREADY_CANCELLED)
    if booking.status == Booking.Status.COMPLETED:
        return blocked(BLOCK_COMPLETED)
    if window == WINDOW_IN_PROGRESS:
        return blocked(BLOCK_IN_PROGRESS)
    if window == WINDOW_SAILED:
        return blocked(BLOCK_SAILED)
    if not ignore_pending and booking.has_pending_cancellation:
        return blocked(BLOCK_PENDING_REQUEST)
    # A live gateway session means money is on its way that paid_amount does not
    # yet know about. Quoting now would tell a customer with 43,700 taka in
    # flight that their refund is 0.00 — and an SSLCommerz session cannot be
    # voided once handed out, so we cannot simply cancel around it. Make them
    # finish or abandon the checkout first; abandoned ones close themselves
    # within the session window.
    if not ignore_pending and booking.has_live_payment_session():
        return blocked(BLOCK_PAYMENT_IN_PROGRESS)

    rule = resolve_rule(package.ship, days_until_start)
    if rule is None:
        # No configured schedule: refuse rather than invent a percentage.
        return blocked(BLOCK_NO_POLICY)

    percent = rule.percent_for(booking.booking_type)
    total = booking.total_amount
    paid = booking.paid_amount

    # The charge is a share of the CONTRACT value (what the tour costs), not of
    # the deposit — that is what the published table means by "% of the total
    # booking amount". It is then settled against what was actually received.
    charge = q2(total * percent / HUNDRED)
    refund = q2(max(paid - charge, ZERO))
    forfeited = q2(min(paid, charge))
    # Charge the deposit did not cover. Recorded, never billed: chasing a B2C
    # customer for the balance of a cancellation fee is not collectable here and
    # the published policy does not claim it. Group contracts are the exception
    # and are handled by a human.
    shortfall = q2(max(charge - paid, ZERO))

    return CancellationQuote(
        allowed=True,
        window=window,
        days_until_start=days_until_start,
        booking_type=booking.booking_type,
        tier_label=rule.label,
        charge_percent=percent,
        total_amount=total,
        paid_amount=paid,
        cancellation_charge=charge,
        refund_amount=refund,
        forfeited_amount=forfeited,
        shortfall_amount=shortfall,
        requires_approval=paid > ZERO,
        rule_id=rule.pk,
        computed_on=today.isoformat(),
    )


def operator_cancellation_quote(booking, *, today=None, note=""):
    """The company cancelling a sailing: involuntary, so no charge at all.

    Weather, a technical fault, or the passenger minimum not being met. The
    industry line between voluntary and involuntary cancellation is exactly this
    — when the customer did not choose it, the tier table does not apply and
    everything paid goes back.
    """
    today = today or timezone.localdate()
    paid = booking.paid_amount
    return CancellationQuote(
        allowed=True,
        window=package_window(booking.package, today),
        days_until_start=(booking.package.start_date - today).days,
        booking_type=booking.booking_type,
        tier_label="Operator cancellation — no charge",
        charge_percent=ZERO,
        total_amount=booking.total_amount,
        paid_amount=paid,
        cancellation_charge=ZERO,
        refund_amount=q2(paid),
        forfeited_amount=ZERO,
        shortfall_amount=ZERO,
        requires_approval=False,
        computed_on=today.isoformat(),
        _extra={"operator_reason": note} if note else {},
    )


def refund_claim_state(booking, today=None):
    """Whether a staff-raised refund on this booking is inside the normal claim
    window, for reasons that survive departure (overpayment, goodwill).

    Returns (within_window, days_since_end). Outside the window the API demands
    an explicit override, so a closed accounting month is never reopened by a
    stray click.
    """
    today = today or timezone.localdate()
    end = booking.package.end_date
    if today <= end:
        return True, 0
    days_since = (today - end).days
    return days_since <= booking.package.ship.refund_claim_window_days, days_since


def suggests_group(booking):
    """Whether this booking looks like a group by headcount. A hint for the
    staff UI only — booking_type is never set from it (see Booking.booking_type)."""
    threshold = booking.package.ship.group_min_pax
    return bool(threshold) and booking.total_pax >= threshold


def pending_request_for(booking):
    return booking.cancellation_requests.filter(
        status=CancellationRequest.Status.PENDING
    ).first()
