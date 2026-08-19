"""Staff dashboard serializers — full-field, staff-only views of every model.

Unlike the public serializers these expose internal state (raw status,
is_booking_open, customer data across bookings) because every staff endpoint
sits behind IsAdminUser.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.bookings.exceptions import RoomUnavailable
from apps.bookings.models import (
    Booking,
    BookingRoom,
    BookingStatusLog,
    Invoice,
    Payment,
)
from apps.bookings.serializers import BookingCreateSerializer
from apps.packages.models import (
    ForeignerSurcharge,
    KidPricingRule,
    Package,
    PackageRoom,
)
from apps.ships.models import (
    Cabin,
    CabinImage,
    FoodMenuItem,
    GalleryImage,
    Room,
    RoomImage,
    RoomType,
    Ship,
)


class StaffTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login is for dashboard staff only — valid credentials without
    is_staff still get rejected."""

    def validate(self, attrs):
        data = super().validate(attrs)
        if not self.user.is_staff:
            raise serializers.ValidationError(
                {"detail": "This account does not have staff access."}
            )
        data["user"] = {
            "username": self.user.username,
            "first_name": self.user.first_name,
            "is_staff": self.user.is_staff,
        }
        return data


class StaffShipSerializer(serializers.ModelSerializer):
    """Ship settings the dashboard can edit — currently the helpline numbers
    printed on the guide report & customer invoices. `authority_phone_list` is
    the resolved list actually used by the PDFs (ship numbers, or the system
    default when the ship's field is blank)."""

    authority_phone_list = serializers.ListField(
        child=serializers.CharField(), read_only=True
    )

    class Meta:
        model = Ship
        fields = [
            "id",
            "name",
            "status",
            "authority_phones",
            "authority_phone_list",
            "contact_notify_email",
            "guide_report_density",
        ]
        read_only_fields = ["name", "status"]

    def validate_authority_phones(self, value):
        # Stored comma-separated. Normalise spacing so the PDF renders cleanly
        # and two lists that differ only in whitespace compare equal.
        numbers = [n.strip() for n in value.split(",") if n.strip()]
        for n in numbers:
            # Keep it permissive (formats vary: 01712-823482, +8801…), but
            # reject anything with no digit at all — almost always a typo.
            if not any(ch.isdigit() for ch in n):
                raise serializers.ValidationError(
                    f"'{n}' does not look like a phone number."
                )
        return ", ".join(numbers)


class StaffRoomTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoomType
        fields = ["id", "name", "max_adults", "max_kids", "base_price"]


class StaffRoomSerializer(serializers.ModelSerializer):
    room_type_name = serializers.CharField(source="room_type.name", read_only=True)

    class Meta:
        model = Room
        fields = ["id", "ship", "room_type", "room_type_name", "room_number", "floor_number"]


class StaffRoomImageSerializer(serializers.ModelSerializer):
    """Room gallery photo, managed from the dashboard's Room Photos tab.

    `image` is upload-only (multipart POST); reads carry `image_url` instead —
    the storage URL (Cloudinary CDN in production), never the raw file path.
    `room` is immutable after upload: moving a photo between rooms would
    silently rewrite history for both rooms' galleries; re-upload instead.
    """

    image = serializers.ImageField(write_only=True)
    image_url = serializers.ImageField(source="image", read_only=True, use_url=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True)

    class Meta:
        model = RoomImage
        fields = ["id", "room", "room_number", "image", "image_url", "caption", "sort_order"]

    def update(self, instance, validated_data):
        validated_data.pop("room", None)  # immutable — see docstring
        return super().update(instance, validated_data)

    def validate_image(self, image):
        # Uploads go straight to the CDN and out to customers' browsers — keep
        # a sane ceiling so a 40 MB camera original can't be published as-is.
        max_mb = 10
        if image.size > max_mb * 1024 * 1024:
            raise serializers.ValidationError(
                f"Image is {image.size / (1024 * 1024):.1f} MB — please compress "
                f"it below {max_mb} MB before uploading."
            )
        return image


class StaffCabinImageSerializer(serializers.ModelSerializer):
    """Cabin gallery photo, managed from the dashboard's Cabins page.

    Same contract as StaffRoomImageSerializer: `image` is upload-only,
    reads carry `image_url` (CDN URL in production), and `cabin` is immutable
    after upload. Setting `is_main=true` atomically clears the previous main
    (enforced in CabinImage.save) — the main image is what the public cabin
    card shows.
    """

    image = serializers.ImageField(write_only=True)
    image_url = serializers.ImageField(source="image", read_only=True, use_url=True)

    class Meta:
        model = CabinImage
        fields = ["id", "cabin", "image", "image_url", "caption", "is_main", "sort_order"]

    def update(self, instance, validated_data):
        validated_data.pop("cabin", None)  # immutable — see docstring
        return super().update(instance, validated_data)

    def validate_image(self, image):
        max_mb = 10
        if image.size > max_mb * 1024 * 1024:
            raise serializers.ValidationError(
                f"Image is {image.size / (1024 * 1024):.1f} MB — please compress "
                f"it below {max_mb} MB before uploading."
            )
        return image


class StaffGalleryImageSerializer(serializers.ModelSerializer):
    """Public-gallery photo, managed from the dashboard's Gallery page.

    Same contract as StaffRoomImageSerializer: `image` is upload-only
    (multipart POST), reads carry `image_url` (CDN URL in production).
    `caption` is the text staff write on each photo; `is_active` hides a
    photo from the website without deleting it.
    """

    image = serializers.ImageField(write_only=True)
    image_url = serializers.ImageField(source="image", read_only=True, use_url=True)
    ship_name = serializers.CharField(source="ship.name", read_only=True)
    # Explicit default: multipart uploads omit the field, and DRF reads a
    # missing boolean in form data as False — without this, every freshly
    # uploaded photo would land hidden.
    is_active = serializers.BooleanField(default=True)

    class Meta:
        model = GalleryImage
        fields = [
            "id",
            "ship",
            "ship_name",
            "image",
            "image_url",
            "caption",
            "is_active",
            "sort_order",
        ]

    def validate_image(self, image):
        max_mb = 10
        if image.size > max_mb * 1024 * 1024:
            raise serializers.ValidationError(
                f"Image is {image.size / (1024 * 1024):.1f} MB — please compress "
                f"it below {max_mb} MB before uploading."
            )
        return image


class StaffCabinSerializer(serializers.ModelSerializer):
    """Showcase cabin content (public /cabins pages), fully staff-editable.

    `features` is a list of strings; `amenities` a list of {label, value};
    `highlights` a list of {title, desc}. Shapes are validated here so a
    malformed payload can never break the public pages.
    """

    ship_name = serializers.CharField(source="ship.name", read_only=True)
    room_type_name = serializers.CharField(source="room_type.name", read_only=True)
    occupancy = serializers.CharField(source="occupancy_label", read_only=True)
    images = StaffCabinImageSerializer(many=True, read_only=True)

    class Meta:
        model = Cabin
        fields = [
            "id",
            "ship",
            "ship_name",
            "room_type",
            "room_type_name",
            "occupancy",
            "slug",
            "name",
            "tagline",
            "description",
            "size_label",
            "features",
            "amenities",
            "highlights",
            "is_active",
            "sort_order",
            "images",
        ]
        extra_kwargs = {"slug": {"required": False, "allow_blank": True}}

    def validate_features(self, value):
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise serializers.ValidationError("Features must be a list of strings.")
        return [item.strip() for item in value if item.strip()]

    def _validate_dict_list(self, value, keys, label):
        if not isinstance(value, list):
            raise serializers.ValidationError(f"{label} must be a list.")
        cleaned = []
        for item in value:
            if not isinstance(item, dict) or set(item.keys()) != set(keys) or not all(
                isinstance(item[key], str) for key in keys
            ):
                raise serializers.ValidationError(
                    f"Each {label.lower()} entry must be an object with "
                    f"{' and '.join(keys)} strings."
                )
            if any(item[key].strip() for key in keys):
                cleaned.append({key: item[key].strip() for key in keys})
        return cleaned

    def validate_amenities(self, value):
        return self._validate_dict_list(value, ["label", "value"], "Amenities")

    def validate_highlights(self, value):
        return self._validate_dict_list(value, ["title", "desc"], "Highlights")


class StaffFoodMenuItemSerializer(serializers.ModelSerializer):
    ship_name = serializers.CharField(source="ship.name", read_only=True)

    class Meta:
        model = FoodMenuItem
        fields = [
            "id", "ship", "ship_name", "day", "meal_type", "name", "order", "is_active",
        ]


class StaffPackageRoomBookingSerializer(serializers.ModelSerializer):
    """Booking summary embedded in the per-package room map — enough for the
    dashboard to show who holds THIS room and what the booking owes.

    Serialised from a BookingRoom: adult_count/kid_details are this cabin's own
    party, while money/customer/status come from its parent booking (the family
    pays once for the whole booking, whatever rooms it holds)."""

    id = serializers.IntegerField(source="booking.id", read_only=True)
    booking_code = serializers.CharField(
        source="booking.booking_code", read_only=True
    )
    customer_name = serializers.CharField(
        source="booking.customer_name", read_only=True
    )
    phone = serializers.CharField(source="booking.phone", read_only=True)
    # This room's pax (BookingRoom fields).
    room_pax = serializers.IntegerField(source="total_pax", read_only=True)
    # The whole booking's pax across all its rooms.
    total_pax = serializers.SerializerMethodField()
    total_amount = serializers.DecimalField(
        source="booking.total_amount", max_digits=12, decimal_places=2,
        read_only=True,
    )
    paid_amount = serializers.DecimalField(
        source="booking.paid_amount", max_digits=12, decimal_places=2,
        read_only=True,
    )
    due_amount = serializers.DecimalField(
        source="booking.due_amount", max_digits=12, decimal_places=2,
        read_only=True,
    )
    status = serializers.CharField(source="booking.status", read_only=True)

    class Meta:
        model = BookingRoom
        fields = [
            "id", "booking_code", "customer_name", "phone",
            "adult_count", "kid_details", "room_pax", "total_pax",
            "total_amount", "paid_amount", "due_amount", "status",
        ]

    def get_total_pax(self, booking_room):
        return booking_room.booking.total_pax


class StaffPackageRoomSerializer(serializers.ModelSerializer):
    """One room within a package with its live status and (if booked) the
    active booking. Expects `bookings_by_room` {room_id: BookingRoom} in
    context.

    availability precedence: booked > blocked > unavailable > available.
    'booked' wins over 'blocked' so a cabin that is both flagged blocked and
    somehow still holds a live booking never hides the customer from staff."""

    room_id = serializers.IntegerField(source="room.id", read_only=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True)
    floor_number = serializers.IntegerField(source="room.floor_number", read_only=True)
    room_type = StaffRoomTypeSerializer(source="room.room_type", read_only=True)
    availability = serializers.SerializerMethodField()
    booking = serializers.SerializerMethodField()
    blocked_by_username = serializers.CharField(
        source="blocked_by.username", read_only=True, default=None
    )

    class Meta:
        model = PackageRoom
        fields = [
            "id", "room_id", "room_number", "floor_number", "room_type",
            "is_available", "availability", "booking",
            "is_blocked", "block_reason", "blocked_by_username", "blocked_at",
        ]

    def _active_booking(self, package_room):
        return self.context.get("bookings_by_room", {}).get(package_room.room_id)

    def get_availability(self, package_room):
        if self._active_booking(package_room):
            return "booked"
        if package_room.is_blocked:
            return "blocked"
        if not package_room.is_available:
            return "unavailable"
        return "available"

    def get_booking(self, package_room):
        booking = self._active_booking(package_room)
        if booking is None:
            return None
        return StaffPackageRoomBookingSerializer(booking).data


class StaffRoomBlockSerializer(serializers.Serializer):
    """Input for the per-package block-room action: which room, and an optional
    internal reason. room_id must be a room attached to the package (validated
    in the view against the PackageRoom set)."""

    room_id = serializers.IntegerField()
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
        help_text="Internal note (never shown to customers).",
    )


class StaffForeignerSurchargeSerializer(serializers.ModelSerializer):
    """The single global foreigner-surcharge row.

    Sits beside the kid-pricing rules in Room Settings because it is the same
    kind of thing: a pricing policy that applies to every sailing. Editing it
    re-prices only future bookings — issued ones carry their own frozen rate.
    """

    class Meta:
        model = ForeignerSurcharge
        fields = ["adult_amount", "kid_amount", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate(self, attrs):
        # DRF never calls model clean(), and the field validators alone would
        # let a raw negative through on a partial update.
        instance = ForeignerSurcharge(
            adult_amount=attrs.get(
                "adult_amount", getattr(self.instance, "adult_amount", Decimal("0.00"))
            ),
            kid_amount=attrs.get(
                "kid_amount", getattr(self.instance, "kid_amount", Decimal("0.00"))
            ),
        )
        instance.clean()
        return attrs


class StaffKidPricingRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = KidPricingRule
        fields = ["id", "min_age", "max_age", "charge_type", "amount"]

    def validate(self, attrs):
        # Surface the model's overlap/amount validation as field errors,
        # merging with the existing instance so partial updates validate too.
        rule = KidPricingRule(
            min_age=attrs.get("min_age", getattr(self.instance, "min_age", None)),
            max_age=attrs.get("max_age", getattr(self.instance, "max_age", None)),
            charge_type=attrs.get("charge_type", getattr(self.instance, "charge_type", None)),
            amount=attrs.get("amount", getattr(self.instance, "amount", None)),
        )
        if self.instance:
            rule.pk = self.instance.pk
        rule.clean()
        return attrs


class StaffPackageSerializer(serializers.ModelSerializer):
    ship_name = serializers.CharField(source="ship.name", read_only=True)
    # Annotated by the viewset queryset:
    bookings_count = serializers.IntegerField(read_only=True)
    paid_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    due_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    rooms_total = serializers.IntegerField(read_only=True)
    is_bookable = serializers.SerializerMethodField()
    # Resolved duration for the dashboard to display — reflects the override if
    # set, otherwise the auto-calculated value. The writable duration_days /
    # duration_nights below are the override knobs (blank = auto).
    effective_days = serializers.SerializerMethodField()
    effective_nights = serializers.SerializerMethodField()

    class Meta:
        model = Package
        fields = [
            "id", "ship", "ship_name", "start_date", "end_date",
            "booking_cutoff_datetime", "adult_price", "status", "is_booking_open",
            "min_deposit_percent", "balance_due_days_before_start",
            "duration_days", "duration_nights", "effective_days", "effective_nights",
            "marketing_title", "marketing_description", "hero_image", "highlights",
            "bookings_count", "paid_total", "due_total", "rooms_total", "is_bookable",
        ]

    def get_is_bookable(self, package):
        return package.is_bookable()

    def get_effective_days(self, package):
        return package.effective_days()

    def get_effective_nights(self, package):
        return package.effective_nights()

    def validate(self, attrs):
        # DRF never calls model clean(), so without this an inverted date
        # range or a ship-date overlap would hit the DB constraints and 500.
        # Merge with the existing instance so partial updates validate too
        # (same pattern as StaffKidPricingRuleSerializer).
        def value(field):
            return attrs.get(field, getattr(self.instance, field, None))

        ship = value("ship")
        package = Package(
            ship=ship,
            start_date=value("start_date"),
            end_date=value("end_date"),
            status=value("status") or Package.Status.DRAFT,
            booking_cutoff_datetime=value("booking_cutoff_datetime"),
        )
        if self.instance:
            package.pk = self.instance.pk
        package.min_deposit_percent = value("min_deposit_percent")
        package.duration_days = value("duration_days")
        package.duration_nights = value("duration_nights")
        package.clean()

        # Changing the price of a sailing that already has bookings on it means
        # existing customers were quoted one figure and the package now says
        # another. Bookings freeze their own total_amount once money is in
        # flight (QA C7), so their money is safe — but the two would silently
        # disagree, and the guide's collection sheet is printed from the
        # booking. Cancel-and-rebook is the honest path, so refuse the edit.
        if self.instance and "adult_price" in attrs:
            if attrs["adult_price"] != self.instance.adult_price:
                active = self.instance.bookings.exclude(
                    status=Booking.Status.CANCELLED
                ).count()
                if active:
                    raise serializers.ValidationError(
                        {
                            "adult_price": (
                                f"This package has {active} active booking(s) — "
                                "its price cannot be changed. Those customers were "
                                "quoted the current price and their bookings hold "
                                "it. Cancel and rebook them to re-price."
                            )
                        }
                    )

        # Moving a package to another ship would leave the old ship's rooms
        # attached: the public rooms endpoint would sell cabins that are not
        # on the sailing vessel, and existing bookings would point at rooms
        # the guide can't find on board.
        if self.instance and ship and self.instance.ship_id != ship.id:
            if (
                self.instance.bookings.exclude(status=Booking.Status.CANCELLED)
                .exists()
            ):
                raise serializers.ValidationError(
                    {
                        "ship": (
                            "This package has active bookings — it cannot be "
                            "moved to another ship."
                        )
                    }
                )
            if self.instance.package_rooms.exclude(room__ship=ship).exists():
                raise serializers.ValidationError(
                    {
                        "ship": (
                            "Attached rooms belong to the current ship. Detach "
                            "them (or regenerate rooms) before changing the ship."
                        )
                    }
                )
        return attrs


class StaffPaymentSerializer(serializers.ModelSerializer):
    booking_code = serializers.CharField(source="booking.booking_code", read_only=True)
    # Same floor as the public PaymentInitiateSerializer. Payment.clean()
    # carries this rule too, but DRF never calls model clean() — without an
    # explicit field a negative "payment" would be written and silently erase
    # settled money from the ledger (paid_amount is a SUM over payments).
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )

    class Meta:
        model = Payment
        fields = [
            "id", "booking", "booking_code", "amount", "payment_type", "gateway",
            "transaction_id", "status", "paid_at", "created_at", "gateway_payload",
            # A payment the gateway won't resolve holds its room out of
            # inventory until a human settles it — staff must be able to see
            # and act on it, not just find it in a log stream (QA H5).
            "needs_manual_review", "reconcile_attempts", "last_reconcile_error",
            "last_reconcile_at",
        ]
        read_only_fields = [
            "transaction_id", "created_at", "gateway_payload",
            "reconcile_attempts", "last_reconcile_error", "last_reconcile_at",
            # Resolving a stuck payment goes through the `resolve` action, which
            # drives it under a row lock and writes an audit trail — never a
            # bare PATCH of the status field.
            "needs_manual_review",
        ]

    def validate(self, attrs):
        # Friendly-error mirror of the authoritative re-check the viewset
        # performs under a row lock (check-then-act safe). Manual collections
        # must obey the same ceiling as the public API: recording more than
        # the due would drive due_amount negative, and that number goes
        # straight onto the guide's printed collection sheet.
        booking = attrs.get("booking")
        amount = attrs.get("amount")
        if booking is not None:
            if booking.status in (
                Booking.Status.CANCELLED,
                Booking.Status.COMPLETED,
            ):
                raise serializers.ValidationError(
                    {
                        "booking": (
                            f"This booking is {booking.get_status_display().lower()}"
                            " — payments can no longer be recorded against it."
                        )
                    }
                )
            if amount is not None and amount > booking.due_amount:
                raise serializers.ValidationError(
                    {
                        "amount": (
                            f"Amount exceeds the due amount ({booking.due_amount})."
                        )
                    }
                )
        return attrs


class StaffPaymentResolveSerializer(serializers.Serializer):
    """Staff resolution of a payment the gateway would never settle (QA H7).

    Only the three terminal states are offered: a payment can be closed
    (no money moved — the cabin is released) or settled (money moved — credit
    the customer). Anything else would leave it stuck, which is the bug.
    """

    status = serializers.ChoiceField(
        choices=[
            Payment.Status.SUCCESS,
            Payment.Status.FAILED,
            Payment.Status.CANCELLED,
        ]
    )
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="What the SSLCommerz merchant panel showed — recorded for audit.",
    )


class StaffStatusLogSerializer(serializers.ModelSerializer):
    changed_by_username = serializers.CharField(
        source="changed_by.username", read_only=True, default=None
    )

    class Meta:
        model = BookingStatusLog
        fields = ["old_status", "new_status", "changed_by_username", "created_at"]


class StaffBookingRoomSerializer(serializers.ModelSerializer):
    """One cabin of a booking in the staff booking list/detail."""

    room = serializers.IntegerField(source="room_id", read_only=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True)
    room_type = serializers.CharField(source="room.room_type.name", read_only=True)

    class Meta:
        model = BookingRoom
        fields = [
            "room", "room_number", "room_type", "adult_count", "kid_details",
            # Full passport numbers, unlike the public confirmation view which
            # masks them: this endpoint is behind staff authentication and the
            # numbers are what staff transcribe for the port authority.
            "foreign_guests",
            "room_subtotal", "is_active",
        ]


class StaffBookingListSerializer(serializers.ModelSerializer):
    package_title = serializers.SerializerMethodField()
    rooms = StaffBookingRoomSerializer(many=True, read_only=True)
    # Comma-joined room numbers for compact list/table columns that want a
    # single string (the dashboard's booking list showed one room_number).
    room_number = serializers.SerializerMethodField()
    total_pax = serializers.IntegerField(read_only=True)
    # How many of total_pax are foreign nationals — drives the dashboard's
    # "FN" badge and the foreign-bookings filter without the list having to
    # walk every cabin's guest JSON client-side.
    foreign_pax_count = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = [
            "id", "booking_code", "customer_name", "phone", "email",
            "package", "package_title", "rooms", "room_number",
            "total_pax", "foreign_pax_count", "special_requests",
            "total_amount", "paid_amount", "due_amount", "status",
            "refund_required", "refund_note", "created_at",
        ]

    def get_package_title(self, booking):
        return booking.package.marketing_title or str(booking.package)

    def get_room_number(self, booking):
        return ", ".join(br.room.room_number for br in booking.rooms.all())

    def get_foreign_pax_count(self, booking):
        return sum(len(br.foreign_guests or []) for br in booking.rooms.all())


class StaffBookingDetailSerializer(StaffBookingListSerializer):
    payments = StaffPaymentSerializer(many=True, read_only=True)
    status_logs = StaffStatusLogSerializer(many=True, read_only=True)

    class Meta(StaffBookingListSerializer.Meta):
        fields = StaffBookingListSerializer.Meta.fields + ["payments", "status_logs"]


class StaffBookingUpdateSerializer(serializers.ModelSerializer):
    """Staff may only touch status, contact info and the refund-owed flag;
    pax/room changes go through cancel-and-rebook so pricing and availability
    stay consistent. refund_required/refund_note let staff clear the flag
    once the customer has actually been refunded (with a note saying how)."""

    # Same 1000-char cap as the public create path, so an edited request can't
    # grow past what a customer could originally submit.
    special_requests = serializers.CharField(
        max_length=1000, required=False, allow_blank=True
    )

    class Meta:
        model = Booking
        fields = [
            "status", "customer_name", "phone", "email",
            "special_requests", "refund_required", "refund_note",
        ]

    def validate(self, attrs):
        # Un-cancelling re-occupies EVERY room the booking held — reject cleanly
        # if any of them was resold in the meantime, instead of letting the
        # partial unique constraint on BookingRoom (package, room, is_active)
        # 500 on the re-activation UPDATE.
        new_status = attrs.get("status")
        if (
            new_status
            and new_status != Booking.Status.CANCELLED
            and self.instance
            and self.instance.status == Booking.Status.CANCELLED
        ):
            held_room_ids = list(
                self.instance.rooms.values_list("room_id", flat=True)
            )
            conflict = (
                BookingRoom.objects.filter(
                    package_id=self.instance.package_id,
                    room_id__in=held_room_ids,
                    is_active=True,
                )
                .exclude(booking_id=self.instance.pk)
                .select_related("room", "booking")
                .first()
            )
            if conflict:
                raise serializers.ValidationError(
                    {
                        "status": (
                            f"Room {conflict.room.room_number} is already "
                            f"held by {conflict.booking.booking_code} on this "
                            "package — this booking cannot be reactivated."
                        )
                    }
                )
        return attrs

    def update(self, instance, validated_data):
        was_cancelled = instance.status == Booking.Status.CANCELLED
        for field, value in validated_data.items():
            setattr(instance, field, value)
        try:
            with transaction.atomic():
                instance.save(changed_by=self.context["request"].user)
                # Un-cancelling: the money decides the status, not the client.
                # Reactivating a booking with a 4750 BDT deposit as "pending"
                # left real money in a status that claims none was paid — and
                # PENDING is scanned by neither enforce_due_deadlines (which
                # chases balances) nor close_sailed_bookings, so the booking
                # would silently fall out of every job that manages it.
                if (
                    was_cancelled
                    and instance.status != Booking.Status.CANCELLED
                    and instance.paid_amount > 0
                ):
                    instance.refresh_paid_amount()
        except IntegrityError:
            # Lost the un-cancel race despite the validate() check above.
            raise RoomUnavailable()
        return instance


class StaffBookingCreateSerializer(BookingCreateSerializer):
    """Manual booking from the dashboard — same validation as the public
    API except the cutoff check (staff may book past cutoff, PRD §5.5),
    and non-public packages (CLOSED, past cutoff) are selectable too.

    DRAFT and CANCELLED packages stay off-limits: they are exempt from the
    ship-date overlap constraint, so a booking on one can sell the same
    physical room twice for the same night."""

    enforce_cutoff = False
    package_id = serializers.PrimaryKeyRelatedField(
        queryset=Package.objects.all(), source="package"
    )

    def validate(self, attrs):
        package = attrs["package"]
        if package.status in (Package.Status.DRAFT, Package.Status.CANCELLED):
            raise serializers.ValidationError(
                {
                    "package_id": (
                        f"Cannot create a booking on a "
                        f"{package.get_status_display().lower()} package."
                    )
                }
            )
        return super().validate(attrs)


class StaffInvoiceSerializer(serializers.ModelSerializer):
    booking_code = serializers.CharField(source="booking.booking_code", read_only=True)
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id", "number", "booking", "booking_code", "payment",
            "total_amount", "paid_amount", "due_amount", "booking_status",
            "sent_via", "sent_at", "pdf_url", "created_at",
        ]

    def get_pdf_url(self, invoice):
        """The authenticated download route — never the raw MEDIA_URL.

        The media path is served with no access check at all (QA C1), so the
        dashboard fetches the PDF through the API (carrying its JWT) rather
        than linking straight at the file.
        """
        if not invoice.pdf_file:
            return None
        url = reverse("staff-invoice-pdf", kwargs={"pk": invoice.pk})
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url
