"""Staff API for the cancellation queue and the refund register.

Everything here is behind IsAdminUser. Two rules shape the design:

- **Staff decide, they do not price.** Approving a cancellation takes no amount:
  the figures were frozen when the customer submitted and approving honours
  them. The only endpoint that accepts a typed amount is the manual-refund one
  (overpayment, duplicate, goodwill), and even that is checked against what the
  booking actually received.
- **Every payout is attributable.** Approve, pay and void all record who did it,
  and a payout cannot be marked paid without a transaction reference — a refund
  register that does not reconcile against the gateway settlement is not an
  accounting document, it is a wish list.
"""

from contextlib import contextmanager
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bookings.identity import normalize_booking_code
from apps.bookings.models import Booking
from apps.packages.models import Package
from apps.refunds import policy, services
from apps.refunds.models import CancellationRequest, CancellationRule, Refund

from .refunds_serializers import (
    StaffCancelBookingSerializer,
    StaffCancellationDecisionSerializer,
    StaffCancellationRequestDetailSerializer,
    StaffCancellationRequestListSerializer,
    StaffCancellationRuleSerializer,
    StaffDepartureCancelSerializer,
    StaffRefundCreateSerializer,
    StaffRefundPaySerializer,
    StaffRefundSerializer,
    StaffRefundVoidSerializer,
)
from .views import StaffPagination


@contextmanager
def as_drf_errors():
    """Services raise Django's ValidationError (they are callable from crons and
    the admin, not just DRF). DRF does not understand that class and would turn
    a perfectly ordinary "already decided" race into a 500, so translate."""
    try:
        yield
    except DjangoValidationError as exc:
        raise ValidationError(
            exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
        ) from exc


class StaffCancellationRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """The approval queue. Read-only plus two decisions — a request is never
    edited, only decided, so the record of what the customer asked for and was
    quoted stays intact."""

    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination
    queryset = (
        CancellationRequest.objects.select_related(
            "booking", "booking__package", "booking__package__ship", "decided_by"
        )
        .prefetch_related("booking__rooms")
        .all()
    )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return StaffCancellationRequestDetailSerializer
        return StaffCancellationRequestListSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("package"):
            qs = qs.filter(booking__package_id=params["package"])
        if params.get("search"):
            term = params["search"].strip()
            qs = qs.filter(
                Q(booking__booking_code__icontains=term)
                | Q(booking__customer_name__icontains=term)
                | Q(booking__phone__icontains=term)
            )
        return qs

    @action(detail=False, methods=["get"])
    def summary(self, request):
        pending = self.get_queryset().filter(
            status=CancellationRequest.Status.PENDING
        )
        agg = pending.aggregate(count=Count("id"), refund_total=Sum("refund_amount"))
        # Requests filed in time that nobody has decided while the ship sailed.
        # The customer did their part; this is our backlog, and it is the one
        # thing in the queue that cannot be fixed by acting sooner.
        stale = pending.filter(
            booking__package__start_date__lte=timezone.localdate()
        ).count()
        return Response(
            {
                "pending_count": agg["count"] or 0,
                "pending_refund_total": str(agg["refund_total"] or "0.00"),
                "departed_undecided_count": stale,
            }
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Cancel the booking and raise the payout, on the frozen figures."""
        obj = self.get_object()
        serializer = StaffCancellationDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with as_drf_errors():
            refund = services.approve_cancellation(
                obj, user=request.user, note=serializer.validated_data["note"]
            )
        obj.refresh_from_db()
        return Response(
            {
                "request": StaffCancellationRequestDetailSerializer(obj).data,
                "refund": StaffRefundSerializer(refund).data if refund else None,
            }
        )

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """Decline. The booking never changed status, so nothing is restored —
        but the customer is told why, so the note is mandatory."""
        obj = self.get_object()
        serializer = StaffCancellationDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with as_drf_errors():
            services.reject_cancellation(
                obj, user=request.user, note=serializer.validated_data["note"]
            )
        obj.refresh_from_db()
        return Response(StaffCancellationRequestDetailSerializer(obj).data)


class StaffRefundViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """The refund register: what is owed, what has been paid, and by whom."""

    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination
    serializer_class = StaffRefundSerializer
    queryset = Refund.objects.select_related(
        "booking", "booking__package", "booking__package__ship", "created_by", "processed_by"
    ).all()

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("reason"):
            qs = qs.filter(reason=params["reason"])
        if params.get("package"):
            qs = qs.filter(booking__package_id=params["package"])
        if params.get("from"):
            qs = qs.filter(created_at__date__gte=params["from"])
        if params.get("to"):
            qs = qs.filter(created_at__date__lte=params["to"])
        if params.get("search"):
            term = params["search"].strip()
            qs = qs.filter(
                Q(booking__booking_code__icontains=term)
                | Q(booking__customer_name__icontains=term)
                | Q(booking__phone__icontains=term)
                | Q(reference_no__icontains=term)
            )
        if params.get("overdue") == "true":
            # Approximated in SQL against the default SLA, then exact per row in
            # the serializer: refund_sla_days is per-ship, and a JOIN-side
            # comparison per row is not worth it for a queue this small.
            cutoff = timezone.now() - timedelta(days=1)
            qs = qs.filter(status=Refund.Status.PENDING, created_at__lt=cutoff)
        return qs

    def create(self, request, *args, **kwargs):
        """Raise a refund by hand. Cancellation refunds do not come through
        here — they are created by approving a request, so no one can bypass
        the charge schedule by typing an amount."""
        serializer = StaffRefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = get_object_or_404(
            Booking, booking_code=normalize_booking_code(data["booking_code"])
        )
        with as_drf_errors():
            refund = services.create_refund(
                booking,
                reason=data["reason"],
                amount=data["amount"],
                user=request.user,
                method=data["method"],
                account_name=data["account_name"],
                account_number=data["account_number"],
                bank_name=data["bank_name"],
                branch_name=data["branch_name"],
                note=data["note"],
                allow_outside_claim_window=data["allow_outside_claim_window"],
            )
        return Response(StaffRefundSerializer(refund).data, status=201)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        refund = self.get_object()
        serializer = StaffRefundPaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with as_drf_errors():
            services.mark_refund_paid(
                refund, user=request.user, **serializer.validated_data
            )
        refund.refresh_from_db()
        return Response(StaffRefundSerializer(refund).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        refund = self.get_object()
        serializer = StaffRefundVoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with as_drf_errors():
            services.void_refund(
                refund, user=request.user, note=serializer.validated_data["note"]
            )
        refund.refresh_from_db()
        return Response(StaffRefundSerializer(refund).data)

    @action(detail=False, methods=["get"])
    def register(self, request):
        """The refund register, as a PDF to file or a CSV to reconcile.

        Same filters as the list, so what you are looking at is what you export.
        """
        from apps.refunds.reports import (
            generate_register_csv,
            generate_register_pdf,
            register_queryset,
        )
        from apps.ships.models import Ship

        params = request.query_params
        refunds = list(
            register_queryset(
                date_from=params.get("from") or None,
                date_to=params.get("to") or None,
                package=params.get("package") or None,
                status=params.get("status") or None,
            )
        )
        stamp = timezone.localdate().isoformat()

        # Deliberately NOT called "format": DRF reserves that query parameter
        # for content negotiation and would 404 on an unknown renderer before
        # this method ever ran.
        if params.get("export") == "csv":
            response = HttpResponse(
                generate_register_csv(refunds), content_type="text/csv"
            )
            response["Content-Disposition"] = (
                f'attachment; filename="refund-register-{stamp}.csv"'
            )
            return response

        ship = (
            refunds[0].booking.package.ship
            if refunds
            else Ship.objects.order_by("id").first()
        )
        if ship is None:
            raise ValidationError({"detail": "No ship configured."})
        pdf = generate_register_pdf(
            refunds,
            ship=ship,
            date_from=params.get("from"),
            date_to=params.get("to"),
        )
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'inline; filename="refund-register-{stamp}.pdf"'
        )
        return response

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Headline figures for the dashboard.

        ``liability`` — money promised and not yet sent — is the important one:
        in accounting terms it is a debt the company is carrying, and it is
        invisible anywhere else in the system.
        """
        qs = self.get_queryset()
        pending = qs.filter(status=Refund.Status.PENDING).aggregate(
            count=Count("id"), total=Sum("amount")
        )
        paid = qs.filter(status=Refund.Status.PAID).aggregate(
            count=Count("id"), total=Sum("amount")
        )
        overdue = [
            refund
            for refund in qs.filter(status=Refund.Status.PENDING)
            if (timezone.now() - refund.created_at).days
            > refund.booking.package.ship.refund_sla_days
        ]
        return Response(
            {
                "liability_count": pending["count"] or 0,
                "liability_total": str(pending["total"] or "0.00"),
                "paid_count": paid["count"] or 0,
                "paid_total": str(paid["total"] or "0.00"),
                "overdue_count": len(overdue),
                "overdue_total": str(sum((r.amount for r in overdue), 0)),
            }
        )


class StaffCancellationRuleViewSet(viewsets.ModelViewSet):
    """CRUD on the charge schedule.

    Safe to edit at any time: every quote already given carries its own frozen
    copy of the tier it used, so changing a percentage here moves future
    cancellations only.
    """

    permission_classes = [IsAdminUser]
    serializer_class = StaffCancellationRuleSerializer
    pagination_class = None
    queryset = CancellationRule.objects.select_related("ship").all()

    def get_queryset(self):
        qs = super().get_queryset()
        ship = self.request.query_params.get("ship")
        if ship == "default":
            qs = qs.filter(ship__isnull=True)
        elif ship:
            qs = qs.filter(ship_id=ship)
        return qs


class StaffBookingCancelView(APIView):
    """POST /api/staff/bookings/<pk>/cancel/ — cancel on the customer's behalf.

    The phone-call path. No approval step (staff ARE the approval), but the same
    record is written, so the register cannot tell the difference later.
    """

    permission_classes = [IsAdminUser]

    def get(self, request, pk):
        """What this cancellation would cost, so the person on the phone can
        read the customer the figure before doing it."""
        booking = get_object_or_404(
            Booking.objects.select_related("package", "package__ship"), pk=pk
        )
        quote = policy.quote_cancellation(booking, ignore_pending=True)
        return Response(
            {
                "allowed": quote.allowed,
                "block_reason": quote.block_reason,
                "tier_label": quote.tier_label,
                "charge_percent": str(quote.charge_percent),
                "paid_amount": str(quote.paid_amount),
                "cancellation_charge": str(quote.cancellation_charge),
                "refund_amount": str(quote.refund_amount),
                "shortfall_amount": str(quote.shortfall_amount),
                "suggests_group": policy.suggests_group(booking),
                "booking_type": booking.booking_type,
                # Staff are warned, not blocked (the customer-facing flow is
                # blocked outright). A live gateway session can still settle
                # after this cancellation — SSLCommerz sessions cannot be
                # voided — and that money would land as a further refund to
                # send back, so whoever is on the phone should know.
                "payment_in_progress": booking.has_live_payment_session(),
            }
        )

    def post(self, request, pk):
        booking = get_object_or_404(
            Booking.objects.select_related("package", "package__ship"), pk=pk
        )
        serializer = StaffCancelBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with as_drf_errors():
            cancellation = services.staff_cancel_booking(
                booking, user=request.user, **serializer.validated_data
            )
        return Response(
            StaffCancellationRequestDetailSerializer(cancellation).data, status=201
        )


class StaffDepartureCancelView(APIView):
    """POST /api/staff/packages/<pk>/cancel-departure/ — the whole sailing.

    Weather, a technical fault, or the passenger minimum not being met. This is
    an INVOLUNTARY cancellation: the tier schedule does not apply and every
    booking is refunded in full. Defaults to a dry run, and the destructive call
    must echo the package id back — a bulk action that cancels dozens of
    holidays should not be reachable by one stray click.
    """

    permission_classes = [IsAdminUser]

    def post(self, request, pk):
        package = get_object_or_404(
            Package.objects.select_related("ship"), pk=pk
        )
        serializer = StaffDepartureCancelSerializer(
            data=request.data, context={"package": package}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with as_drf_errors():
            summary = services.cancel_departure(
                package,
                user=request.user,
                reason_note=data["reason_note"],
                dry_run=data["dry_run"],
            )
        summary["refund_total"] = str(summary["refund_total"])
        summary["dry_run"] = data["dry_run"]
        return Response(summary)
