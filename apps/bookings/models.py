import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.packages.models import Package, PackageRoom
from apps.ships.models import Room

from .guests import clean_foreign_guests, guest_counts
from .pricing import price_breakdown, snapshot_breakdown


def generate_booking_code():
    """Unique human-readable code; also the SSLCommerz tran_id base (Phase 4).

    64 bits of entropy (token_hex(8) → 16 hex chars). This is the sole
    authorization token for a customer's own booking on the unauthenticated
    public API, so it must be broad enough that neither targeted guessing nor a
    birthday-style sweep is feasible (Phase 8a, F3 — was 32 bits)."""
    while True:
        code = "BK-" + secrets.token_hex(8).upper()
        if not Booking.objects.filter(booking_code=code).exists():
            return code


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # created, no successful payment yet
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        FULLY_PAID = "fully_paid", "Fully paid"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    class BookingType(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        GROUP = "group", "Group"

    booking_code = models.CharField(max_length=20, unique=True, blank=True)
    customer_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    email = models.EmailField()
    package = models.ForeignKey(
        Package, on_delete=models.PROTECT, related_name="bookings"
    )
    # Per-room occupancy (room, adult_count, kid_details) used to live here, one
    # room per booking. A family that needs 2–3 cabins is one booking with one
    # payment and one invoice, so the rooms moved to the BookingRoom child table
    # (each row its own room + pax + priced subtotal). Booking keeps only the
    # party-wide fields: customer, the single money truth, and status.
    # Customer's free-text note captured in the booking wizard (dietary needs,
    # accessibility, anniversary, etc.). Optional; surfaced to staff and the
    # guide. Length is bounded at the serializer (1000 chars) — this is the
    # only free-text field on the anonymous booking endpoint, so it must not be
    # an unbounded-text vector.
    special_requests = models.TextField(blank=True)
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    # Frozen copy of the price_breakdown() that produced total_amount, taken
    # when the booking is priced. KidPricingRule and Package.adult_price are
    # admin-editable by design, so recomputing the breakdown at render time
    # would re-price an already-paid booking and the invoice's line items
    # would stop summing to what the customer was actually charged (QA M1).
    # total_amount stays the money truth; this is the itemisation of it.
    price_snapshot = models.JSONField(default=dict, blank=True)
    paid_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    due_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    # Which column of the cancellation-charge schedule this booking is judged
    # by. Deliberately NOT derived from pax count: in this trade "group" is a
    # contract type (agent, corporate, tour operator), negotiated up front —
    # a family of twelve is still an individual booking. Ship.group_min_pax
    # only makes the dashboard SUGGEST the switch; a human decides.
    booking_type = models.CharField(
        max_length=12,
        choices=BookingType.choices,
        default=BookingType.INDIVIDUAL,
        help_text=(
            "Group bookings use the group column of the cancellation-charge "
            "schedule. Set by staff — never inferred from headcount."
        ),
    )
    # "We owe this customer money" is first-class, queryable state — never
    # just a log line. Set automatically whenever a booking with real money
    # on it is cancelled, or a verified payment settles on a dead session.
    # Refunds themselves are manual (phone/bKash); staff clear the flag once
    # the customer has been paid back.
    refund_required = models.BooleanField(default=False)
    refund_note = models.TextField(
        blank=True, help_text="Why a refund is owed / how it was settled."
    )
    # Set by enforce_due_deadlines when the balance reminder email goes out,
    # so the customer is nagged exactly once per booking.
    due_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The staff booking list sorts by -created_at and filters by status
            # and/or package; the dashboards Count/Sum grouped by status. None of
            # those columns was indexed, so each seq-scanned — fine at a handful
            # of rows, but bookings accrue for every sailing forever (QA phase8b
            # F2). (package_id/room_id already have FK indexes; availability's
            # (package,room,status!=cancelled) lookup is served by the partial
            # unique constraint below.)
            models.Index(fields=["status"], name="booking_status_idx"),
            models.Index(fields=["-created_at"], name="booking_created_idx"),
            models.Index(
                fields=["package", "status"], name="booking_package_status_idx"
            ),
        ]
        constraints = [
            # One active booking per (package, room) is now enforced on
            # BookingRoom (a booking may hold several rooms). See
            # BookingRoom.Meta.constraints.
            # No code path may ever persist a negative due — overpayment must
            # be rejected upstream (public and staff APIs both enforce the
            # ceiling; this is the backstop for future/raw ORM paths).
            models.CheckConstraint(
                condition=Q(due_amount__gte=0),
                name="booking_due_amount_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.booking_code} — {self.customer_name}"

    @property
    def total_pax(self):
        return sum(br.total_pax for br in self.rooms.all())

    @property
    def has_pending_cancellation(self):
        """True while a cancellation request is awaiting a staff decision.

        Deliberately NOT a Booking.status value. A pending request must not
        disturb the booking's real state (a fully_paid booking stays fully_paid,
        keeps its rooms, keeps counting as revenue) — otherwise a rejected
        request would need the old status restored from somewhere, and every
        status-driven job (sailing completion, due deadlines, availability)
        would need teaching about a state that means "nothing has happened yet".
        The request row is the state; this is the read.
        """
        if not self.pk:
            return False
        # Local import: apps.refunds imports this module, so a module-level
        # import here would be circular.
        from apps.refunds.models import CancellationRequest

        return self.cancellation_requests.filter(
            status=CancellationRequest.Status.PENDING
        ).exists()

    def reprice(self):
        """Recompute total_amount + price_snapshot from the booking's rooms.

        Server-side pricing — client-submitted amounts are never trusted. Each
        BookingRoom already carries its own frozen room_subtotal + per-room
        price_snapshot (set in BookingRoom.clean()); the booking total is their
        sum and the booking snapshot is the list of per-room snapshots, so the
        invoice can itemise every cabin.

        A booking is priced at creation and re-priced only while NO money has
        been quoted to the gateway against it. Pricing inputs (adult_price,
        base_price, KidPricingRule) are admin-editable by design, so a later
        re-price would rewrite what a paying customer owes. paid_amount > 0 is
        not a sufficient guard: a live PENDING session is money in flight while
        paid_amount is still 0.00, and re-pricing then means the customer is
        charged the amount they authorised but credited against a different
        total — or, if the price DROPPED, due goes negative and the
        non-negative CheckConstraint 500s the IPN and strands their money
        (QA C7). So any payment that is PENDING or SUCCESS freezes the price.
        """
        if self.paid_amount > 0 or self._has_money_in_flight():
            return
        rooms = list(self.rooms.all())
        self.total_amount = sum(
            (br.room_subtotal for br in rooms), Decimal("0.00")
        )
        self.price_snapshot = {"rooms": [br.price_snapshot for br in rooms]}

    def clean(self):
        # Per-room pax limits, availability and pricing now live on
        # BookingRoom.clean(); the booking-wide total is assembled by reprice()
        # once its rooms exist. Booking-level validation (cutoff) is enforced at
        # the serializer.
        pass

    def has_live_payment_session(self):
        """True while a gateway session could still take the customer's money.

        Deliberately NOT `_has_money_in_flight()`, which also counts SUCCESS —
        that one exists to freeze pricing, and reusing it here would refuse to
        cancel every booking that has ever paid.

        Bounded by PAYMENT_SESSION_MINUTES. A PENDING row older than the
        gateway's session lifetime can no longer settle (that is exactly what
        reconcile_pending_payments closes it on), and that job does not run at
        all on some hosting tiers — so without the bound one abandoned checkout
        would lock a customer out of cancelling forever.
        """
        if not self.pk:
            return False
        cutoff = timezone.now() - timedelta(
            minutes=getattr(settings, "PAYMENT_SESSION_MINUTES", 30)
        )
        return self.payments.filter(
            status=Payment.Status.PENDING, created_at__gte=cutoff
        ).exists()

    def _has_money_in_flight(self):
        """True if any gateway session has been handed out for this booking —
        i.e. an amount has been quoted to a customer and may still settle."""
        if not self.pk:
            return False
        return self.payments.filter(
            status__in=(Payment.Status.PENDING, Payment.Status.SUCCESS)
        ).exists()

    def save(self, *args, changed_by=None, silent=False, **kwargs):
        """silent=True suppresses the customer-facing cancellation email — used
        only by expire_stale_bookings, which reaps abandoned checkouts that the
        visitor never considered a booking (QA M6)."""
        if not self.booking_code:
            self.booking_code = generate_booking_code()
        # A cancelled booking has no collectable balance — nobody owes it
        # money and its room is released. paid_amount is kept intact: it is
        # the refund-owed signal.
        #
        # due is CLAMPED at zero. Overpayment is rejected upstream on every
        # write path, so paid > total should be unreachable — but the
        # non-negative CheckConstraint is an integrity net of last resort, and
        # a customer's settling payment must never be the thing that collides
        # with it: that turns a data bug into a 500 on the IPN, which strands
        # real money at the gateway and makes SSLCommerz retry forever (QA C7).
        # Any excess is money we owe back, so it is flagged rather than dropped.
        if self.status == self.Status.CANCELLED:
            self.due_amount = Decimal("0.00")
        else:
            self.due_amount = max(
                self.total_amount - self.paid_amount, Decimal("0.00")
            )
            if self.paid_amount > self.total_amount and not self.refund_required:
                self.refund_required = True
                overpaid = self.paid_amount - self.total_amount
                note = (
                    f"Paid {self.paid_amount} BDT against a total of "
                    f"{self.total_amount} BDT — {overpaid} BDT overpaid. "
                    "Refund the excess to the customer manually."
                )
                self.refund_note = (
                    f"{self.refund_note}\n{note}" if self.refund_note else note
                )
                update_fields = kwargs.get("update_fields")
                if update_fields is not None:
                    kwargs["update_fields"] = list(
                        dict.fromkeys(
                            list(update_fields)
                            + ["refund_required", "refund_note"]
                        )
                    )

        old_status = None
        if self.pk:
            old_status = (
                Booking.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )

        # Cancelling a booking that has verified money on it always owes the
        # customer a call (and possibly money back, per the cancellation-charge
        # schedule). Flag it here so every cancel path — staff API, admin,
        # deadline enforcement — is covered without remembering to opt in.
        if (
            self.status == self.Status.CANCELLED
            and old_status not in (None, self.Status.CANCELLED)
            and self.paid_amount > 0
            and not self.refund_required
        ):
            self.refund_required = True
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "refund_required" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["refund_required"]
        # due_amount is recomputed above on every save; a partial update that
        # touches status/paid must persist it too.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "due_amount" not in update_fields:
            kwargs["update_fields"] = list(update_fields) + ["due_amount"]
        super().save(*args, **kwargs)

        if old_status != self.status:
            # BookingRoom.is_active mirrors "the booking still holds this
            # room" — it is what the partial unique constraint and the public
            # availability query key on. Cancelling releases every room;
            # reactivating (e.g. staff un-cancel) re-holds them. The re-hold is
            # what the partial unique constraint on BookingRoom guards: if
            # another booking took the room meanwhile, this UPDATE raises and
            # the un-cancel is rejected (mirrors the old Booking-level guard).
            if self.status == self.Status.CANCELLED:
                self.rooms.filter(is_active=True).update(is_active=False)
            elif old_status == self.Status.CANCELLED:
                self.rooms.filter(is_active=False).update(is_active=True)

            BookingStatusLog.objects.create(
                booking=self,
                old_status=old_status or "",
                new_status=self.status,
                changed_by=changed_by,
            )
            # Every deliberate cancel path — deadline cron, staff API, Django
            # admin — tells the customer, without having to opt in (QA M3).
            # After commit so email trouble can never roll the cancellation
            # back.
            #
            # EXCEPT the expiry of an abandoned checkout, which passes
            # silent=True. A visitor who closed the tab before paying never had
            # a booking as far as they are concerned; mailing them "your booking
            # has been cancelled", citing a deposit and a cancellation-charge
            # schedule, is confusing, and at ~every abandoned cart it is a
            # deliverability risk on the domain we send real invoices from
            # (QA M6). An abandoned-cart nudge, if the client wants one, is a
            # different message with different copy.
            if (
                self.status == self.Status.CANCELLED
                and old_status is not None
                and old_status != self.Status.CANCELLED
                and not silent
            ):
                from . import invoices

                transaction.on_commit(
                    lambda: invoices.send_cancellation_email(self)
                )

    def refresh_paid_amount(self):
        """Recompute paid/due from successful payments (never incremented) and
        move the booking through the payment statuses.

        Runs under a lock on the booking row: two payments settling in
        overlapping transactions would otherwise each SUM before the other
        commits (READ COMMITTED), and the last writer would record only its
        own amount — settled money vanishing from paid_amount."""
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(pk=self.pk)
            paid = booking.payments.filter(status=Payment.Status.SUCCESS).aggregate(
                total=Sum("amount")
            )["total"] or Decimal("0.00")
            booking.paid_amount = paid
            if booking.status not in (self.Status.CANCELLED, self.Status.COMPLETED):
                if paid >= booking.total_amount and paid > 0:
                    booking.status = self.Status.FULLY_PAID
                elif paid > 0:
                    booking.status = self.Status.PARTIALLY_PAID
            booking.save(
                update_fields=["paid_amount", "due_amount", "status", "updated_at"]
            )
            # Keep the caller's instance in sync with what was persisted.
            self.paid_amount = booking.paid_amount
            self.due_amount = booking.due_amount
            self.status = booking.status


class BookingRoom(models.Model):
    """One cabin within a booking: its room, that room's own party (adults +
    kids), and the priced subtotal for it.

    A booking may hold several rooms (a big family taking 2–3 cabins), each with
    its own pax and its own room-type limits. Money stays booking-wide: the
    booking's total_amount is the sum of every room_subtotal here, and there is
    one payment and one invoice for the whole booking.
    """

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="rooms"
    )
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, related_name="booking_rooms"
    )
    # Denormalised from booking.package so the "one active booking per
    # (package, room)" rule can be a DB-level partial unique constraint —
    # constraints cannot span a relation (booking__package), so the package is
    # copied onto the row. Kept in sync in save().
    package = models.ForeignKey(
        Package, on_delete=models.PROTECT, related_name="booking_rooms"
    )
    adult_count = models.PositiveSmallIntegerField()
    kid_details = models.JSONField(
        default=list,
        blank=True,
        help_text='List of kids as [{"age": 5}, ...]. Ages drive kid pricing.',
    )
    # Foreign nationals in THIS cabin — one entry per foreign guest, each
    # carrying that guest's passport. Same idiom as kid_details (a JSON list of
    # dicts on the row that is priced) rather than a side table, because the
    # list is both a pricing input and a per-cabin record: BookingRoom.clean()
    # prices itself, and a child table's rows cannot exist before the row they
    # point at is saved.
    #
    # Entry shape — passport_number is the only required key (client policy):
    #   {"guest_type": "adult"|"kid",   # which fare this guest sits on
    #    "passport_number": "A1234567", # REQUIRED, normalised uppercase
    #    "full_name": "…",              # optional
    #    "nationality": "US",           # optional, ISO 3166-1 alpha-2
    #    "passport_expiry": "2031-04-09"}  # optional, ISO date
    #
    # The COUNTS here are the surcharge quantity: each foreign guest pays the
    # global ForeignerSurcharge rate for their fare type, on top of their
    # ordinary fare. Guests listed here are NOT extra pax — they are a subset of
    # adult_count / kid_details, so the totals never double-count.
    foreign_guests = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            'Foreign nationals in this cabin, e.g. [{"guest_type": "adult", '
            '"passport_number": "A1234567"}]. A subset of the cabin\'s pax — '
            "each one adds the package's foreigner surcharge."
        ),
    )
    # Frozen priced subtotal for THIS room and its per-room itemisation, taken
    # when the room is priced (same freeze rules as the booking — see
    # Booking.reprice()). The booking's total_amount sums these; the invoice
    # itemises each room from its own price_snapshot.
    room_subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    price_snapshot = models.JSONField(default=dict, blank=True)
    # Mirrors "the booking still holds this room". Set False when the booking is
    # cancelled (Booking.save()), which frees the room for resale; the partial
    # unique constraint and the public availability query both key on it.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["room__room_number"]
        constraints = [
            # One active hold per (package, room): a cancelled booking's rooms
            # go is_active=False and free the cabin. Replaces the old
            # Booking-level uniq_active_booking_per_package_room.
            models.UniqueConstraint(
                fields=["package", "room"],
                condition=Q(is_active=True),
                name="uniq_active_bookingroom_per_package_room",
            ),
        ]

    def __str__(self):
        return f"{self.booking.booking_code} — Room {self.room.room_number}"

    @property
    def kid_ages(self):
        return [kid["age"] for kid in self.kid_details]

    @property
    def total_pax(self):
        return self.adult_count + len(self.kid_details)

    @property
    def foreign_pax(self):
        """(foreign_adults, foreign_kids) in this cabin — the surcharge
        quantities. A subset of total_pax, never added to it."""
        return guest_counts(self.foreign_guests)

    @property
    def has_foreign_guests(self):
        return bool(self.foreign_guests)

    def clean(self):
        if not self.room_id:
            return  # field-level "required" error already covers this
        # package is copied from the booking; it must be present to validate
        # availability and to price against the package's adult_price.
        package = self.package or (self.booking.package if self.booking_id else None)
        if package is None:
            return

        errors = {}
        room_type = self.room.room_type

        if self.adult_count is not None:
            if self.adult_count < 1:
                errors["adult_count"] = "At least one adult is required."
            elif self.adult_count > room_type.max_adults:
                errors["adult_count"] = (
                    f"{room_type.name} allows at most {room_type.max_adults} adults."
                )

        if not isinstance(self.kid_details, list) or not all(
            isinstance(kid, dict) and isinstance(kid.get("age"), int) and kid["age"] >= 0
            for kid in self.kid_details
        ):
            errors["kid_details"] = (
                'kid_details must be a list like [{"age": 5}] with non-negative '
                "integer ages."
            )
        elif len(self.kid_details) > room_type.max_kids:
            errors["kid_details"] = (
                f"{room_type.name} allows at most {room_type.max_kids} kids."
            )

        # Foreign guests are a SUBSET of the cabin's pax, so they can only be
        # checked once the pax counts themselves are sound — otherwise the
        # "3 foreign adults but only N adults" message would quote a count the
        # customer is already being told is invalid.
        if "adult_count" not in errors and "kid_details" not in errors:
            try:
                self.foreign_guests = clean_foreign_guests(
                    self.foreign_guests,
                    adult_count=self.adult_count or 0,
                    kid_count=len(self.kid_details),
                )
            except ValidationError as exc:
                errors["foreign_guests"] = exc.messages

        # Membership only: the room must actually be attached to the package
        # (so we can price against it). Availability — is_available, the admin
        # is_blocked hold, and the "already actively booked" check — is NO
        # LONGER read here. It is validated by the booking flow under a
        # SELECT ... FOR UPDATE lock on the PackageRoom row
        # (PackageRoom.assert_bookable, called from BookingCreateSerializer.
        # create), so a plain unlocked .exists() here would only re-introduce
        # the exact race this refactor closes: the value could flip between
        # this read and the write. Both the public and staff booking paths run
        # through that locked check.
        if not PackageRoom.objects.filter(
            package=package, room_id=self.room_id
        ).exists():
            errors["room"] = "This room is not part of the selected package."

        if errors:
            raise ValidationError(errors)

        # Server-side pricing for this room — client amounts are never trusted.
        # The freeze guard lives on the booking (Booking.reprice): once money is
        # quoted the whole booking's pricing is frozen, so a room is only priced
        # here while its booking is still repriceable.
        if not self.booking_id or (
            self.booking.paid_amount <= 0 and not self.booking._has_money_in_flight()
        ):
            foreign_adults, foreign_kids = self.foreign_pax
            breakdown = price_breakdown(
                room_type,
                package,
                self.adult_count,
                self.kid_ages,
                foreign_adults=foreign_adults,
                foreign_kids=foreign_kids,
            )
            self.room_subtotal = breakdown["total"]
            self.price_snapshot = snapshot_breakdown(
                breakdown, room_number=self.room.room_number
            )

    def save(self, *args, **kwargs):
        # Keep the denormalised package in step with the booking it belongs to.
        if self.booking_id and self.package_id != self.booking.package_id:
            self.package_id = self.booking.package_id
        super().save(*args, **kwargs)


class BookingStatusLog(models.Model):
    """Audit trail of booking status changes (PRD §6)."""

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="status_logs"
    )
    old_status = models.CharField(max_length=20, blank=True)
    new_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.booking.booking_code}: {self.old_status or '—'} → {self.new_status}"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class PaymentType(models.TextChoices):
        FULL = "full", "Full"
        PARTIAL = "partial", "Partial"

    booking = models.ForeignKey(
        Booking, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_type = models.CharField(max_length=10, choices=PaymentType.choices)
    gateway = models.CharField(max_length=30, default="sslcommerz")
    transaction_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    # The gateway checkout URL handed to the customer. Kept so an identical
    # re-request (double-click, back button, reopened tab) is answered with
    # the SAME live session instead of minting a second payable one — an
    # SSLCommerz session cannot be voided once issued (QA H4).
    gateway_url = models.URLField(max_length=500, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Set by reconcile_pending_payments when the gateway will not say what
    # happened on this session. A payment stuck here blocks its room from
    # being released, so it must be escalated to a human, never left to spin
    # silently (QA H5).
    reconcile_attempts = models.PositiveSmallIntegerField(default=0)
    last_reconcile_error = models.TextField(blank=True)
    needs_manual_review = models.BooleanField(default=False)
    # When the gateway was last asked about this payment. Escalated payments
    # are retried on a slow back-off rather than abandoned (QA H7) — a gateway
    # outage ends, and a payment we stop asking about holds its cabin out of
    # inventory forever. This is the key that back-off is measured from.
    last_reconcile_at = models.DateTimeField(null=True, blank=True)
    gateway_payload = models.JSONField(
        default=dict, blank=True, help_text="Raw gateway (SSLCommerz) response."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The reconcile job scans PENDING payments and the dashboard scans
            # SUCCESS ones; status was unindexed (QA phase8b F2). A partial index
            # on the PENDING rows keeps the reconcile queue — which decides which
            # cabins are still held — cheap no matter how many settled payments
            # pile up behind it (settled rows are the vast majority over time).
            models.Index(
                fields=["created_at"],
                condition=Q(status="pending"),
                name="payment_pending_idx",
            ),
        ]
        constraints = [
            # One payment row per gateway tran_id — duplicate IPNs can never
            # create a second credit.
            models.UniqueConstraint(
                fields=["transaction_id"],
                condition=~Q(transaction_id=""),
                name="uniq_payment_transaction_id",
            ),
            # Payment.clean() rejects non-positive amounts, but DRF serializers
            # never call clean() — this makes the rule un-bypassable from any
            # ORM path (staff API, admin, shell, future code). A mis-keyed
            # negative "payment" would silently erase settled money from the
            # ledger; reversals must be explicit operations, not negative rows.
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="payment_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.booking.booking_code}: {self.amount} ({self.status})"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Amount must be positive."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.booking.refresh_paid_amount()


def invoice_pdf_path(invoice, filename):
    """Storage path for an invoice PDF.

    The old scheme was invoices/INV-<booking_code>-<pk>.pdf — fully derivable
    from data the customer already has (the booking code is in their own
    confirmation URL and email) plus a small sequential integer, so one
    customer could enumerate everyone else's invoices (QA C1). The file now
    lives behind an unguessable token, which also means a leaked link cannot
    be walked to a *different* invoice.
    """
    return f"invoices/{invoice.access_token}.pdf"


def select_invoice_storage():
    """Resolve the "invoices" STORAGES alias at runtime.

    Invoice PDFs hold customer PII, so in production they live in their own
    private bucket with a much shorter presigned-URL TTL than public imagery
    (settings.STORAGES["invoices"]); locally and under test the alias is the
    plain filesystem. A callable keeps the choice out of migrations.
    """
    return storages["invoices"]


class Invoice(models.Model):
    class SentVia(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"  # Phase 2

    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, related_name="invoices"
    )
    # What this invoice is an invoice *for*. An invoice attests to money
    # received, so it is issued against the payment that settled — not floating
    # free against the booking, where nothing bounded how many could exist or
    # whether any money backed them at all (QA M3). Nullable only so the
    # migration can adopt pre-existing rows.
    payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )
    # Issued number, stored — not derived at render time. A rendered number
    # would change if the row moved, and could not be constrained by the DB.
    number = models.CharField(max_length=40, unique=True, editable=False, blank=True)
    # Unguessable capability token: it names the PDF on disk and authorises the
    # customer's own download link. secrets.token_urlsafe(32) ≈ 256 bits.
    access_token = models.CharField(
        max_length=64, unique=True, editable=False, blank=True
    )
    # The money this invoice ATTESTS TO, frozen at issue time. The booking's
    # live totals move (the customer keeps paying); an issued invoice must not.
    # Both the PDF and its covering email read these, so they can never
    # contradict each other (QA M2).
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    paid_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    due_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    booking_status = models.CharField(max_length=20, blank=True)
    sent_via = models.CharField(
        max_length=10, choices=SentVia.choices, default=SentVia.EMAIL
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    pdf_file = models.FileField(
        upload_to=invoice_pdf_path, storage=select_invoice_storage, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} ({self.booking.booking_code})"

    @property
    def fully_paid(self):
        """PAID IN FULL is a statement about the booking's STATUS, not merely
        about due==0: a cancelled booking also has due==0 (it is uncollectable,
        not settled), and stamping 'PAID IN FULL' on it was wrong (QA M3).

        booking_status is blank only on rows issued before it existed; those
        fall back to the booking's live status."""
        status = self.booking_status or self.booking.status
        return status == Booking.Status.FULLY_PAID

    def save(self, *args, **kwargs):
        if not self.access_token:
            self.access_token = secrets.token_urlsafe(32)
        if not self.number:
            self.number = self._next_number()
        super().save(*args, **kwargs)

    def _next_number(self):
        """Gapless-per-year sequential number: INV-<YYYY>-<00001>.

        Allocated under a row lock on the counter so two concurrent invoices
        can never draw the same number — an accounting series must be
        sequential and unique, which a pk-derived string was not (QA note).
        """
        year = timezone.localdate().year
        with transaction.atomic():
            counter, _ = InvoiceCounter.objects.select_for_update().get_or_create(
                year=year
            )
            counter.last_number += 1
            counter.save(update_fields=["last_number"])
            return f"INV-{year}-{counter.last_number:05d}"


class InvoiceCounter(models.Model):
    """Per-year invoice sequence. One row per year; locked while allocating."""

    year = models.PositiveIntegerField(primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.year}: {self.last_number}"
