"""Public gallery (GalleryImage) — public read API + staff CRUD.

The /gallery page is fully staff-managed: staff upload photos and write a
caption on each from the dashboard. The public API must be read-only and
active-only; hidden photos stay manageable but never render on the website.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase

from apps.ships.models import GalleryImage, Ship, gallery_image_path
from apps.testing import ThrottlelessTestMixin

User = get_user_model()

# 1x1 px valid GIF — enough for ImageField validation without Pillow gymnastics.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)


def make_image(name="gallery.gif"):
    return SimpleUploadedFile(name, TINY_GIF, content_type="image/gif")


class GalleryTestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ship = Ship.objects.create(name="Gallery Test Ship")
        cls.visible = GalleryImage.objects.create(
            ship=cls.ship, image=make_image(), caption="Sunset deck", sort_order=1
        )
        cls.hidden = GalleryImage.objects.create(
            ship=cls.ship, image=make_image(), caption="Hidden", is_active=False
        )
        cls.staff = User.objects.create_user(
            username="gallerystaff", password="pass12345", is_staff=True
        )

    def auth(self):
        tokens = self.client.post(
            "/api/staff/login/", {"username": "gallerystaff", "password": "pass12345"}
        ).data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    # ── model behavior ────────────────────────────────────────────────────

    def test_upload_path_keyed_by_ship(self):
        image = GalleryImage(ship=self.ship)
        path = gallery_image_path(image, "photo.jpg")
        self.assertEqual(path, f"gallery/{self.ship.pk}/photo.jpg")

    # ── public API ────────────────────────────────────────────────────────

    def test_public_list_active_only(self):
        response = self.client.get("/api/gallery/")
        self.assertEqual(response.status_code, 200)
        ids = [img["id"] for img in response.data]
        self.assertIn(self.visible.pk, ids)
        self.assertNotIn(self.hidden.pk, ids)

    def test_public_payload_shape(self):
        response = self.client.get("/api/gallery/")
        img = next(i for i in response.data if i["id"] == self.visible.pk)
        self.assertEqual(img["caption"], "Sunset deck")
        self.assertEqual(img["sort_order"], 1)
        self.assertTrue(img["image"])

    def test_public_api_is_read_only(self):
        response = self.client.post("/api/gallery/", {})
        self.assertIn(response.status_code, (401, 403, 405))

    # ── staff API ─────────────────────────────────────────────────────────

    def test_staff_endpoints_require_auth(self):
        self.assertEqual(self.client.get("/api/staff/gallery-images/").status_code, 401)
        self.assertEqual(
            self.client.post("/api/staff/gallery-images/", {}).status_code, 401
        )

    def test_staff_list_includes_hidden(self):
        self.auth()
        response = self.client.get("/api/staff/gallery-images/")
        self.assertEqual(response.status_code, 200)
        ids = [img["id"] for img in response.data]
        self.assertIn(self.visible.pk, ids)
        self.assertIn(self.hidden.pk, ids)

    def test_staff_upload_with_caption(self):
        self.auth()
        response = self.client.post(
            "/api/staff/gallery-images/",
            {
                "ship": self.ship.pk,
                "image": make_image("new.gif"),
                "caption": "Morning mist over the delta",
                "sort_order": 5,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["caption"], "Morning mist over the delta")
        self.assertTrue(response.data["image_url"])
        self.assertNotIn("image", response.data)  # upload field is write-only
        created = GalleryImage.objects.get(pk=response.data["id"])
        self.assertTrue(created.is_active)

    def test_staff_edit_caption_and_hide(self):
        self.auth()
        response = self.client.patch(
            f"/api/staff/gallery-images/{self.visible.pk}/",
            {"caption": "New caption", "is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.visible.refresh_from_db()
        self.assertEqual(self.visible.caption, "New caption")
        self.assertFalse(self.visible.is_active)
        # Hidden photo no longer appears publicly.
        public_ids = [img["id"] for img in self.client.get("/api/gallery/").data]
        self.assertNotIn(self.visible.pk, public_ids)

    def test_staff_delete(self):
        self.auth()
        doomed = GalleryImage.objects.create(ship=self.ship, image=make_image())
        response = self.client.delete(f"/api/staff/gallery-images/{doomed.pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(GalleryImage.objects.filter(pk=doomed.pk).exists())

    def test_oversize_image_rejected(self):
        self.auth()
        # Valid GIF header followed by >10 MB of padding.
        big = SimpleUploadedFile(
            "big.gif", TINY_GIF + b"\x00" * (10 * 1024 * 1024), content_type="image/gif"
        )
        response = self.client.post(
            "/api/staff/gallery-images/",
            {"ship": self.ship.pk, "image": big},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("image", response.data)
