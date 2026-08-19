from rest_framework.test import APITestCase

from apps.testing import ThrottlelessTestMixin

from .models import FoodMenuItem, Ship


class ShipLayoutApiTests(ThrottlelessTestMixin, APITestCase):
    """Runs against the real MV Alaska layout created by the seed migration
    (ships.0004), which also applies to the test database."""

    def setUp(self):
        self.ship = Ship.objects.get(name="MV Alaska")

    def test_layout_groups_31_rooms_by_floor(self):
        response = self.client.get(f"/api/ships/{self.ship.id}/layout/")
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(data["name"], "MV Alaska")
        self.assertEqual(data["total_rooms"], 31)
        floors = {f["floor_number"]: f["rooms"] for f in data["floors"]}
        self.assertEqual(set(floors.keys()), {1, 2})
        self.assertEqual(len(floors[1]), 17)
        self.assertEqual(len(floors[2]), 14)
        room_302 = next(r for r in floors[2] if r["room_number"] == "302")
        self.assertEqual(room_302["room_type"]["name"], "3-Person Room")
        self.assertEqual(room_302["room_type"]["max_adults"], 3)

    def test_room_fields_are_public_only(self):
        response = self.client.get(f"/api/ships/{self.ship.id}/layout/")
        room = response.data["floors"][0]["rooms"][0]
        self.assertEqual(
            set(room.keys()),
            {"id", "room_number", "floor_number", "room_type", "images"},
        )

    def test_inactive_ship_hidden_from_list(self):
        Ship.objects.create(name="Retired Ship", status=Ship.Status.INACTIVE)
        response = self.client.get("/api/ships/")
        names = [s["name"] for s in response.data]
        self.assertIn("MV Alaska", names)
        self.assertNotIn("Retired Ship", names)


class ShipFoodMenuApiTests(ThrottlelessTestMixin, APITestCase):
    """Runs against the real MV Alaska menu created by the seed migration
    (ships.0006), which also applies to the test database."""

    def setUp(self):
        self.ship = Ship.objects.get(name="MV Alaska")

    def test_food_menu_groups_by_day_then_meal_type(self):
        response = self.client.get(f"/api/ships/{self.ship.id}/food-menu/")
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(data["name"], "MV Alaska")
        self.assertIn("Chef will select", data["note"])
        days = {d["day"]: d for d in data["days"]}
        self.assertEqual(set(days.keys()), {"day_1", "day_2", "day_3"})

        # Days 1 and 2 run Breakfast → Snacks → Lunch → Evening Snacks → Dinner,
        # and the serializer emits the meals in that serving order.
        day1_order = [m["meal_type"] for m in days["day_1"]["meals"]]
        self.assertEqual(
            day1_order,
            ["breakfast", "snacks", "lunch", "evening_snacks", "dinner"],
        )
        day1_meals = {m["meal_type"]: m["items"] for m in days["day_1"]["meals"]}
        self.assertIn("Bread", day1_meals["breakfast"])
        # The two snack sittings are distinct buckets, not one merged group.
        self.assertIn("Fruits Cake", day1_meals["snacks"])
        self.assertEqual(
            day1_meals["evening_snacks"],
            ["Noodles/Vegetable Pakura", "Vegetable Roll"],
        )

        day2_meals = {m["meal_type"]: m["items"] for m in days["day_2"]["meals"]}
        self.assertEqual(
            day2_meals["evening_snacks"],
            ["Soup", "French Fry", "Vegetable Pakura"],
        )

        day3_meals = {m["meal_type"]: m["items"] for m in days["day_3"]["meals"]}
        self.assertNotIn("dinner", day3_meals)  # tour ends before Day-3 dinner
        # Day 3 ends after lunch — no second snack sitting.
        self.assertNotIn("evening_snacks", day3_meals)

    def test_food_menu_excludes_inactive_items(self):
        item = FoodMenuItem.objects.filter(ship=self.ship, day="day_1").first()
        item.is_active = False
        item.save()
        response = self.client.get(f"/api/ships/{self.ship.id}/food-menu/")
        day1 = next(d for d in response.data["days"] if d["day"] == "day_1")
        all_items = [name for meal in day1["meals"] for name in meal["items"]]
        self.assertNotIn(item.name, all_items)
