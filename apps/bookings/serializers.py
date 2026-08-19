from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import serializers

from apps.packages.models import Package, PackageRoom
from apps.ships.models import Room

from .exceptions import RoomUnavailable
from .guests import clean_foreign_guests, guest_counts, mask_passport
from .identity import normalize_booking_code, phone_digits
from . import invoice_access
from .models import Booking, BookingRoom, Invoice, Payment
from .pricing import booking_price_breakdown, snapshot_booking_breakdown

#: Decimal → string so DRF's encoder never floats money. The API `price_breakdown`
#: field carries the whole booking's per-room breakdown plus its grand total.
breakdown_as_json = snapshot_booking_breakdown


class KidSerializer(serializers.Serializer):
    age = serializers.IntegerField(min_value=0, max_value=17)


class BookingRoomInputSerializer(serializers.Serializer):
    """One room within a booking: which cabin, and that cabin's own party."""

    room_id = serializers.PrimaryKeyRelatedField(
        queryset=Room.objects.all(), source="room"
    )
    adult_count = serializers.IntegerField(min_value=1)
    kid_details = KidSerializer(many=True, required=False)
    # Foreign nationals among this cabin's pax (a subset of adult_count /
    # kid_details), each with a passport. Left as a loose list here on purpose:
    # apps.bookings.guests.clean_foreign_guests owns the whole shape, and the
    # model's clean() calls the SAME function, so the quote endpoint and the
    # create path can never drift on what a valid guest list is.
    foreign_guests = serializers.ListField(
        child=serializers.DictField(), required=False, default=list
    )


class BookingQuoteSerializer(serializers.Serializer):
    """Validates a prospective (multi-room) booking and prices it — no DB writes.

    Also the base of BookingCreateSerializer, so quote and create can never
    disagree on validation or price.
    """

    # Staff subclasses flip this off: admins may book past the cutoff
    # (PRD §5.5 manual override); the public API always enforces it.
    enforce_cutoff = True
    # A quote is a price preview and prices depend only on how MANY foreign
    # guests there are, so it accepts the counts without their passports.
    # BookingCreateSerializer flips this on — a booking must identify them.
    require_passport = False

    package_id = serializers.PrimaryKeyRelatedField(
        queryset=Package.objects.public(), source="package"
    )
    rooms = BookingRoomInputSerializer(many=True)

    def validate_rooms(self, rooms):
        if not rooms:
            raise serializers.ValidationError("At least one room is required.")
        # The same physical cabin cannot be listed twice in one booking — it
        # would double-count pricing and then fail the (package, room) unique
        # constraint at save with a confusing "unavailable" instead.
        room_ids = [entry["room"].pk for entry in rooms]
        if len(room_ids) != len(set(room_ids)):
            raise serializers.ValidationError(
                "A room may only be selected once per booking."
            )
        return rooms

    def validate(self, attrs):
        package = attrs["package"]
        rooms = attrs["rooms"]

        if self.enforce_cutoff and not package.is_bookable():
            raise serializers.ValidationError(
                {"package_id": "Booking is closed for this package."}
            )

        for index, entry in enumerate(rooms):
            self._validate_room(package, index, entry)

        # One passport cannot appear in two cabins of the same booking: that is
        # one person billed two foreigner surcharges and printed twice on the
        # immigration manifest. Per-cabin duplicates are caught inside
        # clean_foreign_guests; only the cross-cabin case is left, and it needs
        # the whole (now normalised) booking in view.
        seen = {}
        for index, entry in enumerate(rooms):
            for guest in entry.get("foreign_guests") or []:
                passport = guest["passport_number"]
                # Blank only on the quote path (require_passport=False), where
                # several unidentified guests are the normal state — they are
                # not duplicates of each other.
                if not passport:
                    continue
                if passport in seen:
                    raise serializers.ValidationError(
                        {
                            "rooms": {
                                index: {
                                    "foreign_guests": (
                                        f"Passport {passport} is already listed "
                                        f"on room {seen[passport]} of this "
                                        "booking."
                                    )
                                }
                            }
                        }
                    )
                seen[passport] = entry["room"].room_number

        return attrs

    def _validate_room(self, package, index, entry):
        """Per-room membership + pax limits. Errors are keyed by room index so
        the frontend can point at the offending cabin.

        Availability (is_available, the admin is_blocked hold, and "already
        booked") is deliberately NOT checked here: this runs before any
        transaction, so a plain unlocked read would be the very check-then-act
        race this refactor removes. The authoritative availability gate is the
        SELECT ... FOR UPDATE check in BookingCreateSerializer.create
        (PackageRoom.assert_bookable). This method still runs for the quote
        endpoint — where there is nothing to lock — so keeping only membership
        and pax here means a quote never asserts an availability it cannot
        hold."""
        room = entry["room"]
        kid_details = entry.get("kid_details") or []

        package_room = PackageRoom.objects.filter(package=package, room=room).first()
        if package_room is None:
            raise serializers.ValidationError(
                {"rooms": {index: {"room_id": "This room is not part of the "
                                              "selected package."}}}
            )

        room_type = room.room_type
        errors = {}
        if entry["adult_count"] > room_type.max_adults:
            errors["adult_count"] = (
                f"{room_type.name} allows at most {room_type.max_adults} adults."
            )
        if len(kid_details) > room_type.max_kids:
            errors["kid_details"] = (
                f"{room_type.name} allows at most {room_type.max_kids} kids."
            )
        if errors:
            raise serializers.ValidationError({"rooms": {index: errors}})

        # Foreign guests, validated by the same function the model's clean()
        # uses. The cleaned (normalised, upper-cased) list is written BACK onto
        # the entry so both the quote's pricing and the created BookingRoom see
        # canonical passports — the cross-cabin duplicate check downstream
        # depends on that normalisation.
        try:
            entry["foreign_guests"] = clean_foreign_guests(
                entry.get("foreign_guests"),
                adult_count=entry["adult_count"],
                kid_count=len(kid_details),
                require_passport=self.require_passport,
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                {"rooms": {index: {"foreign_guests": exc.messages}}}
            ) from None

    def get_breakdown(self):
        """Priced breakdown for the whole booking (all Decimal), grand total
        included."""
        attrs = self.validated_data
        rooms = []
        for entry in attrs["rooms"]:
            foreign_adults, foreign_kids = guest_counts(entry.get("foreign_guests"))
            rooms.append(
                {
                    "room": entry["room"],
                    "adult_count": entry["adult_count"],
                    "kid_ages": [kid["age"] for kid in entry.get("kid_details") or []],
                    "foreign_adults": foreign_adults,
                    "foreign_kids": foreign_kids,
                }
            )
        return booking_price_breakdown(attrs["package"], rooms)


class BookingCreateSerializer(BookingQuoteSerializer):
    # Unlike a quote, a booking must identify every foreign guest — the
    # passport is what goes on the boarding manifest.
    require_passport = True

    customer_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=20)
    email = serializers.EmailField()
    # Optional free-text note. Bounded here (not just on the model's TextField)
    # because this endpoint is anonymous — an uncapped field would let anyone
    # push arbitrarily large rows into the DB. allow_blank so an empty box is
    # simply "no request", not a validation error.
    special_requests = serializers.CharField(
        max_length=1000, required=False, allow_blank=True, default=""
    )

    def create(self, validated_data):
        rooms_data = validated_data.pop("rooms")
        package = validated_data["package"]
        booking = Booking(**validated_data)
        # Lock every PackageRoom this booking touches, ordered by room id so two
        # multi-room bookings that overlap can never deadlock (both grab the
        # rows in the same order). Computed up front so the exact same set of
        # ids is locked, in order, on both the first attempt and any retry.
        room_ids = sorted(entry["room"].pk for entry in rooms_data)
        # Two insert attempts: a booking_code collision (two concurrent
        # requests drawing the same random code, ~2^-32) must be retried
        # with a fresh code — not misreported as a lost room race.
        for retry_left in (True, False):
            try:
                with transaction.atomic():
                    # FIRST thing inside the transaction: take the FOR UPDATE
                    # lock on each PackageRoom row and validate availability
                    # while holding it. This is the single logical resource the
                    # admin-block flow also locks (PackageRoom.block), so a block
                    # and this booking now serialise on the same row instead of
                    # racing on two different guards. The plain .exists()
                    # availability read that used to live in BookingRoom.clean()
                    # is replaced by this locked check — nothing can flip
                    # is_blocked / is_active between the check and the write.
                    for room_id in room_ids:
                        package_room = PackageRoom.lock_for_booking(
                            package_id=package.pk, room_id=room_id
                        )
                        if package_room is None:
                            # Membership was validated in the serializer; a miss
                            # here means the row vanished concurrently — treat it
                            # as unavailable rather than a 500.
                            raise RoomUnavailable()
                        package_room.assert_bookable()

                    booking.full_clean()
                    booking.save()
                    # Each room prices itself in clean(); the partial unique
                    # constraint on BookingRoom remains the final double-booking
                    # guard for any true race that still slips through. The whole
                    # set is created inside one transaction, so if any room is
                    # lost the entire booking rolls back — a family never ends up
                    # half-booked.
                    for entry in rooms_data:
                        booking_room = BookingRoom(
                            booking=booking,
                            package=package,
                            room=entry["room"],
                            adult_count=entry["adult_count"],
                            kid_details=[dict(k) for k in entry.get("kid_details", [])],
                            # Already normalised by _validate_room; clean()
                            # re-runs the same validation as the un-bypassable
                            # guard for non-serializer paths (admin, shell).
                            foreign_guests=[
                                dict(g) for g in entry.get("foreign_guests") or []
                            ],
                        )
                        booking_room.full_clean()
                        booking_room.save()
                    # Now that every room is priced, sum them onto the booking.
                    booking.reprice()
                    booking.save(update_fields=[
                        "total_amount", "price_snapshot", "due_amount", "updated_at"
                    ])
                return booking
            except IntegrityError as exc:
                if "booking_code" in str(exc) and retry_left:
                    booking.booking_code = ""  # regenerated on the next save
                    continue
                raise RoomUnavailable()
            except DjangoValidationError as exc:
                if "uniq_active_bookingroom_per_package_room" in str(exc):
                    raise RoomUnavailable()
                raise serializers.ValidationError(
                    getattr(exc, "message_dict", None) or exc.messages
                )


class PaymentInitiateSerializer(serializers.Serializer):
    """Validates a pay request against the booking's server-side due amount.

    full → amount is taken from booking.due_amount, any client amount ignored.
    partial → client amount required, min_first_payment <= amount <= due
    (the floor comes from Package.min_deposit_percent and applies only to the
    booking's FIRST payment; top-ups have no floor).

    These checks are check-then-act UX; initiate_payment() re-verifies all of
    them under a row lock on the booking.
    """

    payment_type = serializers.ChoiceField(choices=Payment.PaymentType.choices)
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.01")
    )

    def validate(self, attrs):
        from django.utils import timezone

        from .payment_service import minimum_first_payment

        booking = self.context["booking"]

        if booking.status in (Booking.Status.CANCELLED, Booking.Status.COMPLETED):
            raise serializers.ValidationError(
                {"payment_type": "This booking can no longer be paid."}
            )
        if booking.due_amount <= 0:
            raise serializers.ValidationError(
                {"payment_type": "Nothing is due on this booking."}
            )
        # Balance may be paid any time before departure (client policy, QA H6);
        # online payment only stops once the ship has sailed, after which the
        # guide collects any balance on board.
        if timezone.localdate() > booking.package.start_date:
            raise serializers.ValidationError(
                {
                    "payment_type": (
                        "This package has already departed — please settle any "
                        "balance with the guide on board."
                    )
                }
            )

        if attrs["payment_type"] == Payment.PaymentType.PARTIAL:
            amount = attrs.get("amount")
            if amount is None:
                raise serializers.ValidationError(
                    {"amount": "Amount is required for a partial payment."}
                )
            if amount > booking.due_amount:
                raise serializers.ValidationError(
                    {"amount": f"Amount exceeds the due amount ({booking.due_amount})."}
                )
            floor = minimum_first_payment(booking)
            if amount < floor:
                raise serializers.ValidationError(
                    {
                        "amount": (
                            f"Minimum first payment is {floor} BDT "
                            f"({booking.package.min_deposit_percent}% of the total)."
                        )
                    }
                )
        else:
            attrs.pop("amount", None)  # full payment: server decides the amount
        return attrs


class BookingInvoiceSerializer(serializers.ModelSerializer):
    """A customer-facing invoice listing: a download link and the figures the
    invoice states — never another booking's data.

    The link is minted fresh on every read and expires in 30 minutes. The
    invoice's permanent access_token is NOT what travels in it (see
    apps.bookings.invoice_access): this endpoint is already authorised by the
    booking code, so there is nothing to gain from also handing out a
    credential that would still work next year.
    """

    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "number", "total_amount", "paid_amount", "due_amount",
            "sent_at", "created_at", "download_url",
        ]

    def get_download_url(self, invoice):
        url = reverse(
            "invoice-download", kwargs={"token": invoice_access.issue(invoice)}
        )
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url


class BookingPackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = ["id", "start_date", "end_date"]


class BookingRoomPublicSerializer(serializers.ModelSerializer):
    """One room of a booking in the confirmation/status view."""

    room_number = serializers.CharField(source="room.room_number", read_only=True)
    room_type = serializers.CharField(source="room.room_type.name", read_only=True)
    foreign_guests = serializers.SerializerMethodField()

    class Meta:
        model = BookingRoom
        fields = [
            "room_number", "room_type", "adult_count", "kid_details",
            "foreign_guests", "room_subtotal",
        ]

    def get_foreign_guests(self, booking_room):
        """Foreign guests with their passports MASKED.

        This endpoint is reached with a booking code alone — no login — so it
        must never render a full passport number. The customer sees enough to
        recognise which guest a row is (and to spot a typo in the last digits);
        staff APIs and the manifest PDF carry the full number."""
        return [
            {
                **{k: v for k, v in guest.items() if k != "passport_number"},
                "passport_number": mask_passport(guest.get("passport_number")),
            }
            for guest in booking_room.foreign_guests or []
        ]


class BookingLookupSerializer(serializers.Serializer):
    """Input for the public "find my booking" form.

    Normalises the code the way a human types it (case, spaces, a missing
    "BK-") and requires the last four digits of the booking's phone as a second
    factor — see apps.bookings.identity for why a bearer code alone is not
    enough on a form anyone can reach.
    """

    booking_code = serializers.CharField(max_length=40)
    phone_last4 = serializers.CharField(max_length=20)

    def validate_booking_code(self, value):
        code = normalize_booking_code(value)
        if not code:
            raise serializers.ValidationError("Enter your booking code.")
        return code

    def validate_phone_last4(self, value):
        if len(phone_digits(value)) < 4:
            raise serializers.ValidationError(
                "Enter the last 4 digits of the phone number on the booking."
            )
        return value


class BookingPublicSerializer(serializers.ModelSerializer):
    """Confirmation/status representation. Looked up by unguessable
    booking_code only — never exposes other customers' data."""

    package = BookingPackageSerializer(read_only=True)
    rooms = BookingRoomPublicSerializer(many=True, read_only=True)
    total_pax = serializers.IntegerField(read_only=True)
    # The frontend renders the deposit floor from this — it never computes
    # money client-side.
    min_first_payment = serializers.SerializerMethodField()
    # The balance deadline, as a DATE the customer can see — not a policy
    # phrase. It is enforced server-side at payment time (QA H8); showing it
    # is what stops them discovering it by being refused.
    balance_due_at = serializers.SerializerMethodField()
    balance_deadline_passed = serializers.SerializerMethodField()
    # A cancellation awaiting a staff decision is deliberately NOT a booking
    # status (see Booking.has_pending_cancellation), so without this the page
    # has no way to show that the customer already asked. They would see an
    # unchanged booking with a live "Cancel" button, conclude the request never
    # went through, and either send it again or phone in.
    pending_cancellation = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "booking_code",
            "status",
            "package",
            "rooms",
            "total_pax",
            "customer_name",
            "phone",
            "email",
            "special_requests",
            "total_amount",
            "paid_amount",
            "due_amount",
            "min_first_payment",
            "balance_due_at",
            "balance_deadline_passed",
            "pending_cancellation",
        ]

    def get_pending_cancellation(self, booking):
        """The open cancellation request, or null.

        Local import: apps.refunds imports this module, so a module-level one
        would be circular.
        """
        from apps.refunds.policy import pending_request_for
        from apps.refunds.serializers import CancellationRequestPublicSerializer

        pending = pending_request_for(booking)
        return CancellationRequestPublicSerializer(pending).data if pending else None

    def get_min_first_payment(self, booking):
        from .payment_service import minimum_first_payment

        return str(minimum_first_payment(booking))

    def get_balance_due_at(self, booking):
        return booking.package.balance_due_at()

    def get_balance_deadline_passed(self, booking):
        from .payment_service import balance_deadline_passed

        return balance_deadline_passed(booking)
