"""Room gallery images (RoomImage) — storage path, ordering, API exposure.

Uploaded from the admin per room; served to the frontend inside the existing
room payloads (ship layout + package room map). Under test the storage is the
local filesystem (settings.TESTING), so no live bucket/CDN is touched.
"""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.ships.models import Room, RoomImage, RoomType, Ship, room_image_path
from apps.testing import ThrottlelessTestMixin

# 1x1 px valid GIF — enough for ImageField validation without Pillow gymnastics.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)


def make_image(name="room.gif"):
    return SimpleUploadedFile(name, TINY_GIF, content_type="image/gif")


class RoomImageTestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ship = Ship.objects.create(name="Test Ship")
        cls.room_type, _ = RoomType.objects.get_or_create(
            name="2-Person Room",
            defaults=dict(max_adults=2, max_kids=1, base_price=Decimal("2000.00")),
        )
        cls.room = Room.objects.create(
            ship=cls.ship, room_type=cls.room_type, room_number="T1", floor_number=1
        )

    def test_upload_path_is_keyed_by_ship_and_room(self):
        image = RoomImage(room=self.room)
        path = room_image_path(image, "photo.jpg")
        self.assertEqual(path, f"rooms/{self.ship.pk}/T1/photo.jpg")

    def test_images_ordered_by_sort_order_in_layout_api(self):
        second = RoomImage.objects.create(
            room=self.room, image=make_image(), caption="Bed", sort_order=2
        )
        first = RoomImage.objects.create(
            room=self.room, image=make_image(), caption="Window view", sort_order=1
        )

        response = self.client.get(f"/api/ships/{self.ship.pk}/layout/")
        self.assertEqual(response.status_code, 200)
        rooms = response.json()["floors"][0]["rooms"]
        images = rooms[0]["images"]

        self.assertEqual(
            [img["id"] for img in images], [first.pk, second.pk],
            "images must come back in sort_order, not insertion order",
        )
        self.assertEqual(images[0]["caption"], "Window view")
        for img in images:
            self.assertEqual(
                set(img.keys()),
                {"id", "image", "thumbnail_url", "caption", "sort_order"},
            )
            self.assertTrue(img["image"], "each image must carry a usable URL")

    def test_room_without_images_serializes_empty_list(self):
        response = self.client.get(f"/api/ships/{self.ship.pk}/layout/")
        self.assertEqual(response.status_code, 200)
        rooms = response.json()["floors"][0]["rooms"]
        self.assertEqual(rooms[0]["images"], [])

    def test_deleting_room_cascades_images(self):
        room = Room.objects.create(
            ship=self.ship, room_type=self.room_type, room_number="T9"
        )
        RoomImage.objects.create(room=room, image=make_image())
        room.delete()
        self.assertFalse(RoomImage.objects.filter(room_id=room.pk).exists())
