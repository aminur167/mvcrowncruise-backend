"""Seed the real MV Alaska layout (31 rooms) from the client's room-layout PDF.

Source: docs/Alaska Room Layout for corporate.pdf
1st floor (200-series): 12 × 2-person, 5 × 4-person (206, 207, 208, 216, 217)
2nd floor (300-series): 12 × 2-person, 1 × 3-person (302), 1 × 4-person (306)

Idempotent: existing rooms are matched by (ship, room_number) and updated
in place; nothing is deleted.
"""

from decimal import Decimal

from django.db import migrations

FLOOR_1_2PAX = ["201", "202", "203", "204", "205", "209", "210", "211", "212", "213", "214", "215"]
FLOOR_1_4PAX = ["206", "207", "208", "216", "217"]
FLOOR_2_2PAX = ["301", "303", "304", "305", "307", "308", "309", "310", "311", "312", "313", "314"]
FLOOR_2_3PAX = ["302"]
FLOOR_2_4PAX = ["306"]


def seed_rooms(apps, schema_editor):
    Ship = apps.get_model("ships", "Ship")
    Room = apps.get_model("ships", "Room")
    RoomType = apps.get_model("ships", "RoomType")

    ship, _ = Ship.objects.get_or_create(name="MV Alaska")

    # Ensure all room types exist (fresh databases won't have them yet).
    type_2p, _ = RoomType.objects.get_or_create(
        name="2-Person Room",
        defaults={"max_adults": 2, "max_kids": 1, "base_price": Decimal("2000.00")},
    )
    type_3p, _ = RoomType.objects.get_or_create(
        name="3-Person Room",
        defaults={"max_adults": 3, "max_kids": 1, "base_price": Decimal("2750.00")},
    )
    type_4p, _ = RoomType.objects.get_or_create(
        name="4-Person Room",
        defaults={"max_adults": 4, "max_kids": 2, "base_price": Decimal("3500.00")},
    )

    layout = (
        [(num, type_2p, 1) for num in FLOOR_1_2PAX]
        + [(num, type_4p, 1) for num in FLOOR_1_4PAX]
        + [(num, type_2p, 2) for num in FLOOR_2_2PAX]
        + [(num, type_3p, 2) for num in FLOOR_2_3PAX]
        + [(num, type_4p, 2) for num in FLOOR_2_4PAX]
    )
    for room_number, room_type, floor in layout:
        Room.objects.update_or_create(
            ship=ship,
            room_number=room_number,
            defaults={"room_type": room_type, "floor_number": floor},
        )


def unseed_rooms(apps, schema_editor):
    """Reverse: only remove seeded rooms that have no bookings."""
    Ship = apps.get_model("ships", "Ship")
    Room = apps.get_model("ships", "Room")
    ship = Ship.objects.filter(name="MV Alaska").first()
    if ship:
        all_numbers = (
            FLOOR_1_2PAX + FLOOR_1_4PAX + FLOOR_2_2PAX + FLOOR_2_3PAX + FLOOR_2_4PAX
        )
        Room.objects.filter(
            ship=ship, room_number__in=all_numbers, bookings__isnull=True
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ships", "0003_add_3person_room_type"),
        ("bookings", "0001_initial"),  # reverse queries the bookings relation
    ]

    operations = [
        migrations.RunPython(seed_rooms, unseed_rooms),
    ]
