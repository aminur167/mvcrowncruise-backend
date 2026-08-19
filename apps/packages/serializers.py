from rest_framework import serializers

from apps.ships.serializers import (
    PreviewImageSerializer,
    RoomImageSerializer,
    RoomTypeSerializer,
)

from .models import ForeignerSurcharge, KidPricingRule, Package, PackageRoom


class ShipMiniSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)


class KidPricingRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = KidPricingRule
        fields = ["min_age", "max_age", "charge_type", "amount"]


class PackageListSerializer(serializers.ModelSerializer):
    """Public package representation.

    Internal state (raw status, is_booking_open) is deliberately not exposed;
    the outside world only sees the combined is_bookable / booking_status.
    """

    ship = ShipMiniSerializer(read_only=True)
    nights = serializers.SerializerMethodField()
    days = serializers.SerializerMethodField()
    is_bookable = serializers.SerializerMethodField()
    booking_status = serializers.SerializerMethodField()
    # The surcharge is a global policy row, but it is published ON the package
    # because that is where a booking client needs it: the wizard quotes a
    # sailing, not a settings table. Keeping the field names means moving the
    # storage did not change the public API contract at all.
    foreigner_adult_surcharge = serializers.SerializerMethodField()
    foreigner_kid_surcharge = serializers.SerializerMethodField()

    hero_image = serializers.ImageField(read_only=True, use_url=True)

    class Meta:
        model = Package
        fields = [
            "id",
            "ship",
            "start_date",
            "end_date",
            "nights",
            "days",
            "adult_price",
            # Published so the booking wizard can tell a foreign guest what the
            # surcharge will be BEFORE they fill in a passport — the quote still
            # computes the actual money, this is only the advertised rate.
            "foreigner_adult_surcharge",
            "foreigner_kid_surcharge",
            "booking_cutoff_datetime",
            "is_bookable",
            "booking_status",
            "marketing_title",
            "marketing_description",
            "hero_image",
            "highlights",
        ]

    def get_nights(self, package):
        return package.effective_nights()

    def get_days(self, package):
        return package.effective_days()

    def get_is_bookable(self, package):
        return package.is_bookable()

    def get_booking_status(self, package):
        return "open" if package.is_bookable() else "closed"

    def _surcharge(self):
        """The policy row, fetched once per serialization pass rather than once
        per package — the list endpoint renders every open sailing."""
        if not hasattr(self, "_surcharge_cache"):
            self._surcharge_cache = ForeignerSurcharge.get_solo()
        return self._surcharge_cache

    def get_foreigner_adult_surcharge(self, package):
        return str(self._surcharge().adult_amount)

    def get_foreigner_kid_surcharge(self, package):
        return str(self._surcharge().kid_amount)


class PackageDetailSerializer(PackageListSerializer):
    kid_pricing_rules = serializers.SerializerMethodField()

    class Meta(PackageListSerializer.Meta):
        fields = PackageListSerializer.Meta.fields + ["kid_pricing_rules"]

    def get_kid_pricing_rules(self, package):
        return KidPricingRuleSerializer(KidPricingRule.objects.all(), many=True).data


class PackageRoomSerializer(serializers.ModelSerializer):
    """One room within a package, flattened, with its availability status.

    Expects a queryset annotated with `is_booked` (Exists subquery) — see
    PackageViewSet.rooms. Never exposes any booking/customer data.
    """

    id = serializers.IntegerField(source="room.id", read_only=True)
    room_number = serializers.CharField(source="room.room_number", read_only=True)
    floor_number = serializers.IntegerField(source="room.floor_number", read_only=True)
    room_type = RoomTypeSerializer(source="room.room_type", read_only=True)
    images = RoomImageSerializer(source="room.images", many=True, read_only=True)
    # What to SHOW for this cabin, which is not always what was photographed
    # for it. Rooms are photographed one by one and most never are, so a
    # preview keyed strictly on `images` would be blank for most of the deck.
    # These fall back to the showcase photos of the same room type, which are
    # real pictures of an identical cabin — with `preview_source` saying so, so
    # the UI can label them honestly rather than implying "this exact room".
    preview_images = serializers.SerializerMethodField()
    preview_source = serializers.SerializerMethodField()
    availability = serializers.SerializerMethodField()

    class Meta:
        model = PackageRoom
        fields = [
            "id",
            "room_number",
            "floor_number",
            "room_type",
            "images",
            "preview_images",
            "preview_source",
            "availability",
        ]

    def _cabin_images(self, package_room):
        """Showcase photos for this room's type.

        Read from a map built once by the view — resolving it per room would be
        one query per cabin on a 31-cabin deck.
        """
        by_type = self.context.get("cabin_images_by_room_type") or {}
        return by_type.get(package_room.room.room_type_id) or []

    def get_preview_images(self, package_room):
        own = list(package_room.room.images.all())
        source = own or self._cabin_images(package_room)
        return PreviewImageSerializer(source, many=True).data

    def get_preview_source(self, package_room):
        if package_room.room.images.all():
            return "room"
        return "room_type" if self._cabin_images(package_room) else None

    def get_availability(self, package_room):
        # An admin hold is surfaced to the public as "booked" — the room is
        # simply not on sale, and "booked" reads more naturally to a customer
        # than "unavailable" (the internal block state/reason still never leaks;
        # only the label is shared). A room genuinely dropped from inventory
        # (is_available=False) stays "unavailable".
        if package_room.is_booked or package_room.is_blocked:
            return "booked"
        if not package_room.is_available:
            return "unavailable"
        return "available"
