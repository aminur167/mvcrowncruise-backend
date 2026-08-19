from decimal import Decimal

from django.db import migrations


def add_3person_room_type(apps, schema_editor):
    RoomType = apps.get_model("ships", "RoomType")
    RoomType.objects.get_or_create(
        name="3-Person Room",
        defaults={
            "max_adults": 3,
            "max_kids": 1,
            # Placeholder price — admin must set the real price before opening
            # bookings for packages that include room 302.
            "base_price": Decimal("2750.00"),
        },
    )


def remove_3person_room_type(apps, schema_editor):
    RoomType = apps.get_model("ships", "RoomType")
    RoomType.objects.filter(name="3-Person Room", rooms__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ships", "0002_room_floor_number"),
    ]

    operations = [
        migrations.RunPython(add_3person_room_type, remove_3person_room_type),
    ]
