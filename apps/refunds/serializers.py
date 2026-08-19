"""Public (customer-facing) serialization for cancellations.

The input serializer's job is narrow and strict: it accepts a REASON, a PAYOUT
DESTINATION and a confirmation, and nothing else. It does not accept an amount,
a percentage, a tier or a date — those are computed by ``policy`` from the
booking's own record every time, and the only thing the customer's screen
contributes is a signed token proving which figures they agreed to.
"""

import re

from rest_framework import serializers

from apps.bookings.identity import phone_digits, phone_last4_matches

from .models import CancellationRequest, PayoutMethod, Refund

#: Payout channels a customer may choose. CASH and GATEWAY exist on the model
#: for staff-recorded payouts — a customer cannot ask to be handed cash, and
#: gateway refunds are a merchant-side action, so neither is offered here.
CUSTOMER_PAYOUT_METHODS = [
    PayoutMethod.BKASH,
    PayoutMethod.NAGAD,
    PayoutMethod.BANK_TRANSFER,
]

#: bKash/Nagad wallets are Bangladeshi mobile numbers.
BD_MOBILE = re.compile(r"^01[3-9]\d{8}$")


class CancellationQuoteSerializer(serializers.Serializer):
    """What cancelling would cost — the preview screen, and the echo returned
    when a submitted quote no longer matches."""

    allowed = serializers.BooleanField()
    block_reason = serializers.CharField(allow_null=True)
    window = serializers.CharField()
    days_until_start = serializers.IntegerField()
    booking_type = serializers.CharField()
    tier_label = serializers.CharField()
    charge_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    cancellation_charge = serializers.DecimalField(max_digits=12, decimal_places=2)
    refund_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    forfeited_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    requires_approval = serializers.BooleanField()
    # shortfall is deliberately NOT exposed: it is an internal reporting figure
    # for charge the deposit did not cover, it is waived, and showing a customer
    # "you still owe 4,000 BDT" for money we will never ask for only frightens
    # them into calling.


class CancellationRequestCreateSerializer(serializers.Serializer):
    """Everything the customer submits to request a cancellation."""

    phone_confirm = serializers.CharField(
        max_length=20,
        help_text="Last 4 digits of the phone number on the booking.",
    )
    reason_code = serializers.ChoiceField(choices=CancellationRequest.Reason.choices)
    reason_note = serializers.CharField(
        max_length=500, allow_blank=True, required=False, default=""
    )
    # Payout details are required only when there is a payout. A booking with
    # nothing paid is cancelled outright, and asking someone for a bKash number
    # so we can send them zero taka is pure friction — see validate().
    refund_method = serializers.ChoiceField(
        choices=[(m.value, m.label) for m in CUSTOMER_PAYOUT_METHODS],
        required=False,
        allow_blank=True,
        default="",
    )
    refund_account_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    refund_account_number = serializers.CharField(
        max_length=40, required=False, allow_blank=True, default=""
    )
    bank_name = serializers.CharField(
        max_length=120, allow_blank=True, required=False, default=""
    )
    branch_name = serializers.CharField(
        max_length=120, allow_blank=True, required=False, default=""
    )
    acknowledged_charge = serializers.BooleanField()
    quote_token = serializers.CharField()

    def validate_phone_confirm(self, value):
        booking = self.context["booking"]
        if not phone_last4_matches(booking, value):
            # One message for both "no such booking" and "wrong digits" — the
            # response must not confirm that a code is real to someone who
            # cannot pass the check.
            raise serializers.ValidationError(
                "The booking code and phone number do not match our records."
            )
        return phone_digits(value)[-4:]

    def validate_acknowledged_charge(self, value):
        if not value:
            raise serializers.ValidationError(
                "Please confirm you accept the cancellation charge."
            )
        return value

    def validate(self, attrs):
        if attrs["reason_code"] == CancellationRequest.Reason.OTHER and not attrs.get(
            "reason_note", ""
        ).strip():
            raise serializers.ValidationError(
                {"reason_note": "Please tell us why you are cancelling."}
            )

        booking = self.context["booking"]
        method = attrs["refund_method"]
        number = attrs["refund_account_number"].strip()

        if booking.paid_amount <= 0:
            # Nothing to send back. Drop whatever was posted rather than storing
            # a payout destination for a payout that will never exist.
            attrs["refund_method"] = ""
            attrs["refund_account_number"] = ""
            attrs["refund_account_name"] = ""
            attrs["bank_name"] = ""
            attrs["branch_name"] = ""
            return attrs

        if not method:
            raise serializers.ValidationError(
                {"refund_method": "Tell us how you would like the refund sent."}
            )
        if not attrs["refund_account_name"].strip():
            raise serializers.ValidationError(
                {"refund_account_name": "Enter the account holder's name."}
            )
        if method in (PayoutMethod.BKASH, PayoutMethod.NAGAD):
            digits = phone_digits(number)
            if not BD_MOBILE.match(digits):
                raise serializers.ValidationError(
                    {
                        "refund_account_number": (
                            "Enter the 11-digit mobile number of the wallet, "
                            "e.g. 01712345678."
                        )
                    }
                )
            attrs["refund_account_number"] = digits
        else:
            if not re.match(r"^[A-Za-z0-9\- ]{6,40}$", number):
                raise serializers.ValidationError(
                    {"refund_account_number": "Enter a valid bank account number."}
                )
            attrs["refund_account_number"] = number
            if not attrs.get("bank_name", "").strip():
                raise serializers.ValidationError(
                    {"bank_name": "Bank name is required for a bank transfer."}
                )
            if not attrs.get("branch_name", "").strip():
                raise serializers.ValidationError(
                    {"branch_name": "Branch name is required for a bank transfer."}
                )
        return attrs


class CancellationRequestPublicSerializer(serializers.ModelSerializer):
    """The receipt the customer gets back, and what the booking page shows while
    a request is open. The payout account is echoed MASKED — enough to confirm
    they typed the right wallet, not enough to be worth intercepting."""

    reason_label = serializers.CharField(source="get_reason_code_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    refund_method_label = serializers.CharField(
        source="get_refund_method_display", read_only=True
    )
    refund_account_masked = serializers.SerializerMethodField()
    tier_label = serializers.SerializerMethodField()

    class Meta:
        model = CancellationRequest
        fields = [
            "id",
            "status",
            "status_label",
            "reason_code",
            "reason_label",
            "tier_label",
            "total_amount",
            "paid_amount",
            "cancellation_charge",
            "refund_amount",
            "refund_method",
            "refund_method_label",
            "refund_account_masked",
            "requested_at",
            "decided_at",
            "decision_note",
        ]
        read_only_fields = fields

    def get_refund_account_masked(self, request):
        number = request.refund_account_number
        if not number:
            return ""
        return f"{'•' * max(len(number) - 4, 0)}{number[-4:]}"

    def get_tier_label(self, request):
        return request.policy_snapshot.get("tier_label", "")


class CancellationRuleSerializer(serializers.Serializer):
    """The published schedule, so the policy page renders the same table the
    backend charges from."""

    days_before_start = serializers.IntegerField()
    label = serializers.CharField()
    individual_percent = serializers.CharField()
    group_percent = serializers.CharField()


class RefundPublicSerializer(serializers.ModelSerializer):
    """What the customer may see about their own payout. No account number, no
    internal note, no staff identity."""

    status_label = serializers.CharField(source="get_status_display", read_only=True)
    method_label = serializers.CharField(source="get_method_display", read_only=True)

    class Meta:
        model = Refund
        fields = [
            "amount",
            "status",
            "status_label",
            "method_label",
            "reference_no",
            "paid_at",
            "created_at",
        ]
        read_only_fields = fields
