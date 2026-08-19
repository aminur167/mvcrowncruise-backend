from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.bookings.models import Booking, BookingRoom, Payment
from apps.bookings.test_api import build_fixtures
from apps.testing import ThrottlelessTestMixin, create_booking

User = get_user_model()


class StaffApiTestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures()
        cls.staff = User.objects.create_user(
            username="staffer", password="pass12345", is_staff=True
        )
        cls.customer = User.objects.create_user(
            username="notstaff", password="pass12345", is_staff=False
        )

    def login(self, username="staffer", password="pass12345"):
        response = self.client.post(
            "/api/staff/login/", {"username": username, "password": password}
        )
        return response

    def auth(self):
        tokens = self.login().data
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return tokens

    def make_booking(self, room=None, **overrides):
        adult_count = overrides.pop("adult_count", 2)
        kid_details = overrides.pop("kid_details", [])
        return create_booking(
            self.package,
            rooms=[
                {
                    "room": room or self.room_4p,
                    "adult_count": adult_count,
                    "kid_details": kid_details,
                }
            ],
            **overrides,
        )


class StaffAuthTests(StaffApiTestCase):
    def test_login_returns_tokens_and_user(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["username"], "staffer")

    def test_non_staff_login_rejected(self):
        response = self.login(username="notstaff")
        self.assertEqual(response.status_code, 400)

    def test_anonymous_gets_401(self):
        for url in ("/api/staff/bookings/", "/api/staff/packages/", "/api/staff/overview/"):
            self.assertEqual(self.client.get(url).status_code, 401, url)

    def test_non_staff_token_gets_403(self):
        # Non-staff can't log in via staff login; craft a token directly.
        from rest_framework_simplejwt.tokens import RefreshToken

        token = RefreshToken.for_user(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self.assertEqual(self.client.get("/api/staff/bookings/").status_code, 403)

    def test_refresh_rotates(self):
        tokens = self.login().data
        response = self.client.post(
            "/api/staff/login/refresh/", {"refresh": tokens["refresh"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)

    def test_logout_blacklists_refresh(self):
        tokens = self.auth()
        response = self.client.post("/api/staff/logout/", {"refresh": tokens["refresh"]})
        self.assertEqual(response.status_code, 200)
        reuse = self.client.post(
            "/api/staff/login/refresh/", {"refresh": tokens["refresh"]}
        )
        self.assertEqual(reuse.status_code, 401)


class StaffBookingApiTests(StaffApiTestCase):
    def test_list_filters_and_detail(self):
        booking = self.make_booking()
        self.auth()
        listed = self.client.get(f"/api/staff/bookings/?package={self.package.id}")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 1)

        detail = self.client.get(f"/api/staff/bookings/{booking.id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("payments", detail.data)
        self.assertIn("status_logs", detail.data)

    def test_summary_returns_filtered_totals(self):
        self.make_booking()  # total 9500, unpaid
        self.auth()
        response = self.client.get("/api/staff/bookings/summary/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["total_amount"], "9500.00")
        self.assertEqual(response.data["due_amount"], "9500.00")
        # by_status zero-fills every status.
        self.assertIn(Booking.Status.PENDING, response.data["by_status"])
        # Filtering by a non-matching status yields an empty summary.
        filtered = self.client.get("/api/staff/bookings/summary/?status=cancelled")
        self.assertEqual(filtered.data["count"], 0)
        self.assertEqual(filtered.data["due_amount"], "0.00")

    def test_status_patch_records_changed_by(self):
        booking = self.make_booking()
        self.auth()
        response = self.client.patch(
            f"/api/staff/bookings/{booking.id}/", {"status": "cancelled"}
        )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        log = booking.status_logs.first()
        self.assertEqual(log.changed_by.username, "staffer")

    def test_manual_booking_create_bypasses_cutoff(self):
        from django.utils import timezone

        self.package.booking_cutoff_datetime = timezone.now() - timezone.timedelta(hours=1)
        self.package.save()
        self.auth()
        response = self.client.post(
            "/api/staff/bookings/",
            {
                "package_id": self.package.id,
                "rooms": [
                    {"room_id": self.room_2p.id, "adult_count": 2, "kid_details": []}
                ],
                "customer_name": "Walkin Guest",
                "phone": "01900000000",
                "email": "walkin@example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_pax_limits_still_enforced_for_staff(self):
        self.auth()
        response = self.client.post(
            "/api/staff/bookings/",
            {
                "package_id": self.package.id,
                "rooms": [
                    {"room_id": self.room_2p.id, "adult_count": 5, "kid_details": []}
                ],
                "customer_name": "Too Many",
                "phone": "01900000001",
                "email": "toomany@example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class StaffPaymentApiTests(StaffApiTestCase):
    def test_manual_cash_payment_updates_booking(self):
        booking = self.make_booking()  # total 9500
        self.auth()
        response = self.client.post(
            "/api/staff/payments/",
            {
                "booking": booking.id,
                "amount": "4000.00",
                "payment_type": "partial",
                "gateway": "cash",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        booking.refresh_from_db()
        self.assertEqual(booking.paid_amount, Decimal("4000.00"))
        self.assertEqual(booking.due_amount, Decimal("5500.00"))
        self.assertEqual(booking.status, Booking.Status.PARTIALLY_PAID)
        payment = booking.payments.first()
        self.assertEqual(payment.status, Payment.Status.SUCCESS)
        self.assertIsNotNone(payment.paid_at)


class StaffPackageApiTests(StaffApiTestCase):
    def test_list_includes_stats_and_drafts(self):
        from apps.packages.models import Package

        draft = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 5, 1),
            end_date=date(2099, 5, 3),
            adult_price=Decimal("2500.00"),
            status=Package.Status.DRAFT,
        )
        self.make_booking()
        self.auth()
        response = self.client.get("/api/staff/packages/")
        self.assertEqual(response.status_code, 200)
        ids = [p["id"] for p in response.data["results"]]
        self.assertIn(draft.id, ids)  # public API would hide this
        row = next(p for p in response.data["results"] if p["id"] == self.package.id)
        self.assertEqual(row["bookings_count"], 1)
        self.assertEqual(row["due_total"], "9500.00")

    def test_crud_and_actions(self):
        self.auth()
        created = self.client.post(
            "/api/staff/packages/",
            {
                "ship": self.ship.id,
                "start_date": "2099-06-10",
                "end_date": "2099-06-12",
                "adult_price": "3200.00",
                "status": "open",
                "is_booking_open": True,
                "marketing_title": "Test Sailing",
                "highlights": ["One", "Two"],
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        pk = created.data["id"]

        closed = self.client.post(f"/api/staff/packages/{pk}/close-booking/")
        self.assertEqual(closed.status_code, 200)

        rooms = self.client.post(f"/api/staff/packages/{pk}/generate-rooms/")
        self.assertEqual(rooms.status_code, 200)

        deleted = self.client.delete(f"/api/staff/packages/{pk}/")
        self.assertEqual(deleted.status_code, 204)

    def test_delete_with_bookings_gives_409(self):
        self.make_booking()
        self.auth()
        response = self.client.delete(f"/api/staff/packages/{self.package.id}/")
        self.assertEqual(response.status_code, 409)

    def test_room_map_shows_booking_and_availability(self):
        booked = self.make_booking(room=self.room_4p)
        cancelled = self.make_booking(room=self.room_2p, customer_name="Cancelled Guy")
        cancelled.status = Booking.Status.CANCELLED
        cancelled.save()

        self.auth()
        response = self.client.get(f"/api/staff/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 200)
        by_room = {r["room_number"]: r for r in response.data}

        self.assertEqual(by_room["T2"]["availability"], "booked")
        self.assertEqual(by_room["T2"]["booking"]["booking_code"], booked.booking_code)
        self.assertEqual(by_room["T2"]["booking"]["customer_name"], "Rahim Uddin")
        self.assertEqual(by_room["T2"]["booking"]["due_amount"], "9500.00")
        # A cancelled booking releases the room.
        self.assertEqual(by_room["T1"]["availability"], "available")
        self.assertIsNone(by_room["T1"]["booking"])

    def test_guide_report_pdf(self):
        paid = self.make_booking()
        Payment.objects.create(
            booking=paid,
            amount=paid.total_amount,
            payment_type=Payment.PaymentType.FULL,
            status=Payment.Status.SUCCESS,
        )
        cancelled = self.make_booking(room=self.room_2p, customer_name="Cancelled Guy")
        cancelled.status = Booking.Status.CANCELLED
        cancelled.save()

        self.auth()
        response = self.client.get(
            f"/api/staff/packages/{self.package.id}/guide-report/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))


class StaffSettingsApiTests(StaffApiTestCase):
    def test_room_type_price_update(self):
        self.auth()
        response = self.client.patch(
            f"/api/staff/room-types/{self.type_2p.id}/", {"base_price": "2200.00"}
        )
        self.assertEqual(response.status_code, 200)
        self.type_2p.refresh_from_db()
        self.assertEqual(self.type_2p.base_price, Decimal("2200.00"))

    def test_kid_rule_overlap_rejected(self):
        self.auth()
        response = self.client.post(
            "/api/staff/kid-pricing-rules/",
            {"min_age": 2, "max_age": 6, "charge_type": "fixed", "amount": "1000.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_kid_rule_boundary_update(self):
        # The PRD's 8-vs-9 open item: staff moves the boundary from data.
        from apps.packages.models import KidPricingRule

        fixed = KidPricingRule.objects.get(charge_type="fixed")
        full = KidPricingRule.objects.get(charge_type="full_adult")
        self.auth()
        r1 = self.client.patch(
            f"/api/staff/kid-pricing-rules/{full.id}/", {"min_age": 9}, format="json"
        )
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.patch(
            f"/api/staff/kid-pricing-rules/{fixed.id}/", {"max_age": 9}, format="json"
        )
        self.assertEqual(r2.status_code, 200)


class StaffFoodMenuItemTests(StaffApiTestCase):
    def test_anonymous_gets_401(self):
        response = self.client.get("/api/staff/food-menu-items/")
        self.assertEqual(response.status_code, 401)

    def test_create_list_update_delete_item(self):
        from apps.ships.models import FoodMenuItem

        self.auth()
        create = self.client.post(
            "/api/staff/food-menu-items/",
            {
                "ship": self.ship.id,
                "day": FoodMenuItem.Day.DAY_1,
                "meal_type": FoodMenuItem.MealType.BREAKFAST,
                "name": "Test Toast",
                "order": 0,
            },
            format="json",
        )
        self.assertEqual(create.status_code, 201)
        item_id = create.data["id"]
        self.assertEqual(create.data["ship_name"], self.ship.name)

        listing = self.client.get(
            "/api/staff/food-menu-items/", {"page_size": 200}
        )
        self.assertEqual(listing.status_code, 200)

        update = self.client.patch(
            f"/api/staff/food-menu-items/{item_id}/",
            {"is_active": False},
            format="json",
        )
        self.assertEqual(update.status_code, 200)
        self.assertFalse(update.data["is_active"])

        delete = self.client.delete(f"/api/staff/food-menu-items/{item_id}/")
        self.assertEqual(delete.status_code, 204)
        self.assertFalse(FoodMenuItem.objects.filter(id=item_id).exists())


class StaffOverviewTests(StaffApiTestCase):
    def test_overview_stats(self):
        booking = self.make_booking()
        Payment.objects.create(
            booking=booking,
            amount=Decimal("2000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
        )
        self.auth()
        response = self.client.get("/api/staff/overview/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["active_bookings"], 1)
        self.assertEqual(response.data["total_collected"], Decimal("2000.00"))
        self.assertEqual(response.data["total_due"], Decimal("7500.00"))
        self.assertTrue(len(response.data["packages"]) >= 1)

    def test_overview_empty_state_has_no_div_by_zero(self):
        self.auth()
        response = self.client.get("/api/staff/overview/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["collection_rate"], Decimal("0.0"))
        self.assertEqual(response.data["total_revenue_expected"], Decimal("0.00"))
        for pkg in response.data["packages"]:
            self.assertEqual(pkg["occupancy_pct"], Decimal("0.0"))

    def test_overview_collection_rate_and_expected_revenue(self):
        booking = self.make_booking()
        Payment.objects.create(
            booking=booking,
            amount=Decimal("2000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
        )
        self.auth()
        response = self.client.get("/api/staff/overview/")
        # total=9500, paid=2000, due=7500 -> expected=9500, rate=2000/9500*100
        self.assertEqual(response.data["total_revenue_expected"], Decimal("9500.00"))
        self.assertEqual(response.data["collection_rate"], Decimal("21.1"))

    def test_overview_bookings_by_status_zero_fills_all_statuses(self):
        self.make_booking()  # pending, no payment
        self.auth()
        response = self.client.get("/api/staff/overview/")
        by_status = response.data["bookings_by_status"]
        self.assertEqual(set(by_status.keys()), {c[0] for c in Booking.Status.choices})
        self.assertEqual(by_status[Booking.Status.PENDING], 1)
        self.assertEqual(by_status[Booking.Status.CANCELLED], 0)
        self.assertEqual(response.data["pending_payment_bookings"], 1)

    def test_overview_recent_bookings_and_payments(self):
        from django.utils import timezone

        booking = self.make_booking(room=self.room_2p, adult_count=1)
        Payment.objects.create(
            booking=booking,
            amount=Decimal("1000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        self.auth()
        response = self.client.get("/api/staff/overview/")
        self.assertEqual(len(response.data["recent_bookings"]), 1)
        self.assertEqual(
            response.data["recent_bookings"][0]["booking_code"], booking.booking_code
        )
        self.assertEqual(len(response.data["recent_payments"]), 1)
        self.assertEqual(
            response.data["recent_payments"][0]["booking_code"], booking.booking_code
        )

    def test_overview_by_ship_breakdown(self):
        from apps.bookings.test_api import build_fixtures

        (_, _, _, _, room_4p_2, package_2) = build_fixtures(ship_name="Second Ship")
        booking = self.make_booking()  # on cls.ship / cls.package
        Payment.objects.create(
            booking=booking,
            amount=Decimal("2000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
        )
        create_booking(
            package_2,
            rooms=[{"room": room_4p_2, "adult_count": 1, "kid_details": []}],
            customer_name="Karim",
            phone="01800000000",
            email="karim@example.com",
        )

        self.auth()
        response = self.client.get("/api/staff/overview/")
        by_ship = {row["ship_name"]: row for row in response.data["by_ship"]}
        # A real "MV Alaska" ship is seeded by migration 0004 in every test DB,
        # so other ships may also appear — just assert our two are present and correct.
        self.assertIn(self.ship.name, by_ship)
        self.assertIn("Second Ship", by_ship)
        self.assertEqual(by_ship[self.ship.name]["active_bookings"], 1)
        self.assertEqual(
            by_ship[self.ship.name]["paid_total"], Decimal("2000.00")
        )
        self.assertEqual(by_ship["Second Ship"]["active_bookings"], 1)
        self.assertEqual(by_ship["Second Ship"]["paid_total"], Decimal("0.00"))


class StaffLoginThrottleTests(APITestCase):
    """Brute-force guard on the staff login endpoint (Phase 8a, F2).

    Deliberately does NOT use ThrottlelessTestMixin — the whole point is to let
    the real "login": "5/min" rate apply. The throttle counter lives in the
    cache, so clear it before and after so this class can't leak 429s into
    other tests (or inherit them)."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="staffer", password="pass12345", is_staff=True
        )

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def tearDown(self):
        from django.core.cache import cache

        cache.clear()

    def test_sixth_login_attempt_within_a_minute_is_throttled(self):
        # Wrong password on purpose: throttling must count *attempts*, so a
        # brute-forcer can't keep guessing. The first five are answered
        # normally (401 here), the sixth is refused with 429.
        for i in range(5):
            response = self.client.post(
                "/api/staff/login/",
                {"username": "staffer", "password": "wrong-guess"},
            )
            self.assertNotEqual(
                response.status_code, 429, f"attempt {i + 1} should not be throttled"
            )
        sixth = self.client.post(
            "/api/staff/login/",
            {"username": "staffer", "password": "wrong-guess"},
        )
        self.assertEqual(sixth.status_code, 429)

    def test_correct_credentials_also_throttled_after_the_limit(self):
        # The limit is on the endpoint, not on failures — five attempts spent
        # guessing means even a now-correct 6th try is refused until the window
        # resets. This is what stops an attacker who lands the password on
        # guess six from immediately using it.
        for _ in range(5):
            self.client.post(
                "/api/staff/login/",
                {"username": "staffer", "password": "wrong-guess"},
            )
        blocked = self.client.post(
            "/api/staff/login/",
            {"username": "staffer", "password": "pass12345"},
        )
        self.assertEqual(blocked.status_code, 429)


class StaffRoomImageTests(StaffApiTestCase):
    """Room gallery management from the dashboard (Room Photos tab)."""

    # 1x1 px valid GIF — enough for ImageField validation.
    TINY_GIF = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
        b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
        b"\x01\x00;"
    )

    def upload(self, room=None, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile

        payload = {
            "room": (room or self.room_2p).pk,
            "image": SimpleUploadedFile("r.gif", self.TINY_GIF, "image/gif"),
            **extra,
        }
        return self.client.post("/api/staff/room-images/", payload, format="multipart")

    def test_anonymous_and_non_staff_rejected(self):
        response = self.client.get("/api/staff/room-images/")
        self.assertEqual(response.status_code, 401)
        response = self.upload()
        self.assertEqual(response.status_code, 401)

    def test_upload_list_update_delete_cycle(self):
        self.auth()

        created = self.upload(caption="Sea view", sort_order=1)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["caption"], "Sea view")
        self.assertEqual(created.data["room_number"], self.room_2p.room_number)
        self.assertTrue(created.data["image_url"])
        self.assertNotIn("image", created.data)  # upload-only field

        other = self.upload(room=self.room_4p)
        self.assertEqual(other.status_code, 201)

        # List filters by room.
        listed = self.client.get(f"/api/staff/room-images/?room={self.room_2p.pk}")
        self.assertEqual([i["id"] for i in listed.data], [created.data["id"]])

        # PATCH edits caption/sort_order but never moves the photo to another
        # room (silently ignored, like a read-only field).
        patched = self.client.patch(
            f"/api/staff/room-images/{created.data['id']}/",
            {"caption": "Window", "sort_order": 5, "room": self.room_4p.pk},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.data["caption"], "Window")
        self.assertEqual(patched.data["room"], self.room_2p.pk)

        deleted = self.client.delete(f"/api/staff/room-images/{created.data['id']}/")
        self.assertEqual(deleted.status_code, 204)
        listed = self.client.get(f"/api/staff/room-images/?room={self.room_2p.pk}")
        self.assertEqual(listed.data, [])

    def test_oversized_upload_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.auth()
        # Pad a valid GIF past the 10 MB ceiling — content-valid, size-invalid.
        big = SimpleUploadedFile(
            "big.gif", self.TINY_GIF + b"\x00" * (10 * 1024 * 1024), "image/gif"
        )
        response = self.client.post(
            "/api/staff/room-images/",
            {"room": self.room_2p.pk, "image": big},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("compress", str(response.data["image"][0]))

    def test_non_image_upload_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.auth()
        response = self.client.post(
            "/api/staff/room-images/",
            {
                "room": self.room_2p.pk,
                "image": SimpleUploadedFile("evil.txt", b"not an image", "text/plain"),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_uploaded_image_appears_in_public_room_map(self):
        self.auth()
        created = self.upload(caption="Balcony")
        self.assertEqual(created.status_code, 201)

        # Public, unauthenticated endpoint the customer site reads.
        self.client.credentials()
        response = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 200)
        by_room = {r["room_number"]: r for r in response.json()}
        images = by_room[self.room_2p.room_number]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["caption"], "Balcony")
        self.assertTrue(images[0]["image"])
