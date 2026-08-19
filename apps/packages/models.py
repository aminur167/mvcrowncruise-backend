from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields.ranges import RangeOperators
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.utils import timezone

from apps.ships.models import Room, Ship


class PackageQuerySet(models.QuerySet):
    def public(self):
        """Packages visible on the public API/website.

        Only OPEN, not-yet-finished tours. Packages past their booking cutoff
        stay visible (with is_bookable() False) so the calendar doesn't
        silently lose tours; draft/cancelled/completed never appear.
        """
        return self.filter(
            status=Package.Status.OPEN, end_date__gte=timezone.localdate()
        )


class Package(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    ship = models.ForeignKey(Ship, on_delete=models.PROTECT, related_name="packages")
    start_date = models.DateField()
    end_date = models.DateField()
    booking_cutoff_datetime = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Leave blank to auto-set to 12:00 PM (noon) the day before the "
            "tour start date."
        ),
    )
    adult_price = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Charge per adult for this package."
    )
    # Both knobs below are business policy, so they are data (admin-editable,
    # per sailing) — never constants in code.
    min_deposit_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("50.00"),
        # Bounded 1-100. This value is load-bearing for room inventory: at 0 a
        # customer holds a cabin for one paisa, and a partially_paid booking is
        # exempt from hold expiry — which is precisely the bug this field was
        # added to prevent (QA C2/M5). Above 100 the floor exceeds the total and
        # partial payment is silently impossible. Neither is a business setting.
        validators=[
            MinValueValidator(Decimal("1.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
        help_text=(
            "Minimum first payment as % of the booking total, 1-100 (invoice "
            "policy: confirmation requires a 50% advance). Top-ups toward an "
            "existing balance are exempt."
        ),
    )
    balance_due_days_before_start = models.PositiveSmallIntegerField(
        default=3,
        help_text=(
            "Days before departure by which the remaining balance must be "
            "settled (deadline is noon that day). Partially paid bookings past "
            "the deadline are cancelled and flagged for a manual refund call."
        ),
    )
    # The foreign-national surcharge is NOT here: it is a global policy row
    # (ForeignerSurcharge below), not a per-sailing price. See that model for
    # why it moved.
    # Displayed duration ("3 Days · 2 Nights") for the public package card.
    # Both blank => auto-derived from the dates (nights = date span, days =
    # nights + 1), so a normal sailing needs no data entry. Staff may override
    # either from the dashboard when the marketed duration differs from the raw
    # calendar span. Never hardcoded on the frontend — the API sends the
    # resolved values (see effective_days/effective_nights).
    duration_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Leave blank to auto-calculate (nights + 1) from the dates.",
    )
    duration_nights = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Leave blank to auto-calculate from the dates (end − start).",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    is_booking_open = models.BooleanField(
        default=True,
        help_text="Manual override: uncheck to close booking regardless of cutoff.",
    )
    # Admin-editable marketing copy so the public website's package cards can
    # be sourced from real, bookable Package rows instead of hardcoded content.
    marketing_title = models.CharField(
        max_length=100, blank=True, help_text='e.g. "Sundarbans Explorer".'
    )
    marketing_description = models.TextField(blank=True)
    hero_image = models.ImageField(upload_to="packages/hero/", blank=True)
    highlights = models.JSONField(
        default=list,
        blank=True,
        help_text='List of short strings, e.g. ["Mangrove safari", "Sunset dinner"].',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PackageQuerySet.as_manager()

    class Meta:
        ordering = ["-start_date"]
        indexes = [
            # public() = status='open' AND end_date>=today runs on EVERY public
            # read; the list/calendar order and range-filter on the dates. None
            # of status/start_date/end_date was indexed (QA phase8b F2). ship_id
            # already has an FK index; these cover the rest.
            models.Index(fields=["status", "end_date"], name="package_status_end_idx"),
            models.Index(fields=["start_date"], name="package_start_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gt=models.F("start_date")),
                name="package_end_after_start",
            ),
            # DB-level backstop for clean()'s overlap check: no two active
            # packages on one ship may cover the same nights, even via raw ORM
            # writes or two staff sessions racing. daterange() defaults to
            # half-open [), so same-day turnaround stays allowed.
            ExclusionConstraint(
                name="excl_ship_package_date_overlap",
                expressions=[
                    ("ship", RangeOperators.EQUAL),
                    (
                        models.Func(
                            models.F("start_date"),
                            models.F("end_date"),
                            function="daterange",
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=~models.Q(status__in=("draft", "cancelled")),
            ),
        ]

    def __str__(self):
        return f"{self.ship.name}: {self.start_date} – {self.end_date}"

    # Statuses that occupy the ship's calendar. DRAFT and CANCELLED packages
    # never conflict — only real (sellable or sailed) voyages do.
    ACTIVE_STATUSES = (Status.OPEN, Status.CLOSED, Status.COMPLETED)

    def clean(self):
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValidationError({"end_date": "End date must be after start date."})
        # One ship cannot run two voyages over the same nights. Same-day
        # turnaround (this end_date == next start_date) is allowed, so the
        # comparison is half-open: [start_date, end_date).
        if (
            self.ship_id
            and self.start_date
            and self.end_date
            and self.status in self.ACTIVE_STATUSES
        ):
            overlap = (
                Package.objects.exclude(pk=self.pk)
                .filter(
                    ship_id=self.ship_id,
                    status__in=self.ACTIVE_STATUSES,
                    start_date__lt=self.end_date,
                    end_date__gt=self.start_date,
                )
                .first()
            )
            if overlap:
                raise ValidationError(
                    {
                        "start_date": (
                            "Dates overlap with another package on this ship: "
                            f"{overlap} — the same room would be sold twice."
                        )
                    }
                )
        # A cutoff after departure day would keep the public booking API
        # selling cabins while the ship is at sea (the cutoff is the only
        # time gate). Validate the value save() will actually store, so a
        # date move that resyncs an auto-derived cutoff isn't rejected.
        if self.start_date and self.booking_cutoff_datetime:
            effective = self._resolved_cutoff()
            if timezone.localdate(effective) > self.start_date:
                raise ValidationError(
                    {
                        "booking_cutoff_datetime": (
                            "Cutoff is after the departure date "
                            f"({self.start_date}) — bookings would stay open "
                            "during the voyage."
                        )
                    }
                )

        # Also checked here, not only by the field validators: clean() is the
        # gate every non-form path goes through, and a 0% floor would let one
        # paisa hold a cabin permanently (QA M5).
        if self.min_deposit_percent is not None and not (
            Decimal("1") <= self.min_deposit_percent <= Decimal("100")
        ):
            raise ValidationError(
                {
                    "min_deposit_percent": (
                        "Must be between 1 and 100. A 0% deposit would let a "
                        "customer hold a cabin indefinitely without paying."
                    )
                }
            )

        # Duration overrides are optional, but if set they must be sane and
        # mutually consistent — a nights value ≥ days ("3 Days · 3 Nights")
        # is exactly the kind of wrong copy this field exists to prevent.
        if self.duration_nights is not None and self.duration_nights < 1:
            raise ValidationError(
                {"duration_nights": "Must be at least 1 night."}
            )
        if self.duration_days is not None and self.duration_days < 2:
            raise ValidationError(
                {"duration_days": "A multi-day tour spans at least 2 days."}
            )
        days = self.effective_days()
        nights = self.effective_nights()
        if days is not None and nights is not None and days != nights + 1:
            raise ValidationError(
                {
                    "duration_days": (
                        f"Days ({days}) must be exactly one more than nights "
                        f"({nights}). Leave both blank to auto-calculate."
                    )
                }
            )

    @staticmethod
    def cutoff_default_for(start_date):
        """Noon (Asia/Dhaka) the day before the given start date — PRD §5.5."""
        naive = datetime.combine(start_date - timedelta(days=1), time(12, 0))
        return timezone.make_aware(naive, timezone.get_default_timezone())

    def balance_due_at(self):
        """Deadline for settling a booking's remaining balance: noon local
        time, balance_due_days_before_start days before departure. Mirrors
        the booking-cutoff pattern (invoice policy: "the remaining balance
        must be settled before the journey")."""
        naive = datetime.combine(
            self.start_date - timedelta(days=self.balance_due_days_before_start),
            time(12, 0),
        )
        return timezone.make_aware(naive, timezone.get_default_timezone())

    def effective_nights(self):
        """Nights shown on the public card. Admin override wins; otherwise the
        raw calendar span (10 Aug → 12 Aug = 2 nights)."""
        if self.duration_nights is not None:
            return self.duration_nights
        if self.start_date and self.end_date:
            return (self.end_date - self.start_date).days
        return None

    def effective_days(self):
        """Days shown on the public card. Admin override wins; otherwise
        nights + 1 (a 2-night sailing spans 3 calendar days). This is the
        off-by-one the frontend was getting wrong — days is nights + 1, not the
        bare date difference."""
        if self.duration_days is not None:
            return self.duration_days
        nights = self.effective_nights()
        return nights + 1 if nights is not None else None

    def default_cutoff(self):
        return self.cutoff_default_for(self.start_date)

    def _resolved_cutoff(self):
        """The cutoff save() will store for the current field values.

        Blank derives from the start date. On update, a cutoff that was still
        the auto-derived default for the *previous* start date (i.e. never
        hand-picked) follows the dates when they move; a manually customized
        cutoff is kept as-is.
        """
        if self.booking_cutoff_datetime is None:
            return self.default_cutoff()
        if self.pk:
            prev = (
                Package.objects.filter(pk=self.pk)
                .values("start_date", "booking_cutoff_datetime")
                .first()
            )
            if (
                prev
                and prev["start_date"] != self.start_date
                and prev["booking_cutoff_datetime"]
                == self.cutoff_default_for(prev["start_date"])
            ):
                return self.default_cutoff()
        return self.booking_cutoff_datetime

    def save(self, *args, **kwargs):
        if self.start_date:
            self.booking_cutoff_datetime = self._resolved_cutoff()
        super().save(*args, **kwargs)

    def is_bookable(self):
        """Single source of truth for whether new bookings are allowed.

        The public booking API (Phase 3) must check this; admins may still
        create bookings past the cutoff from the admin panel.
        """
        return (
            self.status == self.Status.OPEN
            and self.is_booking_open
            and self.booking_cutoff_datetime is not None
            and timezone.now() < self.booking_cutoff_datetime
            # Backstop for a mis-set cutoff: never sell a departed voyage,
            # whatever the cutoff says. Day-of sales (an admin extending the
            # cutoff to departure morning) stay possible.
            and self.start_date is not None
            and timezone.localdate() <= self.start_date
        )

    is_bookable.boolean = True


class RoomBlocked(ValidationError):
    """A room block/unblock was rejected — booked room, or the package is no
    longer live. Raised by PackageRoom.block()/unblock() so callers (staff API)
    can turn it into a clean 400 instead of a 500."""


class PackageRoom(models.Model):
    package = models.ForeignKey(
        Package, on_delete=models.CASCADE, related_name="package_rooms"
    )
    room = models.ForeignKey(
        Room, on_delete=models.PROTECT, related_name="package_rooms"
    )
    #: Whether this room is part of the package's sellable inventory at all.
    #: Set once when rooms are attached; unrelated to the admin hold below.
    is_available = models.BooleanField(default=True)
    #: Admin hold: staff can withhold a specific room from sale on a live
    #: sailing (a cabin kept for crew, a maintenance issue, a VIP hold) without
    #: removing it from inventory. Distinct from `is_available` (which is the
    #: room's baseline presence on the package) and from "booked" (a customer
    #: holds it) — a blocked room is surfaced to customers as "booked" (simply
    #: not on sale; the public serializer never distinguishes the two), so the
    #: internal reason never leaks, and can be released at any time while the
    #: package is live. Booked rooms cannot be blocked; see block().
    is_blocked = models.BooleanField(
        default=False,
        help_text="Admin hold — withhold this room from sale without deleting it.",
    )
    block_reason = models.CharField(
        max_length=200,
        blank=True,
        help_text="Internal note on why the room is held (never shown to customers).",
    )
    blocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    blocked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["package", "room"], name="uniq_room_per_package"
            ),
        ]

    def __str__(self):
        return f"{self.package} — Room {self.room.room_number}"

    def clean(self):
        if self.package_id and self.room_id and self.room.ship_id != self.package.ship_id:
            raise ValidationError(
                {"room": "Room belongs to a different ship than this package."}
            )

    def _is_actively_booked(self):
        """True if a live (non-cancelled) booking holds this room on this
        package — the partial unique constraint's is_active flag is the single
        truth for 'still held'."""
        # Imported here to avoid a circular import (bookings imports packages).
        from apps.bookings.models import BookingRoom

        return BookingRoom.objects.filter(
            package_id=self.package_id, room_id=self.room_id, is_active=True
        ).exists()

    @classmethod
    def lock_for_booking(cls, *, package_id, room_id):
        """Acquire the SELECT ... FOR UPDATE lock on the (package, room) row and
        return the locked PackageRoom, or None if the room is not attached to
        the package.

        This is the single logical resource that serialises the booking and the
        admin-block flows: both take this same row lock before deciding whether
        the room may change hands, so a booking and a block for the same room
        can no longer proceed at the same instant. Callers hold the lock (i.e.
        stay inside the surrounding transaction) while they read availability
        and write, so the state they validated cannot shift underneath them.
        """
        return (
            cls.objects.select_for_update()
            .select_related("package", "room__room_type")
            .filter(package_id=package_id, room_id=room_id)
            .first()
        )

    def assert_bookable(self):
        """Validate — while the caller holds this row's FOR UPDATE lock — that
        the room may still be sold on this package: it is in inventory, not on
        admin hold, and not already actively booked. Because the lock is held,
        no concurrent block or booking can change any of these between this
        check and the caller's write.

        The exception TYPE preserves the pre-existing API contract:
        - is_blocked (admin hold) → RoomBlocked, a ValidationError → HTTP 400.
          A held room reads to the customer as "not on sale", a client-input
          problem, not a race.
        - not in inventory / already actively booked → RoomUnavailable →
          HTTP 409 Conflict, the lost-a-race semantics.
        """
        from apps.bookings.exceptions import RoomUnavailable

        if self.is_blocked:
            raise RoomBlocked(
                "This room is not available in the selected package."
            )
        if not self.is_available:
            raise RoomUnavailable()
        if self._is_actively_booked():
            raise RoomUnavailable()

    def block(self, *, user=None, reason=""):
        """Withhold this room from sale (admin hold). Acquires the
        PackageRoom row lock (lock_for_booking's resource) and re-checks the
        booked/live state before flipping the flag.

        Booking creation now takes the SAME row lock as its first act inside its
        transaction (BookingCreateSerializer.create), so the two flows serialise
        on this one row: whichever grabs the lock first runs to commit while the
        other waits, and the loser then re-reads the just-committed state. A room
        can therefore no longer end up flagged is_blocked while a live booking
        also holds it — the block sees the booking (and rejects) or the booking
        sees the block (and 409s).

        Rejects (RoomBlocked) when the package is not live (cancelled/completed)
        or the room is already actively booked."""
        with transaction.atomic():
            pr = PackageRoom.objects.select_for_update().select_related(
                "package"
            ).get(pk=self.pk)
            if pr.package.status in (
                Package.Status.CANCELLED,
                Package.Status.COMPLETED,
            ):
                raise RoomBlocked(
                    "This package is "
                    f"{pr.package.get_status_display().lower()} — rooms can no "
                    "longer be blocked or released."
                )
            if pr._is_actively_booked():
                raise RoomBlocked(
                    f"Room {pr.room.room_number} is booked — cancel the booking "
                    "before blocking it."
                )
            pr.is_blocked = True
            pr.block_reason = (reason or "").strip()[:200]
            pr.blocked_by = user
            pr.blocked_at = timezone.now()
            pr.save(
                update_fields=[
                    "is_blocked",
                    "block_reason",
                    "blocked_by",
                    "blocked_at",
                ]
            )
        # Keep the caller's instance in sync with what was persisted.
        self.is_blocked = pr.is_blocked
        self.block_reason = pr.block_reason
        self.blocked_by = pr.blocked_by
        self.blocked_at = pr.blocked_at
        return self

    def unblock(self):
        """Release an admin hold. Allowed on any live package (a cancelled or
        completed package can't be edited, but an already-blocked room there is
        harmless — the package is over — so unblock is simply a no-op-safe
        clear here rather than a hard reject)."""
        with transaction.atomic():
            pr = PackageRoom.objects.select_for_update().get(pk=self.pk)
            pr.is_blocked = False
            pr.block_reason = ""
            pr.blocked_by = None
            pr.blocked_at = None
            pr.save(
                update_fields=[
                    "is_blocked",
                    "block_reason",
                    "blocked_by",
                    "blocked_at",
                ]
            )
        self.is_blocked = False
        self.block_reason = ""
        self.blocked_by = None
        self.blocked_at = None
        return self


class ForeignerSurcharge(models.Model):
    """Global foreign-national surcharge — a fixed amount per foreign guest.

    A sibling of KidPricingRule, and global for the same reason: this is a
    pricing POLICY, not the price of a particular sailing. It first lived on
    Package, which meant staff had to re-enter it for every voyage and — worse
    — could not change it at all once a voyage had bookings, because a
    per-package price is part of what those customers were quoted. A policy
    that applies everywhere and changes rarely belongs in one place.

    Changing it re-prices only FUTURE bookings: every booking freezes the rate
    it was charged into its own price_snapshot when it is created (see
    apps.bookings.pricing), so an issued invoice can never be rewritten by an
    edit here.

    Single row, always pk=1 — enforced in save() so no code path can create a
    second one and leave "the" surcharge ambiguous.
    """

    SINGLETON_PK = 1

    adult_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Extra charge per foreign adult, on top of the adult fare.",
    )
    kid_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text=(
            "Extra charge per foreign child, on top of that child's age-tier "
            "fare. A child on a FREE age tier is still surcharged if set."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Foreigner surcharge"
        verbose_name_plural = "Foreigner surcharge"

    def __str__(self):
        return f"Foreign adult {self.adult_amount} / child {self.kid_amount}"

    def clean(self):
        # Surcharges are additive money — a negative one would DISCOUNT a
        # foreign guest below the local fare, which is never the intent and is
        # invisible on the invoice (it prints as a charge line). Checked here
        # as well as by the field validators because clean() is the gate every
        # non-form path (staff API, admin, shell) goes through.
        for field in ("adult_amount", "kid_amount"):
            value = getattr(self, field)
            if value is not None and value < 0:
                raise ValidationError({field: "Surcharge cannot be negative."})

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Deleting the policy row would make pricing ambiguous rather than
        free — there is nothing to fall back to. Blocked; set both amounts to
        0.00 to charge foreign nationals the same as local guests."""
        raise ValidationError(
            "The foreigner surcharge cannot be deleted. Set both amounts to "
            "0.00 to stop charging it."
        )

    @classmethod
    def get_solo(cls):
        """The one row, created with zero amounts on first use.

        Never raises: pricing must work on a fresh database (and in tests) with
        no admin having visited the settings page, and 0.00 is the correct
        default — it prices exactly as the system did before the surcharge
        existed."""
        return cls.objects.get_or_create(pk=cls.SINGLETON_PK)[0]


class KidPricingRule(models.Model):
    """Admin-configurable kid pricing tiers (PRD §5.2).

    Age ranges are min-inclusive, max-exclusive: [0, 3) free, [3, 8) fixed,
    [8, 99) full adult. The 8-vs-9 boundary is data, never code.
    """

    class ChargeType(models.TextChoices):
        FREE = "free", "Free"
        FIXED = "fixed", "Fixed amount"
        FULL_ADULT = "full_adult", "Full adult charge"

    min_age = models.PositiveSmallIntegerField(help_text="Inclusive lower bound.")
    max_age = models.PositiveSmallIntegerField(help_text="Exclusive upper bound.")
    charge_type = models.CharField(max_length=10, choices=ChargeType.choices)
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Required only for fixed-amount rules.",
    )

    class Meta:
        ordering = ["min_age"]

    def __str__(self):
        return f"Age {self.min_age}–{self.max_age}: {self.get_charge_type_display()}"

    def clean(self):
        errors = {}
        if self.min_age is not None and self.max_age is not None:
            if self.min_age >= self.max_age:
                errors["max_age"] = "Max age must be greater than min age."
            else:
                overlap = (
                    KidPricingRule.objects.exclude(pk=self.pk)
                    .filter(min_age__lt=self.max_age, max_age__gt=self.min_age)
                    .first()
                )
                if overlap:
                    errors["min_age"] = f"Age range overlaps with: {overlap}"
        if self.charge_type == self.ChargeType.FIXED and self.amount is None:
            errors["amount"] = "Amount is required for fixed-amount rules."
        if errors:
            raise ValidationError(errors)

    @classmethod
    def rule_for_age(cls, age):
        return cls.objects.filter(min_age__lte=age, max_age__gt=age).first()
