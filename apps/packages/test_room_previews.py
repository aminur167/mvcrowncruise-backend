"""Cabin preview images on the availability endpoint.

The deck-plan hover card reads these. Two things matter: a cabin nobody
photographed still shows something (or the preview is blank for most of the
ship), and building that fallback must not cost a query per cabin.
"""

from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.bookings.test_api import build_fixtures
from apps.ships.imaging import THUMBNAIL_SPEC, thumbnail_url
from apps.ships.models import Cabin, CabinImage, RoomImage
from apps.testing import ThrottlelessTestMixin

# A 1x1 GIF — ImageField only needs something it can identify as an image.
PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


class ThumbnailUrlTests(ThrottlelessTestMixin, APITestCase):
    def test_cloudinary_urls_gain_a_transform(self):
        url = thumbnail_url(
            "https://res.cloudinary.com/demo/image/upload/v1/rooms/a.jpg"
        )
        self.assertEqual(
            url,
            f"https://res.cloudinary.com/demo/image/upload/{THUMBNAIL_SPEC}/v1/rooms/a.jpg",
        )

    def test_other_urls_are_left_alone(self):
        """Local storage in development and tests — a smaller file is an
        optimisation, not a correctness requirement."""
        for url in ("/media/rooms/a.jpg", "https://example.com/a.jpg", ""):
            with self.subTest(url=url):
                self.assertEqual(thumbnail_url(url), url)

    def test_a_transform_is_never_stacked_twice(self):
        once = thumbnail_url(
            "https://res.cloudinary.com/demo/image/upload/v1/rooms/a.jpg"
        )
        self.assertEqual(thumbnail_url(once), once)


@override_settings(TESTING=True)
class RoomPreviewImageTests(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures(ship_name="Preview Ship")

    def rooms(self):
        response = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 200)
        return {r["room_number"]: r for r in response.data}

    def add_room_photo(self, room, name="own.gif"):
        image = RoomImage(room=room)
        image.image.save(name, ContentFile(PIXEL), save=True)
        return image

    def add_cabin_photo(self, room_type, name="cabin.gif"):
        cabin, _ = Cabin.objects.get_or_create(
            ship=self.ship,
            room_type=room_type,
            slug=f"cabin-{room_type.pk}",
            defaults={"name": f"{room_type.name} showcase"},
        )
        image = CabinImage(cabin=cabin)
        image.image.save(name, ContentFile(PIXEL), save=True)
        return image

    def test_a_photographed_room_previews_its_own_photos(self):
        self.add_room_photo(self.room_2p)
        row = self.rooms()["T1"]
        self.assertEqual(row["preview_source"], "room")
        self.assertEqual(len(row["preview_images"]), 1)
        self.assertIn("thumbnail_url", row["preview_images"][0])

    def test_an_unphotographed_room_falls_back_to_its_cabin_type(self):
        """Rooms are photographed one at a time and most never are — without
        this the hover card would be blank across most of the deck."""
        self.add_cabin_photo(self.type_4p)
        row = self.rooms()["T2"]
        self.assertEqual(row["preview_source"], "room_type")
        self.assertEqual(len(row["preview_images"]), 1)

    def test_the_rooms_own_photos_win_over_the_cabin_types(self):
        self.add_cabin_photo(self.type_2p)
        self.add_room_photo(self.room_2p)
        self.assertEqual(self.rooms()["T1"]["preview_source"], "room")

    def test_no_photos_anywhere_is_reported_honestly(self):
        row = self.rooms()["T1"]
        self.assertIsNone(row["preview_source"])
        self.assertEqual(row["preview_images"], [])

    def test_an_inactive_cabin_is_not_used_as_a_fallback(self):
        """Hidden from the showcase pages means hidden here too."""
        self.add_cabin_photo(self.type_4p)
        Cabin.objects.update(is_active=False)
        self.assertIsNone(self.rooms()["T2"]["preview_source"])

    def test_the_fallback_costs_one_query_however_many_cabins(self):
        """A lookup per room would add 31 queries to the most-hit read in the
        app. The count must not move when a second room type is photographed."""
        self.add_cabin_photo(self.type_2p, name="a.gif")
        with self.assertNumQueries(self.count_room_queries()):
            self.client.get(f"/api/packages/{self.package.id}/rooms/")

        self.add_cabin_photo(self.type_4p, name="b.gif")
        with self.assertNumQueries(self.count_room_queries()):
            self.client.get(f"/api/packages/{self.package.id}/rooms/")

    def count_room_queries(self):
        """Baseline: whatever the endpoint costs right now. Asserted against
        itself across a change in data volume, not pinned to a magic number
        that unrelated work would have to keep updating."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(f"/api/packages/{self.package.id}/rooms/")
        return len(ctx.captured_queries)
