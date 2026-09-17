"""Package offers — a sailing sold at a reduced price for a while.

The discount is applied inside price_breakdown(), the one function every
pricing path goes through, so these tests are mostly about the two things that
can go wrong with money: the reduction landing somewhere the customer is not
actually charged from, and a past offer following a booking that was priced
under it (or a current one rewriting a booking that was not).
"""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from apps.bookings.models import Booking, Payment
from apps.bookings.pricing import price_breakdown, restore_breakdown
from apps.bookings.test_api import build_fixtures
from apps.packages.models import OfferType
from apps.testing import ThrottlelessTestMixin, create_booking


class OfferPricingTests(ThrottlelessTestMixin, APITestCase):
    """A 2-person cabin: 2000 base + 1 adult at 3000 = 5000 before any offer."""

    def setUp(self):
        _, self.type_2p, _, self.room, _, self.package = build_fixtures(
            ship_name="Offer Ship"
        )

    def price(self, adults=1, kids=()):
        return price_breakdown(self.type_2p, self.package, adults, list(kids))

    def set_offer(self, kind, value, label="Eid Offer", ends_at=None):
        self.package.discount_type = kind
        self.package.discount_value = Decimal(value)
        self.package.offer_label = label
        self.package.offer_ends_at = ends_at
        self.package.save()

    def test_no_offer_leaves_the_price_exactly_as_it_was(self):
        bd = self.price()
        self.assertEqual(bd["subtotal"], Decimal("5000.00"))
        self.assertEqual(bd["discount"], Decimal("0.00"))
        self.assertEqual(bd["total"], Decimal("5000.00"))

    def test_a_percentage_comes_off_the_whole_cabin(self):
        """Not off the adult fare alone — the base price is part of what the
        customer pays, so an advertised "20% off" that only touched the fare
        would be a smaller discount than the words promise."""
        self.set_offer(OfferType.PERCENT, "20")
        bd = self.price()
        self.assertEqual(bd["discount"], Decimal("1000.00"))
        self.assertEqual(bd["total"], Decimal("4000.00"))

    def test_a_fixed_amount_comes_off_each_cabin(self):
        self.set_offer(OfferType.FIXED, "750")
        bd = self.price()
        self.assertEqual(bd["discount"], Decimal("750.00"))
        self.assertEqual(bd["total"], Decimal("4250.00"))

    def test_a_discount_never_makes_a_cabin_cost_less_than_nothing(self):
        """A flat amount larger than the cabin is a free cabin, not a debt:
        the booking total feeds a non-negative constraint, and money owed to a
        customer is a refund, not a booking."""
        self.set_offer(OfferType.FIXED, "9999")
        bd = self.price()
        self.assertEqual(bd["discount"], Decimal("5000.00"))
        self.assertEqual(bd["total"], Decimal("0.00"))

    def test_the_discount_is_rounded_to_real_money(self):
        """33% of 5000 is 1650 exactly, but 7% of 5000 is not always so tidy —
        round once, here, or the total inherits a repeating fraction."""
        self.set_offer(OfferType.PERCENT, "33.33")
        bd = self.price()
        self.assertEqual(bd["discount"], Decimal("1666.50"))
        self.assertEqual(bd["total"], Decimal("3333.50"))
        self.assertEqual(bd["discount"].as_tuple().exponent, -2)

    def test_an_expired_offer_is_not_an_offer(self):
        self.set_offer(
            OfferType.PERCENT, "20", ends_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(self.price()["total"], Decimal("5000.00"))

    def test_an_offer_still_running_applies(self):
        self.set_offer(
            OfferType.PERCENT, "20", ends_at=timezone.now() + timedelta(days=2)
        )
        self.assertEqual(self.price()["total"], Decimal("4000.00"))

    def test_a_zero_value_is_not_an_offer_whatever_the_type_says(self):
        self.set_offer(OfferType.PERCENT, "0")
        self.assertEqual(self.price()["total"], Decimal("5000.00"))


class OfferSnapshotTests(ThrottlelessTestMixin, APITestCase):
    """What the customer was given is frozen with the booking, like every other
    rate. Offers end; invoices are re-rendered years later."""

    def setUp(self):
        _, self.type_2p, _, self.room, _, self.package = build_fixtures(
            ship_name="Snapshot Ship"
        )
        self.package.discount_type = OfferType.PERCENT
        self.package.discount_value = Decimal("20")
        self.package.offer_label = "Eid Offer"
        self.package.save()

    def test_a_booking_is_charged_the_discounted_total(self):
        booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )
        self.assertEqual(booking.total_amount, Decimal("4000.00"))

    def test_ending_the_offer_does_not_reprice_someone_who_already_booked(self):
        booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )
        # Money in flight is what freezes a booking's price.
        Payment.objects.create(
            booking=booking,
            amount=Decimal("2000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.PENDING,
            transaction_id="FROZEN-1",
        )
        self.package.discount_type = OfferType.NONE
        self.package.discount_value = Decimal("0.00")
        self.package.save()

        booking.reprice()
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.total_amount, Decimal("4000.00"))

    def test_the_snapshot_remembers_the_offer_by_name(self):
        booking = create_booking(
            self.package, [{"room": self.room, "adult_count": 1}]
        )
        snap = restore_breakdown(booking.rooms.first().price_snapshot)
        self.assertEqual(snap["discount"], Decimal("1000.00"))
        self.assertEqual(snap["offer_label"], "Eid Offer")
        self.assertEqual(snap["subtotal"], Decimal("5000.00"))

    def test_a_snapshot_from_before_offers_existed_still_restores(self):
        """Paid bookings are re-rendered on demand — resend, regeneration after
        a redeploy. A KeyError here would 500 every pre-feature invoice."""
        legacy = {
            "room_base": "2000.00",
            "adult_price": "3000.00",
            "adult_count": 1,
            "adults_subtotal": "3000.00",
            "kids": [],
            "kids_subtotal": "0.00",
            "total": "5000.00",
        }
        restored = restore_breakdown(legacy)
        self.assertEqual(restored["discount"], Decimal("0.00"))
        self.assertEqual(restored["subtotal"], Decimal("5000.00"))
        self.assertEqual(restored["offer_label"], "")


class OfferApiTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        _, _, _, self.room, _, self.package = build_fixtures(ship_name="Api Ship")

    def listed(self):
        response = self.client.get("/api/packages/")
        return next(p for p in response.data if p["id"] == self.package.id)

    def test_no_offer_is_published_as_null(self):
        self.assertIsNone(self.listed()["offer"])

    def test_a_live_offer_is_published_for_the_cards(self):
        self.package.discount_type = OfferType.PERCENT
        self.package.discount_value = Decimal("15")
        self.package.offer_label = "Early Bird"
        self.package.save()
        offer = self.listed()["offer"]
        self.assertEqual(offer["label"], "Early Bird")
        self.assertEqual(offer["type"], "percent")
        self.assertEqual(offer["value"], "15.00")

    def test_an_expired_offer_is_not_published(self):
        """The card must not advertise a price the quote will not honour."""
        self.package.discount_type = OfferType.PERCENT
        self.package.discount_value = Decimal("15")
        self.package.offer_ends_at = timezone.now() - timedelta(minutes=1)
        self.package.save()
        self.assertIsNone(self.listed()["offer"])

    def test_the_quote_charges_the_offer_the_card_advertises(self):
        """The one that matters: the card and the money must agree."""
        self.package.discount_type = OfferType.PERCENT
        self.package.discount_value = Decimal("20")
        self.package.save()
        response = self.client.post(
            "/api/bookings/quote/",
            {
                "package_id": self.package.id,
                "rooms": [{"room_id": self.room.id, "adult_count": 1, "kid_ages": []}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        room = response.data["rooms"][0]
        self.assertEqual(room["subtotal"], "5000.00")
        self.assertEqual(room["discount"], "1000.00")
        self.assertEqual(response.data["grand_total"], "4000.00")


class OfferValidationTests(ThrottlelessTestMixin, APITestCase):
    """Staff-facing guards. A bad discount is a cabin sold for the wrong money,
    so it is refused at the edge rather than clamped silently."""

    def setUp(self):
        _, _, _, _, _, self.package = build_fixtures(ship_name="Guard Ship")

    def assert_rejects(self, **fields):
        for key, value in fields.items():
            setattr(self.package, key, value)
        with self.assertRaises(Exception):
            self.package.full_clean()

    def test_a_percentage_over_a_hundred_is_refused(self):
        self.assert_rejects(
            discount_type=OfferType.PERCENT, discount_value=Decimal("150")
        )

    def test_an_offer_with_no_amount_is_refused(self):
        self.assert_rejects(
            discount_type=OfferType.PERCENT, discount_value=Decimal("0")
        )

    def test_an_amount_left_behind_with_no_offer_is_refused(self):
        """Otherwise it lies dormant and reappears as a discount nobody chose
        the moment someone picks a type again."""
        self.assert_rejects(
            discount_type=OfferType.NONE, discount_value=Decimal("500")
        )



class OfferHeadlinePriceTests(ThrottlelessTestMixin, APITestCase):
    """The was-and-now pair the cards strike through. Computed server-side —
    the browser does no arithmetic on money."""

    def setUp(self):
        _, _, _, _, _, self.package = build_fixtures(ship_name="Headline Ship")
        # build_fixtures prices adults at 3000.00.

    def offer(self):
        response = self.client.get("/api/packages/")
        row = next(p for p in response.data if p["id"] == self.package.id)
        return row["offer"]

    def test_a_percentage_publishes_both_prices(self):
        self.package.discount_type = OfferType.PERCENT
        self.package.discount_value = Decimal("20")
        self.package.save()
        offer = self.offer()
        self.assertEqual(offer["adult_price_before"], "3000.00")
        self.assertEqual(offer["adult_price_after"], "2400.00")

    def test_an_awkward_percentage_still_lands_on_real_money(self):
        self.package.discount_type = OfferType.PERCENT
        self.package.discount_value = Decimal("33.33")
        self.package.save()
        self.assertEqual(self.offer()["adult_price_after"], "2000.10")

    def test_a_fixed_offer_publishes_no_after_price(self):
        """A flat amount comes off the cabin, so there is no honest per-adult
        'now' price: a family of four would see a quarter of their real saving
        and a solo traveller four times it."""
        self.package.discount_type = OfferType.FIXED
        self.package.discount_value = Decimal("1500")
        self.package.save()
        offer = self.offer()
        self.assertEqual(offer["adult_price_before"], "3000.00")
        self.assertIsNone(offer["adult_price_after"])
