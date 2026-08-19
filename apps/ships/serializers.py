from rest_framework import serializers

from .imaging import thumbnail_url
from .models import (
    Cabin,
    CabinImage,
    FoodMenuItem,
    GalleryImage,
    Room,
    RoomImage,
    RoomType,
    Ship,
)


class RoomTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoomType
        fields = ["id", "name", "max_adults", "max_kids", "base_price"]


class RoomImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(read_only=True, use_url=True)
    # A CDN-rendered small version, for previews that must not pull a megabyte
    # per cabin (the deck-plan hover card fetches these as the mouse moves).
    # Falls back to the full image off Cloudinary — see apps.ships.imaging.
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = RoomImage
        fields = ["id", "image", "thumbnail_url", "caption", "sort_order"]

    def get_thumbnail_url(self, image):
        return thumbnail_url(image.image.url if image.image else "")


class PreviewImageSerializer(serializers.Serializer):
    """One image in a cabin preview, whatever model it came from.

    A room's own photos and a cabin type's showcase photos are different models
    with different extra fields; the preview only needs the three they share,
    and emitting one shape means the UI has no branch to get wrong.
    """

    id = serializers.IntegerField(read_only=True)
    image = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    caption = serializers.CharField(read_only=True)

    def get_image(self, obj):
        return obj.image.url if obj.image else ""

    def get_thumbnail_url(self, obj):
        return thumbnail_url(obj.image.url if obj.image else "")


class RoomSerializer(serializers.ModelSerializer):
    room_type = RoomTypeSerializer(read_only=True)
    images = RoomImageSerializer(many=True, read_only=True)

    class Meta:
        model = Room
        fields = ["id", "room_number", "floor_number", "room_type", "images"]


class ShipSerializer(serializers.ModelSerializer):
    layout_image = serializers.ImageField(read_only=True, use_url=True)
    total_rooms = serializers.IntegerField(read_only=True)

    class Meta:
        model = Ship
        fields = ["id", "name", "layout_image", "total_rooms"]


class ShipLayoutSerializer(ShipSerializer):
    """Ship + rooms grouped by floor. Static structure only — per-package
    availability comes from /api/packages/{id}/rooms/."""

    floors = serializers.SerializerMethodField()

    class Meta(ShipSerializer.Meta):
        fields = ShipSerializer.Meta.fields + ["floors"]

    def get_floors(self, ship):
        rooms = (
            ship.rooms.select_related("room_type")
            .prefetch_related("images")
            .order_by("floor_number", "room_number")
        )
        floors = {}
        for room in rooms:
            floors.setdefault(room.floor_number, []).append(
                RoomSerializer(room).data
            )
        return [
            {"floor_number": floor, "rooms": floor_rooms}
            for floor, floor_rooms in sorted(
                floors.items(), key=lambda item: (item[0] is None, item[0] or 0)
            )
        ]


class CabinImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(read_only=True, use_url=True)

    class Meta:
        model = CabinImage
        fields = ["id", "image", "caption", "is_main", "sort_order"]


class CabinListSerializer(serializers.ModelSerializer):
    """Card payload for the /cabins grid. Deliberately price-free — the cabins
    pages are showcase content; pricing belongs to the booking flow."""

    occupancy = serializers.CharField(source="occupancy_label", read_only=True)
    main_image = serializers.SerializerMethodField()

    class Meta:
        model = Cabin
        fields = [
            "id",
            "slug",
            "name",
            "tagline",
            "size_label",
            "occupancy",
            "features",
            "main_image",
        ]

    def get_main_image(self, cabin):
        image = cabin.main_image
        if not image:
            return None
        return CabinImageSerializer(image, context=self.context).data


class CabinDetailSerializer(CabinListSerializer):
    images = CabinImageSerializer(many=True, read_only=True)

    class Meta(CabinListSerializer.Meta):
        fields = CabinListSerializer.Meta.fields + [
            "description",
            "amenities",
            "highlights",
            "images",
        ]


class GalleryImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(read_only=True, use_url=True)

    class Meta:
        model = GalleryImage
        fields = ["id", "image", "caption", "sort_order"]


class FoodMenuSerializer(ShipSerializer):
    """Ship's food menu grouped by day, then by meal type. Chef selects the
    day's actual dishes from these active items — this is a selection pool,
    not a fixed daily plan."""

    note = serializers.SerializerMethodField()
    days = serializers.SerializerMethodField()

    class Meta(ShipSerializer.Meta):
        fields = ShipSerializer.Meta.fields + ["note", "days"]

    def get_note(self, ship):
        return "Chef will select the day's menu from the above items."

    def get_days(self, ship):
        items = ship.food_menu_items.filter(is_active=True).order_by(
            "day", "meal_type", "order", "id"
        )
        days = {}
        for item in items:
            meals = days.setdefault(item.day, {})
            meals.setdefault(item.meal_type, []).append(item.name)

        day_labels = dict(FoodMenuItem.Day.choices)
        meal_labels = dict(FoodMenuItem.MealType.choices)
        meal_order = [choice for choice, _ in FoodMenuItem.MealType.choices]

        return [
            {
                "day": day,
                "day_label": day_labels[day],
                "meals": [
                    {
                        "meal_type": meal_type,
                        "meal_type_label": meal_labels[meal_type],
                        "items": meals[meal_type],
                    }
                    for meal_type in meal_order
                    if meal_type in meals
                ],
            }
            for day, meals in sorted(days.items())
        ]
