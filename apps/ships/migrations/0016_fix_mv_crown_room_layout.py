from django.db import migrations


# Corrects the seeded MV The Crown room catalog to match the ship's real
# room layout ("Crown New Room Layout for corporate.pdf"): the original seed
# data was copied from a different ship's template and had 31 rooms with
# several mismatched capacities instead of the Crown's real 29 rooms / 75 pax.
ROOMS_TO_DELETE = ["216", "217"]

# room_number -> RoomType.name
ROOM_TYPE_FIXES = {
    "201": "3-Person Room",
    "206": "2-Person Room",
    "214": "4-Person Room",
    "215": "4-Person Room",
    "302": "2-Person Room",
    "303": "3-Person Room",
    "304": "3-Person Room",
    "310": "3-Person Room",
    "311": "3-Person Room",
    "313": "4-Person Room",
}


def fix_room_layout(apps, schema_editor):
    Room = apps.get_model("ships", "Room")
    RoomType = apps.get_model("ships", "RoomType")
    PackageRoom = apps.get_model("packages", "PackageRoom")

    PackageRoom.objects.filter(room__room_number__in=ROOMS_TO_DELETE).delete()
    Room.objects.filter(room_number__in=ROOMS_TO_DELETE).delete()

    for room_number, type_name in ROOM_TYPE_FIXES.items():
        room_type = RoomType.objects.filter(name=type_name).first()
        if room_type is not None:
            Room.objects.filter(room_number=room_number).update(room_type=room_type)


def revert_room_layout(apps, schema_editor):
    # The old template data (216/217 and the mismatched capacities) was never
    # correct for this ship, so there is nothing meaningful to restore.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ships", "0015_ship_group_min_pax_ship_refund_claim_window_days_and_more"),
        ("packages", "0010_foreigner_surcharge_global"),
    ]

    operations = [
        migrations.RunPython(fix_room_layout, revert_room_layout),
    ]
