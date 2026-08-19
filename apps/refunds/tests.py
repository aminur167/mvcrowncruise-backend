"""Tests for cancellations and refunds.

Organised around the things that would cost real money if they broke:

- the charge schedule resolving to the wrong tier,
- a refund that exceeds what was paid, or goes negative,
- a quote moving between the screen the customer agreed to and the row we store,
- a booking cancelled (or a payout raised) twice,
- and any of it being reachable without proving who you are.
"""

from datetime import timedelta
from decimal import Decimal
from itertools import count

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.bookings import invoice_access
from apps.bookings.identity import normalize_booking_code
from apps.bookings.models import Booking, Invoice, Payment
from apps.packages.models import KidPricingRule, Package, PackageRoom
from apps.ships.models import Room, RoomType, Ship
from apps.testing import ThrottlelessTestMixin, create_booking

from . import policy, services, tokens
from .models import CancellationRequest, CancellationRule, Refund

User = get_user_model()
ZERO = Decimal("0.00")

_counter = count(1)


def unique(prefix):
    """Ship names are unique and usernames must not collide across test
    classes; the test id is far too long for either column."""
    return f"{prefix}{next(_counter)}"


def build_world(*, ship_name="Refund Ship", starts_in_days=30):
    """A ship, two cabins and a package that departs `starts_in_days` from
    today — the offset is the input every tier test varies."""
    ship = Ship.objects.create(name=ship_name)
    type_2p, _ = RoomType.objects.get_or_create(
        name="2-Person Room",
        defaults=dict(max_adults=2, max_kids=1, base_price=Decimal("2000.00")),
    )
    type_4p, _ = RoomType.objects.get_or_create(
        name="4-Person Room",
        defaults=dict(max_adults=4, max_kids=2, base_price=Decimal("3500.00")),
    )
    room_a = Room.objects.create(ship=ship, room_type=type_2p, room_number="R1")
    room_b = Room.objects.create(ship=ship, room_type=type_4p, room_number="R2")
    start = timezone.localdate() + timedelta(days=starts_in_days)
    package = Package.objects.create(
        ship=ship,
        start_date=start,
        end_date=start + timedelta(days=2),
        adult_price=Decimal("3000.00"),
        status=Package.Status.OPEN,
    )
    PackageRoom.objects.create(package=package, room=room_a)
    PackageRoom.objects.create(package=package, room=room_b)
    for min_age, max_age, ctype, amount in [
        (0, 3, KidPricingRule.ChargeType.FREE, None),
        (3, 8, KidPricingRule.ChargeType.FIXED, Decimal("1500.00")),
        (8, 99, KidPricingRule.ChargeType.FULL_ADULT, None),
    ]:
        KidPricingRule.objects.get_or_create(
            min_age=min_age,
            max_age=max_age,
            defaults={"charge_type": ctype, "amount": amount},
        )
    return ship, package, room_a, room_b


def make_booking(package, room, *, paid=ZERO, adults=2, **fields):
    booking = create_booking(package, [{"room": room, "adult_count": adults}], **fields)
    if paid:
        booking.paid_amount = Decimal(paid)
        booking.save(update_fields=["paid_amount", "due_amount", "updated_at"])
    return booking


# ── The schedule ───────────────────────────────────────────────────────────


class ScheduleResolutionTests(ThrottlelessTestMixin, APITestCase):
    """Which tier applies, and where the money lands."""

    @classmethod
    def setUpTestData(cls):
        cls.ship, cls.package, cls.room_a, cls.room_b = build_world()

    def test_seeded_default_schedule_matches_the_published_table(self):
        tiers = {
            rule.days_before_start: (rule.individual_percent, rule.group_percent)
            for rule in policy.schedule_for(self.ship)
        }
        self.assertEqual(tiers[21], (Decimal("5.00"), Decimal("15.00")))
        self.assertEqual(tiers[7], (Decimal("35.00"), Decimal("25.00")))
        self.assertEqual(tiers[0], (Decimal("100.00"), Decimal("100.00")))

    def test_tier_is_the_largest_boundary_at_or_below_the_days_remaining(self):
        cases = {
            60: Decimal("5.00"),  # far out still lands on the top tier
            21: Decimal("5.00"),
            20: Decimal("15.00"),
            14: Decimal("15.00"),
            13: Decimal("35.00"),
            7: Decimal("35.00"),
            6: Decimal("50.00"),
            3: Decimal("50.00"),
            2: Decimal("75.00"),
            1: Decimal("90.00"),
            0: Decimal("100.00"),
        }
        for days, expected in cases.items():
            with self.subTest(days=days):
                rule = policy.resolve_rule(self.ship, days)
                self.assertEqual(rule.individual_percent, expected)

    def test_group_booking_uses_the_group_column(self):
        rule = policy.resolve_rule(self.ship, 7)
        self.assertEqual(
            rule.percent_for(Booking.BookingType.INDIVIDUAL), Decimal("35.00")
        )
        self.assertEqual(rule.percent_for(Booking.BookingType.GROUP), Decimal("25.00"))

    def test_ship_specific_schedule_replaces_the_default_entirely(self):
        CancellationRule.objects.create(
            ship=self.ship,
            days_before_start=0,
            label="Any time",
            individual_percent=Decimal("10.00"),
            group_percent=Decimal("10.00"),
        )
        schedule = policy.schedule_for(self.ship)
        self.assertEqual(len(schedule), 1)
        self.assertEqual(
            policy.resolve_rule(self.ship, 45).individual_percent, Decimal("10.00")
        )

    def test_no_schedule_refuses_to_quote_rather_than_guessing(self):
        CancellationRule.objects.all().delete()
        booking = make_booking(self.package, self.room_a, paid="5000.00")
        quote = policy.quote_cancellation(booking)
        self.assertFalse(quote.allowed)
        self.assertEqual(quote.block_reason, policy.BLOCK_NO_POLICY)


class ChargeArithmeticTests(ThrottlelessTestMixin, APITestCase):
    def make(self, *, starts_in_days, paid, ship_name):
        ship, package, room_a, _ = build_world(
            ship_name=ship_name, starts_in_days=starts_in_days
        )
        return make_booking(package, room_a, paid=paid)

    def test_full_payment_far_out_charges_five_percent(self):
        booking = self.make(starts_in_days=30, paid="9000.00", ship_name="S1")
        quote = policy.quote_cancellation(booking)
        # total = 2000 base + 2 × 3000 = 8000
        self.assertEqual(quote.total_amount, Decimal("8000.00"))
        self.assertEqual(quote.cancellation_charge, Decimal("400.00"))
        self.assertEqual(quote.refund_amount, Decimal("8600.00"))

    def test_charge_larger_than_the_deposit_never_produces_a_negative_refund(self):
        # 50% deposit paid, cancelling inside 48 hours → 75% charge.
        booking = self.make(starts_in_days=2, paid="4000.00", ship_name="S2")
        quote = policy.quote_cancellation(booking)
        self.assertEqual(quote.cancellation_charge, Decimal("6000.00"))
        self.assertEqual(quote.refund_amount, ZERO)
        self.assertEqual(quote.forfeited_amount, Decimal("4000.00"))
        # The uncovered 2,000 is recorded for reporting — and never billed.
        self.assertEqual(quote.shortfall_amount, Decimal("2000.00"))

    def test_amounts_are_decimal_and_quantised_to_two_places(self):
        booking = self.make(starts_in_days=30, paid="8000.00", ship_name="S3")
        quote = policy.quote_cancellation(booking)
        for amount in (
            quote.cancellation_charge,
            quote.refund_amount,
            quote.forfeited_amount,
        ):
            self.assertIsInstance(amount, Decimal)
            self.assertEqual(amount.as_tuple().exponent, -2)

    def test_unpaid_booking_needs_no_approval(self):
        booking = self.make(starts_in_days=30, paid=ZERO, ship_name="S4")
        quote = policy.quote_cancellation(booking)
        self.assertTrue(quote.allowed)
        self.assertFalse(quote.requires_approval)
        self.assertEqual(quote.refund_amount, ZERO)

    def test_operator_cancellation_refunds_everything_with_no_charge(self):
        booking = self.make(starts_in_days=1, paid="8000.00", ship_name="S5")
        quote = policy.operator_cancellation_quote(booking, note="Bad weather")
        self.assertEqual(quote.cancellation_charge, ZERO)
        self.assertEqual(quote.refund_amount, Decimal("8000.00"))


class WindowTests(ThrottlelessTestMixin, APITestCase):
    """A sailing that has started or finished cannot be cancelled online."""

    def quote_for(self, starts_in_days, ship_name):
        ship, package, room_a, _ = build_world(
            ship_name=ship_name, starts_in_days=starts_in_days
        )
        booking = make_booking(package, room_a, paid="8000.00")
        return policy.quote_cancellation(booking), booking

    def test_upcoming_is_allowed(self):
        quote, _ = self.quote_for(5, "W1")
        self.assertTrue(quote.allowed)

    def test_departure_day_is_in_progress_and_blocked(self):
        quote, _ = self.quote_for(0, "W2")
        self.assertFalse(quote.allowed)
        self.assertEqual(quote.block_reason, policy.BLOCK_IN_PROGRESS)

    def test_mid_sailing_is_blocked(self):
        quote, _ = self.quote_for(-1, "W3")
        self.assertFalse(quote.allowed)
        self.assertEqual(quote.block_reason, policy.BLOCK_IN_PROGRESS)

    def test_sailed_is_blocked(self):
        quote, _ = self.quote_for(-5, "W4")
        self.assertFalse(quote.allowed)
        self.assertEqual(quote.block_reason, policy.BLOCK_SAILED)

    def test_already_cancelled_is_blocked(self):
        quote, booking = self.quote_for(10, "W5")
        booking.status = Booking.Status.CANCELLED
        booking.save()
        self.assertEqual(
            policy.quote_cancellation(booking).block_reason,
            policy.BLOCK_ALREADY_CANCELLED,
        )


# ── The customer-facing flow ───────────────────────────────────────────────


class PublicCancellationFlowTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Flow"), starts_in_days=30
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")
        self.code = self.booking.booking_code

    def preview(self):
        return self.client.get(f"/api/bookings/{self.code}/cancellation-preview/")

    def payload(self, **overrides):
        data = {
            "phone_confirm": self.booking.phone[-4:],
            "reason_code": "plans_changed",
            "refund_method": "bkash",
            "refund_account_name": "Rahim Uddin",
            "refund_account_number": "01712345678",
            "acknowledged_charge": True,
            "quote_token": self.preview().data["quote_token"],
        }
        data.update(overrides)
        return data

    def submit(self, **overrides):
        return self.client.post(
            f"/api/bookings/{self.code}/cancellation-request/",
            self.payload(**overrides),
            format="json",
        )

    def test_preview_quotes_the_charge_and_issues_a_token(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["allowed"])
        self.assertEqual(response.data["cancellation_charge"], "400.00")
        self.assertTrue(response.data["quote_token"])

    def test_preview_never_leaks_the_waived_shortfall_figure(self):
        self.assertNotIn("shortfall_amount", self.preview().data)

    def test_request_creates_a_pending_row_without_touching_the_booking(self):
        response = self.submit()
        self.assertEqual(response.status_code, 201)
        self.booking.refresh_from_db()
        # The booking keeps its status and its cabin until staff decide.
        self.assertEqual(self.booking.status, Booking.Status.PENDING)
        self.assertTrue(self.booking.rooms.filter(is_active=True).exists())
        request = CancellationRequest.objects.get()
        self.assertEqual(request.status, CancellationRequest.Status.PENDING)
        self.assertEqual(request.refund_amount, Decimal("7600.00"))

    def test_amounts_submitted_by_the_client_are_ignored(self):
        self.submit(
            refund_amount="8000.00",
            cancellation_charge="0.00",
            total_amount="999999.00",
        )
        request = CancellationRequest.objects.get()
        self.assertEqual(request.cancellation_charge, Decimal("400.00"))
        self.assertEqual(request.refund_amount, Decimal("7600.00"))

    def test_wrong_phone_digits_are_rejected(self):
        response = self.submit(phone_confirm="9999")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CancellationRequest.objects.exists())

    def test_unacknowledged_charge_is_rejected(self):
        self.assertEqual(self.submit(acknowledged_charge=False).status_code, 400)

    def test_tampered_quote_token_is_rejected(self):
        response = self.submit(quote_token="not-a-real-token")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(CancellationRequest.objects.exists())

    def test_a_token_from_another_booking_is_rejected(self):
        other = make_booking(self.package, self.room_b, paid="8000.00")
        other_quote = policy.quote_cancellation(other)
        response = self.submit(quote_token=tokens.issue(other, other_quote))
        self.assertEqual(response.status_code, 409)

    def test_stale_token_priced_at_a_different_tier_is_refused(self):
        stale = policy.quote_cancellation(self.booking)
        stale.cancellation_charge = Decimal("1.00")
        stale.refund_amount = Decimal("7999.00")
        response = self.submit(quote_token=tokens.issue(self.booking, stale))
        self.assertEqual(response.status_code, 409)
        # The fresh, correct quote comes back so the customer re-confirms.
        self.assertEqual(response.data["quote"]["cancellation_charge"], "400.00")

    def test_bkash_number_must_be_a_real_mobile_number(self):
        response = self.submit(refund_account_number="12345")
        self.assertEqual(response.status_code, 400)
        self.assertIn("refund_account_number", response.data)

    def test_bank_transfer_requires_bank_and_branch(self):
        response = self.submit(
            refund_method="bank_transfer", refund_account_number="1234567890123"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("bank_name", response.data)

    def test_other_reason_requires_a_note(self):
        response = self.submit(reason_code="other")
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason_note", response.data)

    def test_a_second_request_while_one_is_open_is_refused(self):
        self.assertEqual(self.submit().status_code, 201)
        second = self.client.post(
            f"/api/bookings/{self.code}/cancellation-request/",
            self.payload(quote_token=tokens.issue(
                self.booking, policy.quote_cancellation(self.booking)
            )),
            format="json",
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(CancellationRequest.objects.count(), 1)

    def test_unpaid_booking_is_cancelled_immediately(self):
        booking = make_booking(self.package, self.room_b, paid=ZERO)
        quote = policy.quote_cancellation(booking)
        response = self.client.post(
            f"/api/bookings/{booking.booking_code}/cancellation-request/",
            {
                "phone_confirm": booking.phone[-4:],
                "reason_code": "booked_by_mistake",
                "refund_method": "bkash",
                "refund_account_name": "Nobody",
                "refund_account_number": "01712345678",
                "acknowledged_charge": True,
                "quote_token": tokens.issue(booking, quote),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        self.assertFalse(Refund.objects.exists())

    def test_cannot_start_a_payment_while_a_cancellation_is_pending(self):
        self.submit()
        response = self.client.post(
            f"/api/bookings/{self.code}/pay/",
            {"payment_type": Payment.PaymentType.FULL},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_in_progress_departure_cannot_be_cancelled_online(self):
        ship, package, room, _ = build_world(ship_name=unique("Sailing"), starts_in_days=0)
        booking = make_booking(package, room, paid="8000.00")
        preview = self.client.get(
            f"/api/bookings/{booking.booking_code}/cancellation-preview/"
        )
        self.assertFalse(preview.data["allowed"])
        self.assertEqual(preview.data["block_reason"], policy.BLOCK_IN_PROGRESS)
        self.assertIsNone(preview.data["quote_token"])


class BookingCodeAndLookupTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.ship, self.package, self.room_a, _ = build_world(
            ship_name=unique("Lookup")
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")

    def test_normalizer_repairs_how_people_type_a_code(self):
        canonical = self.booking.booking_code
        for typed in (
            canonical.lower(),
            f"  {canonical} ",
            canonical.replace("-", ""),
            canonical.replace("BK-", "").lower(),
        ):
            with self.subTest(typed=typed):
                self.assertEqual(normalize_booking_code(typed), canonical)

    def test_detail_route_accepts_a_lowercase_code(self):
        response = self.client.get(
            f"/api/bookings/{self.booking.booking_code.lower()}/"
        )
        self.assertEqual(response.status_code, 200)

    def test_lookup_needs_both_the_code_and_the_phone_digits(self):
        response = self.client.post(
            "/api/bookings/lookup/",
            {
                "booking_code": self.booking.booking_code.lower(),
                "phone_last4": self.booking.phone[-4:],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["booking_code"], self.booking.booking_code)

    def test_lookup_with_a_wrong_phone_answers_like_an_unknown_code(self):
        """A leaked code alone must not confirm that a booking exists."""
        wrong = self.client.post(
            "/api/bookings/lookup/",
            {"booking_code": self.booking.booking_code, "phone_last4": "1111"},
            format="json",
        )
        unknown = self.client.post(
            "/api/bookings/lookup/",
            {"booking_code": "BK-0000000000000000", "phone_last4": "1111"},
            format="json",
        )
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(wrong.status_code, unknown.status_code)
        self.assertEqual(wrong.data["detail"], unknown.data["detail"])


# ── Staff decisions ────────────────────────────────────────────────────────


class StaffDecisionTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username=unique("staff"), password="pass12345", is_staff=True
        )
        self.client.force_authenticate(user=self.staff)
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Staff"), starts_in_days=30
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")
        self.request = services.create_cancellation_request(
            self.booking,
            reason_code=CancellationRequest.Reason.PLANS_CHANGED,
            refund_method="bkash",
            refund_account_number="01712345678",
            refund_account_name="Rahim",
        )

    def test_approve_cancels_the_booking_and_raises_the_payout(self):
        response = self.client.post(
            f"/api/staff/cancellation-requests/{self.request.pk}/approve/",
            {"note": "ok"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.CANCELLED)
        self.assertFalse(self.booking.rooms.filter(is_active=True).exists())
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, Decimal("7600.00"))
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.reason, Refund.Reason.CUSTOMER_CANCELLATION)
        self.assertTrue(self.booking.refund_required)

    def test_approval_honours_the_frozen_charge_after_the_schedule_changes(self):
        CancellationRule.objects.filter(ship__isnull=True).update(
            individual_percent=Decimal("100.00")
        )
        self.client.post(
            f"/api/staff/cancellation-requests/{self.request.pk}/approve/",
            {},
            format="json",
        )
        self.assertEqual(Refund.objects.get().amount, Decimal("7600.00"))

    def test_money_paid_after_the_quote_increases_the_refund(self):
        """Staff delay must not cost the customer, but nor may it swallow a
        balance payment that landed while the request waited."""
        self.booking.paid_amount = Decimal("8000.00") + Decimal("500.00")
        self.booking.save(update_fields=["paid_amount", "due_amount", "updated_at"])
        self.client.post(
            f"/api/staff/cancellation-requests/{self.request.pk}/approve/",
            {},
            format="json",
        )
        self.assertEqual(Refund.objects.get().amount, Decimal("8100.00"))

    def test_reject_leaves_the_booking_alone_and_needs_a_reason(self):
        blank = self.client.post(
            f"/api/staff/cancellation-requests/{self.request.pk}/reject/",
            {"note": "   "},
            format="json",
        )
        self.assertEqual(blank.status_code, 400)

        ok = self.client.post(
            f"/api/staff/cancellation-requests/{self.request.pk}/reject/",
            {"note": "Called the customer, they are travelling after all."},
            format="json",
        )
        self.assertEqual(ok.status_code, 200)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, Booking.Status.PENDING)
        self.assertTrue(self.booking.rooms.filter(is_active=True).exists())
        self.assertFalse(Refund.objects.exists())

    def test_a_decided_request_cannot_be_decided_again(self):
        url = f"/api/staff/cancellation-requests/{self.request.pk}/approve/"
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 400)
        self.assertEqual(Refund.objects.count(), 1)

    def test_payout_account_is_masked_in_the_queue_and_whole_on_the_detail(self):
        listing = self.client.get("/api/staff/cancellation-requests/")
        row = listing.data["results"][0]
        self.assertNotIn("01712345678", str(row))
        self.assertTrue(row["refund_account_masked"].endswith("5678"))

        detail = self.client.get(
            f"/api/staff/cancellation-requests/{self.request.pk}/"
        )
        self.assertEqual(detail.data["refund_account_number"], "01712345678")

    def test_staff_endpoints_are_closed_to_everyone_else(self):
        self.client.force_authenticate(user=None)
        for url in (
            "/api/staff/cancellation-requests/",
            "/api/staff/refunds/",
            "/api/staff/cancellation-rules/",
        ):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (401, 403))

        customer = User.objects.create_user(username=unique("cust"), password="pass12345")
        self.client.force_authenticate(user=customer)
        self.assertIn(
            self.client.get("/api/staff/refunds/").status_code, (401, 403)
        )


class RefundLedgerTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username=unique("ops"), password="pass12345", is_staff=True
        )
        self.client.force_authenticate(user=self.staff)
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Ledger"), starts_in_days=30
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")

    def raise_refund(self, **overrides):
        data = {
            "booking_code": self.booking.booking_code,
            "reason": Refund.Reason.OVERPAYMENT,
            "amount": "1000.00",
            "note": "Paid twice by mistake.",
        }
        data.update(overrides)
        return self.client.post("/api/staff/refunds/", data, format="json")

    def test_manual_refund_cannot_exceed_what_was_paid(self):
        response = self.raise_refund(amount="9000.00")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Refund.objects.exists())

    def test_two_refunds_cannot_together_exceed_what_was_paid(self):
        self.assertEqual(self.raise_refund(amount="6000.00").status_code, 201)
        self.client.post(
            f"/api/staff/refunds/{Refund.objects.get().pk}/mark-paid/",
            {"method": "bkash", "reference_no": "TRX1"},
            format="json",
        )
        self.assertEqual(self.raise_refund(amount="3000.00").status_code, 400)

    def test_a_cancellation_refund_cannot_be_raised_by_hand(self):
        response = self.raise_refund(reason=Refund.Reason.CUSTOMER_CANCELLATION)
        self.assertEqual(response.status_code, 400)

    def test_goodwill_refund_demands_a_reason(self):
        response = self.raise_refund(reason=Refund.Reason.GOODWILL, note="")
        self.assertEqual(response.status_code, 400)

    def test_marking_paid_requires_a_transaction_reference(self):
        self.raise_refund()
        refund = Refund.objects.get()
        missing = self.client.post(
            f"/api/staff/refunds/{refund.pk}/mark-paid/",
            {"method": "bkash", "reference_no": "  "},
            format="json",
        )
        self.assertEqual(missing.status_code, 400)

    def test_paying_a_refund_records_who_and_clears_the_owed_flag(self):
        self.raise_refund()
        refund = Refund.objects.get()
        self.booking.refresh_from_db()
        self.assertTrue(self.booking.refund_required)

        response = self.client.post(
            f"/api/staff/refunds/{refund.pk}/mark-paid/",
            {"method": "bkash", "reference_no": "TRX-9911"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PAID)
        self.assertEqual(refund.processed_by, self.staff)
        self.assertIsNotNone(refund.paid_at)
        self.assertTrue(refund.status_logs.exists())
        self.booking.refresh_from_db()
        self.assertFalse(self.booking.refund_required)

    def test_a_paid_refund_cannot_be_paid_or_voided_again(self):
        self.raise_refund()
        refund = Refund.objects.get()
        self.client.post(
            f"/api/staff/refunds/{refund.pk}/mark-paid/",
            {"method": "bkash", "reference_no": "TRX-1"},
            format="json",
        )
        again = self.client.post(
            f"/api/staff/refunds/{refund.pk}/mark-paid/",
            {"method": "bkash", "reference_no": "TRX-2"},
            format="json",
        )
        voided = self.client.post(
            f"/api/staff/refunds/{refund.pk}/void/",
            {"note": "oops"},
            format="json",
        )
        self.assertEqual(again.status_code, 400)
        self.assertEqual(voided.status_code, 400)

    def test_refunds_outside_the_claim_window_need_an_override(self):
        ship, package, room, _ = build_world(
            ship_name=unique("Gone"), starts_in_days=-90
        )
        old = make_booking(package, room, paid="8000.00")
        refused = self.client.post(
            "/api/staff/refunds/",
            {
                "booking_code": old.booking_code,
                "reason": Refund.Reason.OVERPAYMENT,
                "amount": "500.00",
                "note": "late claim",
            },
            format="json",
        )
        self.assertEqual(refused.status_code, 400)

        allowed = self.client.post(
            "/api/staff/refunds/",
            {
                "booking_code": old.booking_code,
                "reason": Refund.Reason.OVERPAYMENT,
                "amount": "500.00",
                "note": "Verified against the bank statement.",
                "allow_outside_claim_window": True,
            },
            format="json",
        )
        self.assertEqual(allowed.status_code, 201)

    def test_register_renders_as_pdf_and_as_csv(self):
        self.raise_refund(amount="1500.00")
        pdf = self.client.get("/api/staff/refunds/register/")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertGreater(len(pdf.content), 1000)

        csv_response = self.client.get("/api/staff/refunds/register/?export=csv")
        self.assertEqual(csv_response.status_code, 200)
        body = csv_response.content.decode()
        self.assertIn(self.booking.booking_code, body)
        self.assertIn("1500.00", body)

    def test_register_renders_with_no_rows(self):
        """An empty period is a normal month, not an error."""
        response = self.client.get("/api/staff/refunds/register/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_summary_reports_the_outstanding_liability(self):
        self.raise_refund(amount="1200.00")
        summary = self.client.get("/api/staff/refunds/summary/")
        self.assertEqual(summary.data["liability_count"], 1)
        self.assertEqual(summary.data["liability_total"], "1200.00")


class StaffCancellationAndDepartureTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username=unique("desk"), password="pass12345", is_staff=True
        )
        self.client.force_authenticate(user=self.staff)
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Desk"), starts_in_days=10
        )

    def test_staff_cancel_applies_the_schedule(self):
        booking = make_booking(self.package, self.room_a, paid="8000.00")
        response = self.client.post(
            f"/api/staff/bookings/{booking.pk}/cancel/",
            {"reason_code": "medical", "reason_note": "Hospitalised."},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.CANCELLED)
        # 10 days out → the 7-day tier, 35% of 8,000.
        self.assertEqual(Refund.objects.get().amount, Decimal("5200.00"))

    def test_waiving_the_charge_refunds_everything_and_needs_a_note(self):
        booking = make_booking(self.package, self.room_a, paid="8000.00")
        bare = self.client.post(
            f"/api/staff/bookings/{booking.pk}/cancel/",
            {"reason_code": "medical", "waive_charge": True},
            format="json",
        )
        self.assertEqual(bare.status_code, 400)

        response = self.client.post(
            f"/api/staff/bookings/{booking.pk}/cancel/",
            {
                "reason_code": "medical",
                "waive_charge": True,
                "reason_note": "Bereavement — approved by the manager.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        refund = Refund.objects.get()
        self.assertEqual(refund.amount, Decimal("8000.00"))
        self.assertTrue(refund.policy_snapshot["charge_waived"])

    def test_cancelling_a_departure_previews_before_it_destroys(self):
        make_booking(self.package, self.room_a, paid="8000.00")
        make_booking(self.package, self.room_b, paid="4000.00")

        preview = self.client.post(
            f"/api/staff/packages/{self.package.pk}/cancel-departure/",
            {"reason_note": "Cyclone warning", "dry_run": True},
            format="json",
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.data["bookings"], 2)
        self.assertEqual(preview.data["refund_total"], "12000.00")
        # Nothing has happened yet.
        self.assertFalse(Refund.objects.exists())
        self.package.refresh_from_db()
        self.assertEqual(self.package.status, Package.Status.OPEN)

    def test_destructive_departure_cancel_must_echo_the_package_id(self):
        make_booking(self.package, self.room_a, paid="8000.00")
        response = self.client.post(
            f"/api/staff/packages/{self.package.pk}/cancel-departure/",
            {"reason_note": "Cyclone warning", "dry_run": False},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Refund.objects.exists())

    def test_departure_cancellation_refunds_every_booking_in_full(self):
        a = make_booking(self.package, self.room_a, paid="8000.00")
        b = make_booking(self.package, self.room_b, paid="4000.00")
        response = self.client.post(
            f"/api/staff/packages/{self.package.pk}/cancel-departure/",
            {
                "reason_note": "Engine fault",
                "dry_run": False,
                "confirm_package_id": self.package.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, Booking.Status.CANCELLED)
        self.assertEqual(b.status, Booking.Status.CANCELLED)
        # Involuntary: no charge, whatever the tier would have said.
        amounts = sorted(r.amount for r in Refund.objects.all())
        self.assertEqual(amounts, [Decimal("4000.00"), Decimal("8000.00")])
        self.assertTrue(
            all(r.reason == Refund.Reason.OPERATOR_CANCELLATION for r in Refund.objects.all())
        )
        self.package.refresh_from_db()
        self.assertEqual(self.package.status, Package.Status.CANCELLED)


class NotificationTests(ThrottlelessTestMixin, APITestCase):
    def setUp(self):
        self.ship, self.package, self.room_a, _ = build_world(
            ship_name=unique("Mail"), starts_in_days=30
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")
        mail.outbox = []

    def test_request_acknowledgement_states_the_figures(self):
        # Every notification is dispatched on commit (a mail outage must never
        # roll back a cancellation), so the test transaction has to be made to
        # run those callbacks.
        with self.captureOnCommitCallbacks(execute=True):
            services.create_cancellation_request(
                self.booking,
                reason_code=CancellationRequest.Reason.PLANS_CHANGED,
                refund_method="bkash",
                refund_account_number="01712345678",
                refund_account_name="Rahim",
            )
        body = mail.outbox[0].body
        self.assertIn("7600.00", body)
        self.assertIn("400.00", body)
        self.assertIn("NOT cancelled yet", body)

    def test_cancellation_email_quotes_the_real_refund_once_approved(self):
        staff = User.objects.create_user(
            username=unique("mailstaff"), password="pass12345", is_staff=True
        )
        with self.captureOnCommitCallbacks(execute=True):
            request = services.create_cancellation_request(
                self.booking,
                reason_code=CancellationRequest.Reason.PLANS_CHANGED,
                refund_method="bkash",
                refund_account_number="01712345678",
                refund_account_name="Rahim",
            )
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            services.approve_cancellation(request, user=staff)
        bodies = "\n".join(message.body for message in mail.outbox)
        self.assertIn("7600.00", bodies)
        self.assertIn("ending 5678", bodies)


# ── Invoice download links ────────────────────────────────────────────────


class InvoiceDownloadLinkTests(ThrottlelessTestMixin, APITestCase):
    """The download URL is a signed, expiring link — not the invoice's
    permanent access_token, which now never leaves the server."""

    def setUp(self):
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Invoice")
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")
        self.invoice = Invoice.objects.create(
            booking=self.booking,
            total_amount=self.booking.total_amount,
            paid_amount=self.booking.paid_amount,
            due_amount=self.booking.due_amount,
        )
        self.invoice.pdf_file.save(
            "x.pdf", ContentFile(b"%PDF-1.4 test"), save=True
        )

    def listed_url(self):
        response = self.client.get(
            f"/api/bookings/{self.booking.booking_code}/invoices/"
        )
        self.assertEqual(response.status_code, 200)
        return response.data[0]["download_url"]

    def test_the_permanent_token_never_appears_in_the_link(self):
        self.assertNotIn(self.invoice.access_token, self.listed_url())

    def test_a_fresh_link_downloads_the_pdf_as_an_attachment(self):
        response = APIClient().get(self.listed_url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            b"".join(response.streaming_content).startswith(b"%PDF")
        )
        # Attachment, not inline: an inline PDF parks the token in the address
        # bar and the browser history.
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_the_old_permanent_token_no_longer_opens_anything(self):
        response = APIClient().get(
            f"/api/invoices/{self.invoice.access_token}/download/"
        )
        self.assertEqual(response.status_code, 404)

    def test_an_expired_link_is_refused_with_a_reason(self):
        """A link that aged out is the one failure the holder did nothing to
        cause, so it says so rather than 404-ing blankly."""
        url = self.listed_url()
        later = timezone.now() + timedelta(
            seconds=invoice_access.MAX_AGE_SECONDS + 60
        )
        with patch("django.core.signing.time.time", return_value=later.timestamp()):
            response = APIClient().get(url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "link_expired")

    def test_a_tampered_link_gets_a_blank_404(self):
        url = self.listed_url()
        tampered = f"{url[:-4]}zzzz/"
        self.assertEqual(APIClient().get(tampered).status_code, 404)

    def test_a_link_only_ever_serves_its_own_invoice(self):
        """One customer's link must not reach another's document, and the
        listing must not leak anyone else's."""
        other = make_booking(self.package, self.room_b, paid="8000.00")
        other_invoice = Invoice.objects.create(
            booking=other,
            total_amount=other.total_amount,
            paid_amount=other.paid_amount,
            due_amount=other.due_amount,
        )
        other_invoice.pdf_file.save("y.pdf", ContentFile(b"%PDF other"), save=True)

        response = APIClient().get(self.listed_url())
        body = b"".join(response.streaming_content)
        self.assertIn(b"test", body)
        self.assertNotIn(b"other", body)

        listing = self.client.get(
            f"/api/bookings/{self.booking.booking_code}/invoices/"
        )
        self.assertEqual(len(listing.data), 1)
        self.assertEqual(listing.data[0]["number"], self.invoice.number)


class PendingCancellationVisibilityTests(ThrottlelessTestMixin, APITestCase):
    """The booking payload has to say a request is open, or the page shows a
    live Cancel button to someone who already pressed it."""

    def setUp(self):
        self.ship, self.package, self.room_a, _ = build_world(
            ship_name=unique("Pending")
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")

    def booking_payload(self):
        response = self.client.get(f"/api/bookings/{self.booking.booking_code}/")
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_null_until_a_request_exists(self):
        self.assertIsNone(self.booking_payload()["pending_cancellation"])

    def test_carries_the_open_request_with_its_figures(self):
        services.create_cancellation_request(
            self.booking,
            reason_code=CancellationRequest.Reason.PLANS_CHANGED,
            refund_method="bkash",
            refund_account_number="01712345678",
            refund_account_name="Rahim",
        )
        pending = self.booking_payload()["pending_cancellation"]
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["refund_amount"], "7600.00")
        # Masked here too — this endpoint needs only the booking code.
        self.assertTrue(pending["refund_account_masked"].endswith("5678"))
        self.assertNotIn("01712345678", str(pending))

    def test_clears_once_the_request_is_decided(self):
        staff = User.objects.create_user(
            username=unique("dec"), password="pass12345", is_staff=True
        )
        request = services.create_cancellation_request(
            self.booking,
            reason_code=CancellationRequest.Reason.PLANS_CHANGED,
            refund_method="bkash",
            refund_account_number="01712345678",
            refund_account_name="Rahim",
        )
        services.reject_cancellation(request, user=staff, note="Spoke to them.")
        self.assertIsNone(self.booking_payload()["pending_cancellation"])


class PaymentInFlightTests(ThrottlelessTestMixin, APITestCase):
    """Money on its way to the gateway, versus a cancellation.

    An SSLCommerz session cannot be voided once handed out, so the two can
    genuinely overlap; these pin down what happens when they do.
    """

    def setUp(self):
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Flight")
        )
        self.booking = make_booking(self.package, self.room_a, paid="8000.00")

    def open_session(self, booking=None, amount="4000.00", age_minutes=0):
        payment = Payment.objects.create(
            booking=booking or self.booking,
            amount=Decimal(amount),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.PENDING,
            transaction_id=unique("TRX"),
        )
        if age_minutes:
            Payment.objects.filter(pk=payment.pk).update(
                created_at=timezone.now() - timedelta(minutes=age_minutes)
            )
        return payment

    def test_a_live_session_blocks_the_customer_from_cancelling(self):
        self.open_session()
        quote = policy.quote_cancellation(self.booking)
        self.assertFalse(quote.allowed)
        self.assertEqual(quote.block_reason, policy.BLOCK_PAYMENT_IN_PROGRESS)

    def test_the_public_endpoint_refuses_while_a_session_is_live(self):
        self.open_session()
        preview = self.client.get(
            f"/api/bookings/{self.booking.booking_code}/cancellation-preview/"
        )
        self.assertFalse(preview.data["allowed"])
        self.assertEqual(preview.data["block_reason"], "payment_in_progress")
        self.assertIsNone(preview.data["quote_token"])

    def test_an_abandoned_session_stops_blocking_once_it_can_no_longer_settle(self):
        """Nothing can settle on a session older than the gateway's lifetime,
        and the job that closes those does not run on every hosting tier — so
        one abandoned checkout must not lock the customer out forever."""
        self.open_session(age_minutes=120)
        self.assertTrue(policy.quote_cancellation(self.booking).allowed)

    def test_a_settled_payment_does_not_block(self):
        """has_live_payment_session must not be _has_money_in_flight, which
        counts SUCCESS too — that would refuse every paid booking."""
        Payment.objects.create(
            booking=self.booking,
            amount=Decimal("8000.00"),
            payment_type=Payment.PaymentType.FULL,
            status=Payment.Status.SUCCESS,
            transaction_id=unique("TRX"),
        )
        self.assertTrue(policy.quote_cancellation(self.booking).allowed)

    def test_staff_are_warned_rather_than_blocked(self):
        self.open_session()
        staff = User.objects.create_user(
            username=unique("desk"), password="pass12345", is_staff=True
        )
        self.client.force_authenticate(user=staff)
        response = self.client.get(f"/api/staff/bookings/{self.booking.pk}/cancel/")
        self.assertTrue(response.data["allowed"])
        self.assertTrue(response.data["payment_in_progress"])


class SettlementAfterCancellationTests(ThrottlelessTestMixin, APITestCase):
    """Money that lands after the booking is already cancelled must reach the
    refund LEDGER, not just the flag — the liability figure and the register
    are what refunds are actually worked from."""

    def setUp(self):
        self.ship, self.package, self.room_a, self.room_b = build_world(
            ship_name=unique("Late")
        )

    def settle_on_cancelled(self, booking, amount):
        """What payment_service does when a gateway session settles on a
        booking that has since been cancelled."""
        from apps.bookings.payment_service import (
            _raise_refund_for_settled_on_cancelled,
        )

        payment = Payment.objects.create(
            booking=booking,
            amount=Decimal(amount),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
            transaction_id=unique("TRX"),
        )
        booking.refresh_paid_amount()
        _raise_refund_for_settled_on_cancelled(
            booking, payment, payment.transaction_id
        )
        return payment

    def test_raises_a_refund_when_the_booking_had_no_open_payout(self):
        booking = make_booking(self.package, self.room_a)
        booking.status = Booking.Status.CANCELLED
        booking.save()

        self.settle_on_cancelled(booking, "3000.00")

        refund = Refund.objects.get(booking=booking)
        self.assertEqual(refund.amount, Decimal("3000.00"))
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.created_by, None)  # system-raised

    def test_folds_into_the_open_payout_instead_of_colliding_with_it(self):
        """The usual case: a cancellation refund is already open, and a second
        row would violate one-open-refund-per-booking and lose the money."""
        booking = make_booking(self.package, self.room_b)
        # A REAL settled payment, not a faked paid_amount: refresh_paid_amount
        # recomputes from Payment rows, so the deposit has to exist as one or
        # the later settlement would wipe it out of the total.
        Payment.objects.create(
            booking=booking,
            amount=Decimal("4000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
            transaction_id=unique("TRX"),
        )
        booking.refresh_paid_amount()
        staff = User.objects.create_user(
            username=unique("ops"), password="pass12345", is_staff=True
        )
        services.staff_cancel_booking(
            booking,
            user=staff,
            reason_code=CancellationRequest.Reason.PLANS_CHANGED,
            waive_charge=True,
            reason_note="Goodwill.",
        )
        first = Refund.objects.get(booking=booking)
        self.assertEqual(first.amount, Decimal("4000.00"))

        booking.refresh_from_db()
        self.settle_on_cancelled(booking, "2500.00")

        self.assertEqual(Refund.objects.filter(booking=booking).count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("6500.00"))
        # The increase is auditable, not a silent overwrite.
        self.assertTrue(first.status_logs.filter(note__icontains="Increased").exists())

    def test_a_bookkeeping_failure_never_breaks_the_gateway_callback(self):
        """SSLCommerz retries a failed IPN forever; a refund-row problem must
        not be what makes it fail. The flag set alongside still records it."""
        booking = make_booking(self.package, self.room_a)
        booking.status = Booking.Status.CANCELLED
        booking.save()
        from apps.bookings.payment_service import (
            _raise_refund_for_settled_on_cancelled,
        )

        payment = Payment.objects.create(
            booking=booking,
            amount=Decimal("1000.00"),
            payment_type=Payment.PaymentType.PARTIAL,
            status=Payment.Status.SUCCESS,
            transaction_id=unique("TRX"),
        )
        with patch(
            "apps.refunds.services.add_to_open_refund_or_create",
            side_effect=RuntimeError("ledger is on fire"),
        ):
            # Must not raise.
            _raise_refund_for_settled_on_cancelled(booking, payment, "TRX-BAD")
        self.assertFalse(Refund.objects.filter(booking=booking).exists())
