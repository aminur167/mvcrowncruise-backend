from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.bookings.models import Booking
from apps.ships.models import Room, RoomType, Ship
from apps.testing import ThrottlelessTestMixin, create_booking

from .models import KidPricingRule, Package, PackageRoom

PACKAGE_LIST_FIELDS = {
    "id",
    "ship",
    "start_date",
    "end_date",
    "nights",
    "days",
    "adult_price",
    # Advertised foreign-national rates, so the booking wizard can quote the
    # surcharge before a passport is entered.
    "foreigner_adult_surcharge",
    "foreigner_kid_surcharge",
    "booking_cutoff_datetime",
    "is_bookable",
    "booking_status",
    "marketing_title",
    "marketing_description",
    "hero_image",
    "highlights",
}
ROOM_FIELDS = {
    "id",
    "room_number",
    "floor_number",
    "room_type",
    # This cabin's own photos...
    "images",
    # ...and what to actually SHOW for it, which falls back to the showcase
    # photos of the same cabin type when it has none of its own. preview_source
    # says which, so the UI never implies another cabin's photo is this room.
    "preview_images",
    "preview_source",
    "availability",
}


class PackageApiTestCase(ThrottlelessTestMixin, APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ship = Ship.objects.create(name="Test Ship")
        cls.type_2p, _ = RoomType.objects.get_or_create(
            name="2-Person Room",
            defaults=dict(max_adults=2, max_kids=1, base_price=Decimal("2000.00")),
        )
        cls.room_a = Room.objects.create(
            ship=cls.ship, room_type=cls.type_2p, room_number="T1", floor_number=1
        )
        cls.room_b = Room.objects.create(
            ship=cls.ship, room_type=cls.type_2p, room_number="T2", floor_number=1
        )
        cls.room_c = Room.objects.create(
            ship=cls.ship, room_type=cls.type_2p, room_number="T3", floor_number=2
        )
        cls.package = Package.objects.create(
            ship=cls.ship,
            start_date=date(2099, 1, 10),
            end_date=date(2099, 1, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        for room in (cls.room_a, cls.room_b, cls.room_c):
            PackageRoom.objects.create(package=cls.package, room=room)
        KidPricingRule.objects.create(
            min_age=0, max_age=3, charge_type=KidPricingRule.ChargeType.FREE
        )
        KidPricingRule.objects.create(
            min_age=3,
            max_age=8,
            charge_type=KidPricingRule.ChargeType.FIXED,
            amount=Decimal("1500.00"),
        )

    def make_booking(self, room, status=Booking.Status.PENDING):
        booking = create_booking(
            self.package,
            rooms=[{"room": room, "adult_count": 2, "kid_details": []}],
            customer_name="Rahim",
        )
        if status != Booking.Status.PENDING:
            booking.status = status
            booking.save()
        return booking


class PackageListApiTests(PackageApiTestCase):
    def test_open_upcoming_package_listed_with_public_fields_only(self):
        response = self.client.get("/api/packages/")
        self.assertEqual(response.status_code, 200)
        ids = [p["id"] for p in response.data]
        self.assertIn(self.package.id, ids)
        listed = next(p for p in response.data if p["id"] == self.package.id)
        self.assertEqual(set(listed.keys()), PACKAGE_LIST_FIELDS)
        self.assertEqual(set(listed["ship"].keys()), {"id", "name"})
        self.assertEqual(listed["nights"], 2)
        # days is nights + 1 (a 2-night sailing spans 3 calendar days) — the
        # off-by-one the public card was rendering as "2 Days · 1 Nights".
        self.assertEqual(listed["days"], 3)
        self.assertTrue(listed["is_bookable"])
        self.assertEqual(listed["booking_status"], "open")

    def test_draft_cancelled_and_past_packages_hidden(self):
        draft = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 2, 10),
            end_date=date(2099, 2, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.DRAFT,
        )
        cancelled = Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 3, 10),
            end_date=date(2099, 3, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.CANCELLED,
        )
        past = Package.objects.create(
            ship=self.ship,
            start_date=date(2020, 1, 10),
            end_date=date(2020, 1, 12),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        ids = [p["id"] for p in self.client.get("/api/packages/").data]
        for hidden in (draft, cancelled, past):
            self.assertNotIn(hidden.id, ids)

    def test_cutoff_passed_package_stays_listed_as_closed(self):
        closed = Package.objects.create(
            ship=self.ship,
            start_date=timezone.localdate() + timedelta(days=1),
            end_date=timezone.localdate() + timedelta(days=3),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
            booking_cutoff_datetime=timezone.now() - timedelta(hours=1),
        )
        listed = next(
            p for p in self.client.get("/api/packages/").data if p["id"] == closed.id
        )
        self.assertFalse(listed["is_bookable"])
        self.assertEqual(listed["booking_status"], "closed")

    def test_detail_includes_kid_pricing_rules(self):
        response = self.client.get(f"/api/packages/{self.package.id}/")
        self.assertEqual(response.status_code, 200)
        rules = response.data["kid_pricing_rules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(
            set(rules[0].keys()), {"min_age", "max_age", "charge_type", "amount"}
        )


class PackageRoomsApiTests(PackageApiTestCase):
    def rooms_by_number(self):
        response = self.client.get(f"/api/packages/{self.package.id}/rooms/")
        self.assertEqual(response.status_code, 200)
        return {room["room_number"]: room for room in response.data}

    def test_availability_states(self):
        self.make_booking(self.room_a)
        PackageRoom.objects.filter(package=self.package, room=self.room_b).update(
            is_available=False
        )
        rooms = self.rooms_by_number()
        self.assertEqual(rooms["T1"]["availability"], "booked")
        self.assertEqual(rooms["T2"]["availability"], "unavailable")
        self.assertEqual(rooms["T3"]["availability"], "available")

    def test_cancelled_booking_frees_room(self):
        booking = self.make_booking(self.room_a)
        booking.status = Booking.Status.CANCELLED
        booking.save()
        self.assertEqual(self.rooms_by_number()["T1"]["availability"], "available")

    def test_no_customer_or_booking_data_exposed(self):
        self.make_booking(self.room_a)
        for room in self.rooms_by_number().values():
            self.assertEqual(set(room.keys()), ROOM_FIELDS)

    def test_rooms_endpoint_query_count_is_constant(self):
        # One query for the package, one annotated query for all rooms, one
        # prefetch for all rooms' gallery images, and one lookup of the
        # showcase cabins that unphotographed rooms borrow preview photos from
        # — regardless of room count (N+1 guard).
        with self.assertNumQueries(4):
            self.client.get(f"/api/packages/{self.package.id}/rooms/")


class CalendarApiTests(PackageApiTestCase):
    def test_package_days_highlighted(self):
        response = self.client.get("/api/calendar/?year=2099&month=1")
        self.assertEqual(response.status_code, 200)
        days = [d["date"] for d in response.data["dates"]]
        self.assertEqual(days, ["2099-01-10", "2099-01-11", "2099-01-12"])
        entry = response.data["dates"][0]["packages"][0]
        self.assertEqual(entry["id"], self.package.id)
        self.assertEqual(entry["ship_name"], "Test Ship")
        self.assertTrue(entry["is_bookable"])

    def test_month_boundary_package_appears_in_both_months(self):
        Package.objects.create(
            ship=self.ship,
            start_date=date(2099, 3, 31),
            end_date=date(2099, 4, 2),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        march = self.client.get("/api/calendar/?year=2099&month=3").data
        april = self.client.get("/api/calendar/?year=2099&month=4").data
        self.assertEqual([d["date"] for d in march["dates"]], ["2099-03-31"])
        self.assertEqual(
            [d["date"] for d in april["dates"]], ["2099-04-01", "2099-04-02"]
        )

    def test_invalid_params_rejected(self):
        for query in ("?month=13", "?month=abc", "?year=1800&month=5"):
            self.assertEqual(
                self.client.get(f"/api/calendar/{query}").status_code, 400
            )

    def test_defaults_to_current_month(self):
        response = self.client.get("/api/calendar/")
        self.assertEqual(response.status_code, 200)
        today = timezone.localdate()
        self.assertEqual(response.data["year"], today.year)
        self.assertEqual(response.data["month"], today.month)
