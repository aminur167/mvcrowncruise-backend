"""Foreign-national surcharge + passport capture.

Covers the four things that can silently cost money or leak data:
pricing arithmetic, backend-enforced validation (the API must not be
bypassable), passport exposure on the anonymous endpoint, and — the one that
breaks live data — invoices of bookings priced BEFORE this feature existed.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.packages.models import ForeignerSurcharge, Package
from apps.testing import ThrottlelessTestMixin

from .guests import clean_foreign_guests, mask_passport, normalise_passport
from .models import Booking, BookingRoom
from .pricing import calculate_total, restore_breakdown, snapshot_breakdown
from .tests import BookingBaseTestCase

ADULT_SURCHARGE = Decimal("3000.00")
KID_SURCHARGE = Decimal("1500.00")


def guest(passport="A1234567", guest_type="adult", **extra):
    return {"guest_type": guest_type, "passport_number": passport, **extra}


def set_surcharge(adult=ADULT_SURCHARGE, kid=KID_SURCHARGE):
    """Set the one global policy row — the surcharge is not per-package."""
    surcharge = ForeignerSurcharge.get_solo()
    surcharge.adult_amount = adult
    surcharge.kid_amount = kid
    surcharge.save()
    return surcharge


class ForeignSurchargePricingTests(BookingBaseTestCase):
    def setUp(self):
        set_surcharge()

    def test_surcharge_is_per_person_on_top_of_the_ordinary_fare(self):
        """A foreign adult pays the adult fare AND the surcharge — the
        surcharge is additive, never a replacement fare."""
        local = calculate_total(self.type_4p, self.package, 2, [])
        mixed = calculate_total(
            self.type_4p, self.package, 2, [], foreign_adults=1, foreign_kids=0
        )
        self.assertEqual(mixed - local, ADULT_SURCHARGE)

    def test_foreign_kid_is_surcharged_even_on_the_free_age_tier(self):
        """Age 2 is free under KidPricingRule, but a foreign toddler still
        carries the foreigner charge — the two rules are independent."""
        local = calculate_total(self.type_4p, self.package, 1, [2])
        foreign = calculate_total(
            self.type_4p, self.package, 1, [2], foreign_kids=1
        )
        self.assertEqual(foreign - local, KID_SURCHARGE)

    def test_zero_surcharge_package_prices_exactly_as_before(self):
        """The default (0.00) must be a no-op even when guests are listed —
        otherwise every existing sailing silently re-prices."""
        set_surcharge(adult=Decimal("0.00"), kid=Decimal("0.00"))
        self.assertEqual(
            calculate_total(self.type_4p, self.package, 2, [5], foreign_adults=2),
            calculate_total(self.type_4p, self.package, 2, [5]),
        )

    def test_booking_total_includes_the_surcharge(self):
        booking = self.make_booking(
            rooms=[
                {
                    "room": self.room_4p,
                    "adult_count": 2,
                    "kid_details": [{"age": 5}],
                    "foreign_guests": [
                        guest("A1234567"),
                        guest("B7654321", "kid"),
                    ],
                }
            ]
        )
        expected = (
            Decimal("3500.00")               # room base
            + 2 * Decimal("3000.00")         # adults
            + Decimal("1500.00")             # kid age 5, fixed tier
            + ADULT_SURCHARGE                # 1 foreign adult
            + KID_SURCHARGE                  # 1 foreign kid
        )
        self.assertEqual(booking.total_amount, expected)

    def test_snapshot_carries_the_rates_so_the_invoice_survives_a_rate_change(self):
        """The invoice itemises from the frozen snapshot. If it read today's
        policy row, an admin editing the surcharge would rewrite what a paid
        customer was charged — and now that the rate is GLOBAL, one edit would
        do that to every past booking at once."""
        booking = self.make_booking(
            rooms=[
                {
                    "room": self.room_2p,
                    "adult_count": 1,
                    "foreign_guests": [guest()],
                }
            ]
        )
        snap = booking.rooms.get().price_snapshot
        self.assertEqual(snap["foreign_adult_count"], 1)
        self.assertEqual(Decimal(snap["foreigner_adult_surcharge"]), ADULT_SURCHARGE)

        set_surcharge(adult=Decimal("9999.00"))
        restored = restore_breakdown(booking.rooms.get().price_snapshot)
        self.assertEqual(restored["foreigner_adult_surcharge"], ADULT_SURCHARGE)


class LegacySnapshotTests(BookingBaseTestCase):
    """Bookings priced before this feature have snapshots with no foreigner
    keys at all, and their invoices are still re-rendered on demand."""

    def test_restore_breakdown_tolerates_a_pre_feature_snapshot(self):
        legacy = {
            "room_base": "2000.00",
            "adult_price": "3000.00",
            "adult_count": 2,
            "adults_subtotal": "6000.00",
            "kids": [],
            "kids_subtotal": "0.00",
            "total": "8000.00",
        }
        restored = restore_breakdown(legacy)
        self.assertEqual(restored["foreign_adult_count"], 0)
        self.assertEqual(restored["foreign_kid_count"], 0)
        self.assertEqual(restored["foreigner_subtotal"], Decimal("0.00"))
        # The money truth is untouched — no phantom charge appears.
        self.assertEqual(restored["total"], Decimal("8000.00"))

    def test_snapshot_round_trip_is_lossless(self):
        booking = self.make_booking(
            rooms=[
                {
                    "room": self.room_2p,
                    "adult_count": 2,
                    "foreign_guests": [guest()],
                }
            ]
        )
        snap = booking.rooms.get().price_snapshot
        restored = restore_breakdown(snap)
        self.assertEqual(
            snapshot_breakdown(restored, room_number=restored["room_number"]), snap
        )


class GuestListValidationTests(BookingBaseTestCase):
    def clean(self, raw, adults=2, kids=1):
        return clean_foreign_guests(raw, adult_count=adults, kid_count=kids)

    def test_passport_is_required(self):
        with self.assertRaisesMessage(ValidationError, "passport number is required"):
            self.clean([{"guest_type": "adult"}])

    def test_other_fields_are_optional(self):
        """Client policy: passport is mandatory, everything else is a bonus."""
        self.assertEqual(
            self.clean([guest()]),
            [{"guest_type": "adult", "passport_number": "A1234567"}],
        )

    def test_optional_fields_are_kept_when_given(self):
        cleaned = self.clean(
            [
                guest(
                    full_name="Jane Doe",
                    nationality="us",
                    passport_expiry="2099-01-01",
                )
            ]
        )
        self.assertEqual(cleaned[0]["full_name"], "Jane Doe")
        self.assertEqual(cleaned[0]["nationality"], "US")
        self.assertEqual(cleaned[0]["passport_expiry"], "2099-01-01")

    def test_passport_is_normalised(self):
        self.assertEqual(normalise_passport("a 123-4567"), "A1234567")
        self.assertEqual(self.clean([guest("a 123-4567")])[0]["passport_number"],
                         "A1234567")

    def test_duplicate_passport_in_one_room_is_rejected(self):
        """Normalisation must not be a way to bill one person twice."""
        with self.assertRaisesMessage(ValidationError, "listed twice"):
            self.clean([guest("A1234567"), guest("a-123-4567")])

    def test_foreign_adults_cannot_exceed_the_rooms_adults(self):
        with self.assertRaisesMessage(ValidationError, "only 1 adult"):
            self.clean([guest("A1111111"), guest("B2222222")], adults=1)

    def test_foreign_kids_cannot_exceed_the_rooms_kids(self):
        with self.assertRaisesMessage(ValidationError, "only 0 child"):
            self.clean([guest("A1111111", "kid")], kids=0)

    def test_bad_country_code_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, "not a valid"):
            self.clean([guest(nationality="XX")])

    def test_bangladeshi_is_not_a_foreign_guest(self):
        with self.assertRaisesMessage(ValidationError, "not foreign guests"):
            self.clean([guest(nationality="BD")])

    def test_expired_passport_is_rejected(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with self.assertRaisesMessage(ValidationError, "cannot be used to board"):
            self.clean([guest(passport_expiry=yesterday)])

    def test_malformed_passport_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, "5-20 letters and digits"):
            self.clean([guest("A!@#")])

    def test_model_clean_enforces_the_rules(self):
        """The un-bypassable guard: the ORM path (admin, shell, future code)
        never reaches the serializer."""
        br = BookingRoom(
            booking=self.make_booking(room=self.room_4p, adult_count=1),
            package=self.package,
            room=self.room_2p,
            adult_count=1,
            foreign_guests=[{"guest_type": "adult"}],  # no passport
        )
        with self.assertRaises(ValidationError) as ctx:
            br.full_clean()
        self.assertIn("foreign_guests", ctx.exception.message_dict)

    def test_mask_passport_keeps_only_the_last_four(self):
        self.assertEqual(mask_passport("A1234567"), "****4567")
        self.assertEqual(mask_passport("AB12"), "****")


class ForeignGuestAPITests(ThrottlelessTestMixin, APITestCase):
    """The API is the surface a client could attack — every rule above must
    hold over HTTP, and no full passport may leave the anonymous endpoint."""

    @classmethod
    def setUpTestData(cls):
        from .test_api import build_fixtures

        (
            cls.ship,
            cls.type_2p,
            cls.type_4p,
            cls.room_2p,
            cls.room_4p,
            cls.package,
        ) = build_fixtures("FN Test Ship")
        set_surcharge()

    def payload(self, foreign_guests=None, **extra):
        room = {"room_id": self.room_4p.id, "adult_count": 2}
        if foreign_guests is not None:
            room["foreign_guests"] = foreign_guests
        return {
            "package_id": self.package.id,
            "rooms": [room],
            "customer_name": "Rahim Uddin",
            "phone": "01700000000",
            "email": "rahim@example.com",
            **extra,
        }

    def test_quote_prices_the_surcharge(self):
        without = self.client.post(
            "/api/bookings/quote/",
            {"package_id": self.package.id,
             "rooms": [{"room_id": self.room_4p.id, "adult_count": 2}]},
            format="json",
        )
        with_fn = self.client.post(
            "/api/bookings/quote/",
            {"package_id": self.package.id,
             "rooms": [{"room_id": self.room_4p.id, "adult_count": 2,
                        "foreign_guests": [guest()]}]},
            format="json",
        )
        self.assertEqual(without.status_code, 200)
        self.assertEqual(with_fn.status_code, 200)
        delta = Decimal(with_fn.data["grand_total"]) - Decimal(
            without.data["grand_total"]
        )
        self.assertEqual(delta, ADULT_SURCHARGE)

    def test_create_persists_guests_and_charges_the_surcharge(self):
        response = self.client.post(
            "/api/bookings/", self.payload([guest(full_name="Jane Doe")]),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(booking_code=response.data["booking_code"])
        room = booking.rooms.get()
        self.assertEqual(len(room.foreign_guests), 1)
        self.assertEqual(room.foreign_guests[0]["passport_number"], "A1234567")
        self.assertEqual(
            booking.total_amount,
            Decimal("3500.00") + 2 * Decimal("3000.00") + ADULT_SURCHARGE,
        )

    def test_anonymous_endpoint_never_returns_a_full_passport(self):
        created = self.client.post(
            "/api/bookings/", self.payload([guest()]), format="json"
        )
        code = created.data["booking_code"]
        detail = self.client.get(f"/api/bookings/{code}/")
        returned = detail.data["rooms"][0]["foreign_guests"][0]["passport_number"]
        self.assertEqual(returned, "****4567")
        # Belt and braces: the raw number must not appear anywhere in the body.
        self.assertNotIn("A1234567", str(detail.data))

    def test_api_rejects_more_foreign_adults_than_the_room_holds(self):
        response = self.client.post(
            "/api/bookings/",
            self.payload([guest("A1111111"), guest("B2222222"), guest("C3333333")],),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_api_rejects_a_missing_passport(self):
        response = self.client.post(
            "/api/bookings/", self.payload([{"guest_type": "adult"}]), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_same_passport_cannot_appear_in_two_cabins_of_one_booking(self):
        """One person, two cabins = two surcharges billed for one guest and a
        duplicated manifest line."""
        response = self.client.post(
            "/api/bookings/",
            {
                "package_id": self.package.id,
                "rooms": [
                    {"room_id": self.room_4p.id, "adult_count": 1,
                     "foreign_guests": [guest("A1234567")]},
                    {"room_id": self.room_2p.id, "adult_count": 1,
                     "foreign_guests": [guest("a 123 4567")]},
                ],
                "customer_name": "Rahim Uddin",
                "phone": "01700000000",
                "email": "rahim@example.com",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)

    def test_booking_without_foreign_guests_is_unaffected(self):
        response = self.client.post("/api/bookings/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(booking_code=response.data["booking_code"])
        self.assertEqual(booking.rooms.get().foreign_guests, [])
        self.assertEqual(
            booking.total_amount, Decimal("3500.00") + 2 * Decimal("3000.00")
        )

    def test_client_cannot_dictate_the_surcharge_amount(self):
        """Amounts are never trusted from the client — an injected price must
        be ignored, not honoured."""
        payload = self.payload([guest()])
        payload["rooms"][0]["room_subtotal"] = "1.00"
        payload["total_amount"] = "1.00"
        response = self.client.post("/api/bookings/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        booking = Booking.objects.get(booking_code=response.data["booking_code"])
        self.assertEqual(
            booking.total_amount,
            Decimal("3500.00") + 2 * Decimal("3000.00") + ADULT_SURCHARGE,
        )


class DocumentTests(BookingBaseTestCase):
    """The PDFs must render for both a foreign booking and a legacy one."""

    def setUp(self):
        set_surcharge(kid=Decimal("0.00"))

    def test_invoice_and_guide_report_render_with_foreign_guests(self):
        from .invoices import generate_invoice_pdf
        from .models import Invoice
        from .reports import generate_guide_report_pdf

        booking = self.make_booking(
            rooms=[
                {
                    "room": self.room_2p,
                    "adult_count": 2,
                    "foreign_guests": [
                        guest(full_name="Jane Doe", nationality="US")
                    ],
                }
            ]
        )
        invoice = Invoice.objects.create(
            booking=booking,
            total_amount=booking.total_amount,
            paid_amount=booking.paid_amount,
            due_amount=booking.due_amount,
            booking_status=booking.status,
        )
        pdf = generate_invoice_pdf(invoice)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

        report = generate_guide_report_pdf(self.package, scope="all")
        self.assertTrue(report.startswith(b"%PDF"))
        self.assertGreater(len(report), 1000)

    def test_invoice_renders_for_a_pre_feature_snapshot(self):
        """A booking whose snapshot predates the feature must still invoice —
        this is the regression that would hit every existing customer."""
        from .invoices import generate_invoice_pdf
        from .models import Invoice

        booking = self.make_booking(room=self.room_2p, adult_count=2)
        room = booking.rooms.get()
        room.price_snapshot = {
            k: v
            for k, v in room.price_snapshot.items()
            if not k.startswith("foreign")
        }
        room.save(update_fields=["price_snapshot"])
        invoice = Invoice.objects.create(
            booking=booking,
            total_amount=booking.total_amount,
            paid_amount=Decimal("0.00"),
            due_amount=booking.total_amount,
            booking_status=booking.status,
        )
        self.assertTrue(generate_invoice_pdf(invoice).startswith(b"%PDF"))


class SurchargePolicyTests(BookingBaseTestCase):
    """The surcharge is ONE global row, not a per-package price."""

    def test_negative_surcharge_is_rejected(self):
        surcharge = ForeignerSurcharge.get_solo()
        surcharge.adult_amount = Decimal("-100.00")
        with self.assertRaises(ValidationError):
            surcharge.clean()

    def test_defaults_to_zero_on_a_fresh_database(self):
        """Pricing must work before any admin has visited the settings page,
        and 0.00 prices exactly as the system did before the surcharge
        existed."""
        surcharge = ForeignerSurcharge.get_solo()
        self.assertEqual(surcharge.adult_amount, Decimal("0.00"))
        self.assertEqual(surcharge.kid_amount, Decimal("0.00"))

    def test_only_ever_one_row(self):
        """Two rows would make "the" surcharge ambiguous — save() pins the pk."""
        set_surcharge()
        ForeignerSurcharge(adult_amount=Decimal("50.00")).save()
        self.assertEqual(ForeignerSurcharge.objects.count(), 1)
        self.assertEqual(
            ForeignerSurcharge.get_solo().adult_amount, Decimal("50.00")
        )

    def test_it_cannot_be_deleted(self):
        """There is no fallback policy to read if the row disappears."""
        with self.assertRaises(ValidationError):
            ForeignerSurcharge.get_solo().delete()

    def test_one_rate_applies_to_every_package(self):
        """The point of the move: staff set it once, not per sailing."""
        set_surcharge()
        other = Package.objects.create(
            ship=self.ship,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 3),
            adult_price=Decimal("3000.00"),
            status=Package.Status.OPEN,
        )
        for package in (self.package, other):
            self.assertEqual(
                calculate_total(self.type_2p, package, 1, [], foreign_adults=1)
                - calculate_total(self.type_2p, package, 1, []),
                ADULT_SURCHARGE,
            )

    def test_changing_it_does_not_touch_an_existing_booking(self):
        """A global rate means one edit could otherwise re-price every booking
        ever made. The frozen snapshot and total are what prevent that."""
        set_surcharge()
        booking = self.make_booking(
            rooms=[
                {"room": self.room_2p, "adult_count": 1, "foreign_guests": [guest()]}
            ]
        )
        before = booking.total_amount
        set_surcharge(adult=Decimal("9999.00"))
        booking.refresh_from_db()
        booking.reprice()
        self.assertEqual(booking.total_amount, before)
