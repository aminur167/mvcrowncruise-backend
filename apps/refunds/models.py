"""Cancellation requests and refunds.

Three models, three jobs:

- ``CancellationRule`` — the cancellation-charge schedule as DATA. The seven
  tiers the policy page prints ("3 weeks before departure → 5%") used to be a
  hardcoded array in the React route; they live here so the admin can edit them
  and the site can never disagree with the contract. Per-ship, because the
  system is multi-ship by design.
- ``CancellationRequest`` — a customer asking to cancel. It is a REQUEST, not a
  cancellation: the booking keeps its status and its rooms until staff approve.
  Every money figure is frozen at submission time (see ``policy_snapshot``).
- ``Refund`` — money the company owes a customer, from any cause: a cancellation,
  an overpayment, a duplicate gateway settlement, or a goodwill decision. This
  is the accounting record; ``Booking.refund_required`` remains as the "we owe
  this customer" flag the staff dashboard already keys on.

Money is Decimal everywhere and every amount is computed server-side from the
booking's own paid_amount and the rules above — a client never submits an
amount, and a refund is never larger than what was actually paid.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.bookings.models import Booking
from apps.ships.models import Ship

ZERO = Decimal("0.00")


class PayoutMethod(models.TextChoices):
    """How money physically goes back to a customer.

    Shared by CancellationRequest (what the customer asked for) and Refund
    (what staff actually did), so the two can never drift apart. GATEWAY is
    listed for the day SSLCommerz refund permission is enabled on the merchant
    account — today every payout is settled by hand.
    """

    BKASH = "bkash", "bKash"
    NAGAD = "nagad", "Nagad"
    BANK_TRANSFER = "bank_transfer", "Bank transfer"
    CASH = "cash", "Cash"
    GATEWAY = "gateway", "Back to the payment gateway"


class CancellationRule(models.Model):
    """One tier of the cancellation-charge schedule.

    Resolution (see ``policy.resolve_rule``): given how many whole days remain
    before departure, the applicable tier is the one with the LARGEST
    ``days_before_start`` that is still <= that number. So the seven standard
    rows (21, 14, 7, 3, 2, 1, 0) mean "cancel 21+ days out → 5%", and the 0 row
    is the catch-all for the final day. A booking cancelled two months ahead
    still lands on the 21-day row: there is deliberately no free tier unless the
    admin adds one (e.g. days_before_start=60 at 0%).

    ``ship`` blank = the default schedule, used by every ship that has no rows
    of its own. A ship with even one active row of its own uses ONLY its own
    rows — schedules are never merged, because a half-inherited charge table is
    impossible to reason about when money is on the line.
    """

    ship = models.ForeignKey(
        Ship,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="cancellation_rules",
        help_text=(
            "Leave blank for the default schedule used by every ship. Set a "
            "ship to give that ship its own complete schedule (its rows then "
            "replace the default entirely)."
        ),
    )
    days_before_start = models.PositiveSmallIntegerField(
        help_text=(
            "Applies when the cancellation lands this many whole days or more "
            "before departure (and fewer than the next tier up). Use 0 for the "
            "final-day catch-all."
        )
    )
    label = models.CharField(
        max_length=60,
        help_text='Shown to the customer, e.g. "3 weeks before departure".',
    )
    individual_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal("100.00"))],
        help_text="% of the booking total charged on an individual booking.",
    )
    group_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal("100.00"))],
        help_text="% charged on a group booking (see Booking.booking_type).",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-days_before_start"]
        constraints = [
            # NULL ship means "default schedule", and in Postgres NULLs are
            # distinct — so a single unique_together would let the default
            # schedule hold five rows all at 21 days. Two partial constraints
            # cover both halves properly.
            models.UniqueConstraint(
                fields=["ship", "days_before_start"],
                condition=Q(ship__isnull=False),
                name="uniq_ship_cancellation_tier",
            ),
            models.UniqueConstraint(
                fields=["days_before_start"],
                condition=Q(ship__isnull=True),
                name="uniq_default_cancellation_tier",
            ),
            models.CheckConstraint(
                condition=Q(individual_percent__gte=0)
                & Q(individual_percent__lte=100)
                & Q(group_percent__gte=0)
                & Q(group_percent__lte=100),
                name="cancellation_percent_within_0_100",
            ),
        ]

    def __str__(self):
        scope = self.ship.name if self.ship_id else "default"
        return f"[{scope}] {self.label} — {self.individual_percent}% / {self.group_percent}%"

    def percent_for(self, booking_type):
        return (
            self.group_percent
            if booking_type == Booking.BookingType.GROUP
            else self.individual_percent
        )


class CancellationRequest(models.Model):
    """A customer's (or staff's) request to cancel a booking.

    Nothing about the booking changes when this is created — the rooms stay
    held and the status stays whatever it was. Only an approval cancels the
    booking, which is what makes the flow safe to expose on an unauthenticated,
    booking-code-authorised endpoint: the worst a stranger with a leaked code
    can do is put a request in a queue a human then reads.

    EVERY money figure here is frozen at ``requested_at``. If staff sit on the
    queue for two days and the booking crosses into a harsher tier meanwhile,
    the customer must not pay for that delay — so approval never recomputes,
    it only says yes or no to the numbers already stored.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn by customer"

    class Reason(models.TextChoices):
        PLANS_CHANGED = "plans_changed", "Plans changed"
        MEDICAL = "medical", "Illness / emergency"
        DATE_CHANGE = "date_change", "Wants a different date"
        BOOKED_BY_MISTAKE = "booked_by_mistake", "Booked by mistake"
        OTHER = "other", "Other"

    class Source(models.TextChoices):
        CUSTOMER = "customer", "Customer (website)"
        STAFF = "staff", "Staff (phone / desk)"

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="cancellation_requests"
    )
    source = models.CharField(
        max_length=10, choices=Source.choices, default=Source.CUSTOMER
    )
    reason_code = models.CharField(max_length=24, choices=Reason.choices)
    reason_note = models.TextField(blank=True)

    # ---- Frozen at submission (never recomputed) -------------------------
    booking_type = models.CharField(max_length=12, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    cancellation_charge = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO
    )
    refund_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        help_text="max(0, paid − charge). Never negative: a refund cannot bill.",
    )
    forfeited_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        help_text="min(paid, charge) — what the company keeps.",
    )
    shortfall_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        help_text=(
            "max(0, charge − paid): charge the deposit did not cover. Recorded "
            "for reporting only — waived by default, never invoiced."
        ),
    )
    policy_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "The tier, percentages and dates this quote was computed from. "
            "Editing the schedule later must not move an existing request."
        ),
    )

    # ---- Where the money goes back ---------------------------------------
    # The payment came in through SSLCommerz but refunds are settled by hand,
    # so the destination has to be collected from the customer or the whole
    # queue turns into a phone-call exercise. It is routinely NOT the booking
    # phone (husband books, wife's bKash), hence its own fields.
    refund_method = models.CharField(
        max_length=16, choices=PayoutMethod.choices, blank=True
    )
    refund_account_name = models.CharField(max_length=120, blank=True)
    refund_account_number = models.CharField(max_length=40, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    branch_name = models.CharField(max_length=120, blank=True)

    # ---- Workflow ---------------------------------------------------------
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancellation_decisions",
    )
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status", "-requested_at"], name="cxreq_status_idx"),
        ]
        constraints = [
            # One open request per booking. Without this, a double-submit (or a
            # refresh on the confirm screen) queues two requests and a
            # distracted approver could cancel-and-refund the same booking
            # twice. Mirrors the partial-unique pattern already used for room
            # holds on BookingRoom.
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(status="pending"),
                name="one_open_cancellation_request_per_booking",
            ),
            models.CheckConstraint(
                condition=Q(refund_amount__gte=0)
                & Q(cancellation_charge__gte=0)
                & Q(forfeited_amount__gte=0)
                & Q(shortfall_amount__gte=0),
                name="cancellation_request_amounts_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(refund_amount__lte=models.F("paid_amount")),
                name="cancellation_refund_within_paid",
            ),
        ]

    def __str__(self):
        return f"{self.booking.booking_code} — {self.get_status_display()}"

    @property
    def is_open(self):
        return self.status == self.Status.PENDING


class Refund(models.Model):
    """Money owed back to a customer, and the record of paying it.

    Created ``PENDING`` (a liability), moved to ``PAID`` when staff record how
    they actually sent the money. Rows are effectively append-only: a mistake is
    corrected with a new row, not by editing history, so the refund register
    always reconciles against the gateway settlement.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending payout"
        PAID = "paid", "Paid"
        REJECTED = "rejected", "Rejected"
        VOID = "void", "Void / raised in error"

    class Reason(models.TextChoices):
        # Customer cancelled — the tier schedule applies.
        CUSTOMER_CANCELLATION = "customer_cancellation", "Customer cancellation"
        # WE cancelled (weather, minimum pax, technical). Involuntary: no
        # cancellation charge is ever applied, the customer gets everything back.
        OPERATOR_CANCELLATION = "operator_cancellation", "Operator cancellation"
        # Money that was never ours: no tier, no approval question.
        OVERPAYMENT = "overpayment", "Overpayment"
        DUPLICATE_PAYMENT = "duplicate_payment", "Duplicate payment"
        # Service failure / gesture. Amount is a human decision, note required.
        GOODWILL = "goodwill", "Goodwill / service issue"

    #: Alias so call sites read Refund.Method.BKASH alongside Refund.Status.
    Method = PayoutMethod

    booking = models.ForeignKey(
        Booking, on_delete=models.PROTECT, related_name="refunds"
    )
    cancellation_request = models.OneToOneField(
        CancellationRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="refund",
    )
    reason = models.CharField(max_length=24, choices=Reason.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    cancellation_charge = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO
    )
    policy_snapshot = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    method = models.CharField(
        max_length=16, choices=PayoutMethod.choices, blank=True
    )
    account_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    branch_name = models.CharField(max_length=120, blank=True)
    reference_no = models.CharField(
        max_length=64,
        blank=True,
        help_text="bKash/bank transaction id — required to reconcile the payout.",
    )
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="refunds_created",
        help_text="Null when raised automatically by the system.",
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="refunds_processed",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="refund_status_idx"),
            models.Index(fields=["reason"], name="refund_reason_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gte=0), name="refund_amount_non_negative"
            ),
            # A second open refund on the same booking is always a mistake
            # (double-click, two staff working the same queue) and it is the one
            # mistake that costs real money twice.
            models.UniqueConstraint(
                fields=["booking"],
                condition=Q(status="pending"),
                name="one_open_refund_per_booking",
            ),
        ]

    def __str__(self):
        return f"{self.booking.booking_code} — {self.amount} BDT ({self.get_status_display()})"

    @property
    def masked_account_number(self):
        """For list views and anywhere a payout destination does not need to be
        fully legible. The full number is only shown on the detail screen."""
        if not self.account_number:
            return ""
        tail = self.account_number[-4:]
        return f"{'•' * max(len(self.account_number) - 4, 0)}{tail}"


class RefundStatusLog(models.Model):
    """Audit trail for refund state changes — who moved this money and when.

    Same role BookingStatusLog plays for bookings; a payout with no attributable
    author is not auditable.
    """

    refund = models.ForeignKey(
        Refund, on_delete=models.CASCADE, related_name="status_logs"
    )
    old_status = models.CharField(max_length=10, blank=True)
    new_status = models.CharField(max_length=10)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="refund_status_changes",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.refund_id}: {self.old_status or '—'} → {self.new_status}"
