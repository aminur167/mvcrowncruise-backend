"""Staff serialization for the cancellation queue and the refund register.

Split out of ``staff/serializers.py`` rather than appended to it: this is a
self-contained feature with its own vocabulary, and that module is already ~840
lines of unrelated CRUD.

Staff see more than customers do — the full payout account, the internal notes,
who decided what — because they are the people who have to send the money and
answer for it. What they still cannot do is TYPE an amount for a cancellation:
those figures come from the frozen request. The only place a human keys in a
number is a goodwill/overpayment refund, which is a deliberate decision and is
checked against what the booking actually received.
"""

from rest_framework import serializers

from apps.refunds.models import (
    CancellationRequest,
    CancellationRule,
    PayoutMethod,
    Refund,
)


class StaffCancellationRequestListSerializer(serializers.ModelSerializer):
    booking_code = serializers.CharField(source="booking.booking_code", read_only=True)
    customer_name = serializers.CharField(
        source="booking.customer_name", read_only=True
    )
    phone = serializers.CharField(source="booking.phone", read_only=True)
    package_start_date = serializers.DateField(
        source="booking.package.start_date", read_only=True
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    reason_label = serializers.CharField(
        source="get_reason_code_display", read_only=True
    )
    tier_label = serializers.SerializerMethodField()
    #: Payout destinations are masked in the LIST. The queue is the screen most
    #: likely to be open on a shared desk or in a screenshot; the full number is
    #: one click away on the detail view, where it is actually needed.
    refund_account_masked = serializers.SerializerMethodField()
    departure_passed = serializers.SerializerMethodField()

    class Meta:
        model = CancellationRequest
        fields = [
            "id",
            "booking_code",
            "customer_name",
            "phone",
            "package_start_date",
            "departure_passed",
            "source",
            "status",
            "status_label",
            "reason_code",
            "reason_label",
            "reason_note",
            "booking_type",
            "tier_label",
            "total_amount",
            "paid_amount",
            "cancellation_charge",
            "refund_amount",
            "shortfall_amount",
            "refund_method",
            "refund_account_masked",
            "requested_at",
            "decided_at",
            "decision_note",
        ]
        read_only_fields = fields

    def get_tier_label(self, request):
        return request.policy_snapshot.get("tier_label", "")

    def get_refund_account_masked(self, request):
        number = request.refund_account_number
        if not number:
            return ""
        return f"{'•' * max(len(number) - 4, 0)}{number[-4:]}"

    def get_departure_passed(self, request):
        """Flags a request the customer filed in time but nobody has decided —
        and the ship has now sailed. The charge stays frozen at what they were
        quoted; this is purely to make the queue shout about it."""
        from django.utils import timezone

        return request.booking.package.start_date <= timezone.localdate()


class StaffCancellationRequestDetailSerializer(StaffCancellationRequestListSerializer):
    refund_account_number = serializers.CharField(read_only=True)
    refund_account_name = serializers.CharField(read_only=True)
    bank_name = serializers.CharField(read_only=True)
    branch_name = serializers.CharField(read_only=True)
    policy_snapshot = serializers.JSONField(read_only=True)
    decided_by_name = serializers.SerializerMethodField()
    refund_id = serializers.SerializerMethodField()

    class Meta(StaffCancellationRequestListSerializer.Meta):
        fields = StaffCancellationRequestListSerializer.Meta.fields + [
            "refund_account_number",
            "refund_account_name",
            "bank_name",
            "branch_name",
            "policy_snapshot",
            "decided_by_name",
            "refund_id",
        ]
        read_only_fields = fields

    def get_decided_by_name(self, request):
        return request.decided_by.get_username() if request.decided_by_id else ""

    def get_refund_id(self, request):
        refund = getattr(request, "refund", None)
        return refund.pk if refund else None


class StaffCancellationDecisionSerializer(serializers.Serializer):
    """Approve or reject. Notably has NO amount field: approval honours the
    figures frozen when the customer submitted, so there is nothing to type."""

    note = serializers.CharField(
        max_length=1000, allow_blank=True, required=False, default=""
    )


class StaffCancelBookingSerializer(serializers.Serializer):
    """Staff cancelling on the customer's behalf (the phone call)."""

    reason_code = serializers.ChoiceField(choices=CancellationRequest.Reason.choices)
    reason_note = serializers.CharField(
        max_length=1000, allow_blank=True, required=False, default=""
    )
    waive_charge = serializers.BooleanField(required=False, default=False)
    refund_method = serializers.ChoiceField(
        choices=PayoutMethod.choices, required=False, allow_blank=True, default=""
    )
    refund_account_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    refund_account_number = serializers.CharField(
        max_length=40, required=False, allow_blank=True, default=""
    )
    bank_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    branch_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        if attrs.get("waive_charge") and not attrs.get("reason_note", "").strip():
            raise serializers.ValidationError(
                {"reason_note": "Say why the cancellation charge is being waived."}
            )
        return attrs


class StaffRefundSerializer(serializers.ModelSerializer):
    booking_code = serializers.CharField(source="booking.booking_code", read_only=True)
    customer_name = serializers.CharField(
        source="booking.customer_name", read_only=True
    )
    phone = serializers.CharField(source="booking.phone", read_only=True)
    package_start_date = serializers.DateField(
        source="booking.package.start_date", read_only=True
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    reason_label = serializers.CharField(source="get_reason_display", read_only=True)
    method_label = serializers.CharField(source="get_method_display", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    processed_by_name = serializers.SerializerMethodField()
    age_days = serializers.SerializerMethodField()
    overdue = serializers.SerializerMethodField()

    class Meta:
        model = Refund
        fields = [
            "id",
            "booking_code",
            "customer_name",
            "phone",
            "package_start_date",
            "reason",
            "reason_label",
            "amount",
            "cancellation_charge",
            "status",
            "status_label",
            "method",
            "method_label",
            "account_name",
            "account_number",
            "bank_name",
            "branch_name",
            "reference_no",
            "note",
            "created_by_name",
            "processed_by_name",
            "paid_at",
            "created_at",
            "age_days",
            "overdue",
        ]
        read_only_fields = fields

    def get_created_by_name(self, refund):
        return refund.created_by.get_username() if refund.created_by_id else "system"

    def get_processed_by_name(self, refund):
        return refund.processed_by.get_username() if refund.processed_by_id else ""

    def get_age_days(self, refund):
        from django.utils import timezone

        return (timezone.now() - refund.created_at).days

    def get_overdue(self, refund):
        """Pending past the SLA we promised the customer. This is the number the
        dashboard should be shouting about — an unpaid refund is a promise the
        company has broken, not merely a task."""
        if refund.status != Refund.Status.PENDING:
            return False
        return self.get_age_days(refund) > refund.booking.package.ship.refund_sla_days


class StaffRefundCreateSerializer(serializers.Serializer):
    """Raise a refund by hand — overpayment, duplicate settlement, goodwill.

    Customer cancellations do NOT come through here: those are created by
    approving a request, so the tier schedule can never be bypassed by typing a
    number into a form.
    """

    MANUAL_REASONS = [
        Refund.Reason.OVERPAYMENT,
        Refund.Reason.DUPLICATE_PAYMENT,
        Refund.Reason.GOODWILL,
        Refund.Reason.OPERATOR_CANCELLATION,
    ]

    booking_code = serializers.CharField(max_length=40)
    reason = serializers.ChoiceField(
        choices=[(r.value, r.label) for r in MANUAL_REASONS]
    )
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    method = serializers.ChoiceField(
        choices=PayoutMethod.choices, required=False, allow_blank=True, default=""
    )
    account_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    account_number = serializers.CharField(
        max_length=40, required=False, allow_blank=True, default=""
    )
    bank_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    branch_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    note = serializers.CharField(max_length=1000, allow_blank=True, default="")
    allow_outside_claim_window = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if attrs["reason"] == Refund.Reason.GOODWILL and not attrs["note"].strip():
            raise serializers.ValidationError(
                {"note": "A goodwill refund must record why it was given."}
            )
        if attrs["allow_outside_claim_window"] and not attrs["note"].strip():
            raise serializers.ValidationError(
                {
                    "note": (
                        "Overriding the claim window reopens a closed period — "
                        "record the justification."
                    )
                }
            )
        return attrs


class StaffRefundPaySerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=PayoutMethod.choices)
    reference_no = serializers.CharField(max_length=64)
    note = serializers.CharField(
        max_length=1000, allow_blank=True, required=False, default=""
    )

    def validate_reference_no(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "Enter the transaction id — a payout with no reference cannot "
                "be reconciled."
            )
        return value.strip()


class StaffRefundVoidSerializer(serializers.Serializer):
    note = serializers.CharField(max_length=1000)


class StaffCancellationRuleSerializer(serializers.ModelSerializer):
    """The charge schedule, editable from the dashboard.

    Editing it never touches money already quoted: every request and refund
    carries its own frozen snapshot of the tier it was priced against.
    """

    ship_name = serializers.CharField(source="ship.name", read_only=True)

    class Meta:
        model = CancellationRule
        fields = [
            "id",
            "ship",
            "ship_name",
            "days_before_start",
            "label",
            "individual_percent",
            "group_percent",
            "is_active",
        ]


class StaffDepartureCancelSerializer(serializers.Serializer):
    """Cancelling a whole sailing (weather, technical, minimum pax not met)."""

    reason_note = serializers.CharField(max_length=1000)
    #: Preview first: how many bookings, how many people, how much money. A
    #: destructive bulk action should never be a single unpreviewed click.
    dry_run = serializers.BooleanField(default=True)
    confirm_package_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        package = self.context["package"]
        if not attrs["dry_run"] and attrs.get("confirm_package_id") != package.pk:
            raise serializers.ValidationError(
                {
                    "confirm_package_id": (
                        "Re-send the package id to confirm you are cancelling "
                        "this departure."
                    )
                }
            )
        return attrs
