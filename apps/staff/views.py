"""Staff dashboard API — full CRUD behind IsAdminUser (is_staff)."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.utils import timezone
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.bookings import invoices, payment_service
from apps.bookings.models import Booking, BookingRoom, Invoice, Payment
from apps.bookings.reports import generate_guide_report_pdf
from apps.bookings.serializers import BookingPublicSerializer
from apps.packages.models import (
    ForeignerSurcharge,
    KidPricingRule,
    Package,
    PackageRoom,
    RoomBlocked,
)
from apps.ships.models import (
    Cabin,
    CabinImage,
    FoodMenuItem,
    GalleryImage,
    Room,
    RoomImage,
    RoomType,
    Ship,
)

from .serializers import (
    StaffBookingCreateSerializer,
    StaffBookingDetailSerializer,
    StaffBookingListSerializer,
    StaffBookingUpdateSerializer,
    StaffCabinImageSerializer,
    StaffCabinSerializer,
    StaffFoodMenuItemSerializer,
    StaffGalleryImageSerializer,
    StaffInvoiceSerializer,
    StaffForeignerSurchargeSerializer,
    StaffKidPricingRuleSerializer,
    StaffPackageRoomSerializer,
    StaffPackageSerializer,
    StaffPaymentResolveSerializer,
    StaffPaymentSerializer,
    StaffRoomBlockSerializer,
    StaffRoomImageSerializer,
    StaffRoomSerializer,
    StaffRoomTypeSerializer,
    StaffShipSerializer,
    StaffTokenObtainPairSerializer,
)


class StaffPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class StaffLoginView(TokenObtainPairView):
    serializer_class = StaffTokenObtainPairSerializer
    # Tight per-IP throttle (5/min) to blunt credential stuffing / password
    # spraying against the admin dashboard — the default anon bucket (100/min)
    # is far too loose for a login oracle.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class StaffTokenRefreshView(TokenRefreshView):
    """Same tight throttle as login: a refresh token is a credential too, and
    this endpoint should not be a brute-force bypass around the login limit."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"


class StaffLogoutView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        try:
            RefreshToken(request.data.get("refresh")).blacklist()
        except Exception:
            pass  # already invalid/blacklisted — logout is idempotent
        return Response({"detail": "ok"})


def package_stats_queryset():
    """All packages annotated with booking/collection stats. Subqueries avoid
    the classic multi-join aggregate duplication bug."""
    bookings = Booking.objects.filter(package=OuterRef("pk")).exclude(
        status=Booking.Status.CANCELLED
    )
    return (
        Package.objects.all()
        .select_related("ship")
        .annotate(
            bookings_count=Subquery(
                bookings.values("package").annotate(c=Count("pk")).values("c")[:1]
            ),
            paid_total=Subquery(
                bookings.values("package").annotate(s=Sum("paid_amount")).values("s")[:1]
            ),
            due_total=Subquery(
                bookings.values("package").annotate(s=Sum("due_amount")).values("s")[:1]
            ),
            rooms_total=Subquery(
                PackageRoom.objects.filter(package=OuterRef("pk"))
                .values("package").annotate(c=Count("pk")).values("c")[:1]
            ),
        )
        .order_by("-start_date")
    )


def ship_stats_queryset():
    """All ships annotated with upcoming-package and booking/collection stats,
    for the dashboard's multi-ship breakdown (subqueries — no join fanout)."""
    upcoming_packages = Package.objects.filter(
        ship=OuterRef("pk"), status=Package.Status.OPEN, end_date__gte=timezone.localdate()
    )
    active_bookings = Booking.objects.filter(package__ship=OuterRef("pk")).exclude(
        status=Booking.Status.CANCELLED
    )
    return Ship.objects.all().annotate(
        upcoming_packages=Subquery(
            upcoming_packages.values("ship").annotate(c=Count("pk")).values("c")[:1]
        ),
        active_bookings=Subquery(
            active_bookings.values("package__ship").annotate(c=Count("pk")).values("c")[:1]
        ),
        paid_total=Subquery(
            active_bookings.values("package__ship")
            .annotate(s=Sum("paid_amount"))
            .values("s")[:1]
        ),
        due_total=Subquery(
            active_bookings.values("package__ship")
            .annotate(s=Sum("due_amount"))
            .values("s")[:1]
        ),
    )


class StaffPackageViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = StaffPackageSerializer
    pagination_class = StaffPagination

    def get_queryset(self):
        return package_stats_queryset()

    @action(detail=True, methods=["post"], url_path="close-booking")
    def close_booking(self, request, pk=None):
        package = self.get_object()
        package.is_booking_open = False
        package.save()
        return Response({"detail": "Booking closed."})

    @action(detail=True, methods=["post"], url_path="open-booking")
    def open_booking(self, request, pk=None):
        package = self.get_object()
        package.is_booking_open = True
        package.save()
        return Response({"detail": "Booking reopened."})

    @action(detail=True, methods=["post"], url_path="generate-rooms")
    def generate_rooms(self, request, pk=None):
        package = self.get_object()
        created = 0
        for room in package.ship.rooms.all():
            _, was_created = PackageRoom.objects.get_or_create(package=package, room=room)
            created += was_created
        return Response({"detail": f"{created} room(s) attached."})

    @action(detail=True, methods=["get"])
    def rooms(self, request, pk=None):
        """Room map for one package: every attached room with its status and,
        when booked, the active booking summary (staff-only data)."""
        package = self.get_object()
        package_rooms = (
            PackageRoom.objects.filter(package=package)
            .select_related("room__room_type")
            .order_by("room__floor_number", "room__room_number")
        )
        # room_id → the BookingRoom that actively holds it (its own pax + parent
        # booking). is_active mirrors "still held", so cancelled rooms are out.
        bookings_by_room = {
            br.room_id: br
            for br in BookingRoom.objects.filter(
                package=package, is_active=True
            ).select_related("booking")
        }
        serializer = StaffPackageRoomSerializer(
            package_rooms, many=True, context={"bookings_by_room": bookings_by_room}
        )
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="block-room")
    def block_room(self, request, pk=None):
        """Admin hold: withhold one attached room from sale on this sailing.

        A held room is surfaced to customers as "booked" (simply not on sale;
        the reason never leaves the dashboard) and can be released again at any
        time while the package is live. Booked rooms are rejected — cancel the
        booking first."""
        package = self.get_object()
        serializer = StaffRoomBlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        package_room = self._package_room_or_404(
            package, serializer.validated_data["room_id"]
        )
        try:
            package_room.block(
                user=request.user,
                reason=serializer.validated_data.get("reason", ""),
            )
        except RoomBlocked as exc:
            raise ValidationError({"room_id": exc.messages})
        return Response(
            StaffPackageRoomSerializer(package_room).data
        )

    @action(detail=True, methods=["post"], url_path="unblock-room")
    def unblock_room(self, request, pk=None):
        """Release an admin hold on one attached room, returning it to sale."""
        package = self.get_object()
        serializer = StaffRoomBlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        package_room = self._package_room_or_404(
            package, serializer.validated_data["room_id"]
        )
        package_room.unblock()
        return Response(
            StaffPackageRoomSerializer(package_room).data
        )

    def _package_room_or_404(self, package, room_id):
        package_room = (
            PackageRoom.objects.filter(package=package, room_id=room_id)
            .select_related("room__room_type", "package", "blocked_by")
            .first()
        )
        if package_room is None:
            raise Http404("That room is not attached to this package.")
        return package_room

    @action(detail=True, methods=["get"], url_path="guide-report")
    def guide_report(self, request, pk=None):
        package = self.get_object()
        # ?scope=all → every cabin (booked first, then available); default
        # "booked" → only the cabins the guide collects dues from.
        scope = "all" if request.query_params.get("scope") == "all" else "booked"
        pdf = generate_guide_report_pdf(package, scope=scope)
        suffix = "-all-rooms" if scope == "all" else ""
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="guide-report-{package.start_date}{suffix}.pdf"'
        )
        return response


class StaffBookingViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get_queryset(self):
        qs = (
            Booking.objects.select_related("package__ship")
            .prefetch_related("rooms__room__room_type")
            .order_by("-created_at")
        )
        params = self.request.query_params
        if params.get("package"):
            try:
                qs = qs.filter(package_id=int(params["package"]))
            except ValueError:
                # Non-numeric filter would ValueError inside the ORM → 500.
                raise ValidationError({"package": "Must be a package id."})
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("refund_required") in ("true", "false"):
            # "Refunds owed" queue: cancelled-with-money bookings must be
            # findable, not buried among ordinary cancellations.
            qs = qs.filter(refund_required=params["refund_required"] == "true")
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(
                Q(booking_code__icontains=term)
                | Q(customer_name__icontains=term)
                | Q(phone__icontains=term)
                | Q(email__icontains=term)
            )
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return StaffBookingCreateSerializer
        if self.action in ("update", "partial_update"):
            return StaffBookingUpdateSerializer
        if self.action == "retrieve":
            return StaffBookingDetailSerializer
        return StaffBookingListSerializer

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Aggregate totals across the *entire* filtered set (not just one
        page), so the dashboard's summary cards show true figures that still
        honor the active package/status/search filters.

        Money figures come from ACTIVE bookings only — a cancelled booking is
        not collectable, so folding it in would inflate the totals. Cancelled
        money is reported as its own line (cancelled_paid_amount: deposits
        sitting on cancelled bookings, i.e. the refund conversation)."""
        qs = self.get_queryset()
        active = qs.exclude(status=Booking.Status.CANCELLED)
        agg = active.aggregate(
            total=Sum("total_amount"),
            paid=Sum("paid_amount"),
            due=Sum("due_amount"),
        )
        cancelled_agg = qs.filter(status=Booking.Status.CANCELLED).aggregate(
            paid=Sum("paid_amount")
        )
        by_status = {
            value: 0 for value, _ in Booking.Status.choices
        }
        for row in qs.values("status").annotate(c=Count("pk")):
            by_status[row["status"]] = row["c"]

        count = qs.count()
        active_count = active.count()
        fully_paid = by_status.get(Booking.Status.FULLY_PAID, 0) + by_status.get(
            Booking.Status.COMPLETED, 0
        )
        fully_paid_rate = (
            (Decimal(fully_paid) / Decimal(active_count) * 100).quantize(Decimal("0.1"))
            if active_count > 0
            else Decimal("0.0")
        )

        def money(value):
            return str((value or Decimal("0.00")).quantize(Decimal("0.01")))

        return Response(
            {
                "count": count,
                "total_amount": money(agg["total"]),
                "paid_amount": money(agg["paid"]),
                "due_amount": money(agg["due"]),
                "cancelled_paid_amount": money(cancelled_agg["paid"]),
                "refunds_owed_count": qs.filter(refund_required=True).count(),
                "fully_paid_rate": str(fully_paid_rate),
                "by_status": by_status,
            }
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking = serializer.save()
        return Response(
            BookingPublicSerializer(booking).data, status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        # Return the full detail shape so the dashboard can refresh in place.
        booking = self.get_object()
        return Response(StaffBookingDetailSerializer(booking).data)


class StaffPaymentViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    """List payments + record manual (cash/offline) collections — e.g. dues
    the guide collects on the ship — and resolve payments the gateway would
    never settle (`resolve` action)."""

    permission_classes = [IsAdminUser]
    serializer_class = StaffPaymentSerializer
    pagination_class = StaffPagination

    def get_queryset(self):
        qs = Payment.objects.select_related("booking").order_by("-created_at")
        if self.request.query_params.get("booking"):
            qs = qs.filter(booking_id=self.request.query_params["booking"])
        # The manual-review queue: payments the gateway will not resolve, each
        # of which is holding a cabin out of inventory until staff act (QA H7).
        review = self.request.query_params.get("needs_manual_review")
        if review is not None:
            qs = qs.filter(needs_manual_review=review.lower() in ("1", "true", "yes"))
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Staff resolution of a stuck payment (QA H7).

        A payment escalated to needs_manual_review sits PENDING, and a PENDING
        payment blocks its room from ever being released — so without a human
        control the cabin is lost from inventory permanently. Having checked
        the SSLCommerz merchant panel, staff either close it (no money moved →
        the expiry job reclaims the cabin) or settle it (money moved → credit
        the customer). Both are audited on the payment.
        """
        payment = self.get_object()
        serializer = StaffPaymentResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = payment_service.resolve_payment_manually(
            payment,
            serializer.validated_data["status"],
            staff_user=request.user,
            note=serializer.validated_data.get("note", ""),
        )
        return Response(StaffPaymentSerializer(payment).data)

    def perform_create(self, serializer):
        # The serializer's status/ceiling checks are check-then-act; re-verify
        # under a lock on the booking row so a concurrent gateway settlement
        # (or another staff member) can't slip this collection past the due —
        # an over-recorded amount would put a negative due on the guide's
        # printed collection sheet.
        with transaction.atomic():
            booking = Booking.objects.select_for_update().get(
                pk=serializer.validated_data["booking"].pk
            )
            if booking.status in (
                Booking.Status.CANCELLED,
                Booking.Status.COMPLETED,
            ):
                raise ValidationError(
                    {
                        "booking": (
                            f"This booking is {booking.get_status_display().lower()}"
                            " — payments can no longer be recorded against it."
                        )
                    }
                )
            amount = serializer.validated_data["amount"]
            if amount > booking.due_amount:
                raise ValidationError(
                    {"amount": f"Amount exceeds the due amount ({booking.due_amount})."}
                )
            payment = serializer.save(
                gateway=serializer.validated_data.get("gateway") or "cash",
                status=Payment.Status.SUCCESS,
                paid_at=timezone.now(),
            )
        # Same invoice-per-payment behavior as gateway payments (Phase 5).
        booking = payment.booking
        transaction.on_commit(
            lambda: invoices.create_and_send_invoice(booking, payment=payment)
        )


# These settings viewsets back small, bounded config tables the dashboard shows
# whole and reads as bare arrays; opt out of the project-wide default paginator
# so their response shape stays a plain list (QA phase8b F3). The paginated
# resources (packages/bookings/payments/rooms/invoices) set StaffPagination.
class StaffShipViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Read + edit ship settings (helpline numbers). Ships are created via the
    seed migration / Django admin, so no create or delete here."""

    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffShipSerializer
    queryset = Ship.objects.all().order_by("name")


class StaffRoomTypeViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffRoomTypeSerializer
    queryset = RoomType.objects.all().order_by("max_adults")


class StaffRoomViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = StaffRoomSerializer
    pagination_class = StaffPagination

    def get_queryset(self):
        return Room.objects.select_related("room_type", "ship").order_by(
            "floor_number", "room_number"
        )


class StaffRoomImageViewSet(viewsets.ModelViewSet):
    """Room gallery photos (dashboard Room Photos tab). Upload is multipart
    POST; files land in the configured media storage (Cloudinary CDN in
    production). Unpaginated: the whole fleet's gallery is a bounded set the
    tab reads in one request, optionally narrowed with ?room=<id>."""

    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffRoomImageSerializer

    def get_queryset(self):
        qs = RoomImage.objects.select_related("room").order_by("sort_order", "id")
        if self.request.query_params.get("room"):
            qs = qs.filter(room_id=self.request.query_params["room"])
        return qs


class StaffCabinViewSet(viewsets.ModelViewSet):
    """Showcase cabins for the public /cabins pages — the dashboard's Cabins
    page CRUDs these. Unpaginated: a ship carries a handful of cabin
    categories, read whole by the dashboard."""

    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffCabinSerializer
    queryset = (
        Cabin.objects.select_related("ship", "room_type")
        .prefetch_related("images")
        .order_by("sort_order", "id")
    )


class StaffCabinImageViewSet(viewsets.ModelViewSet):
    """Cabin gallery photos (dashboard Cabins page). Multipart upload like
    room images; ?cabin=<id> narrows to one cabin's gallery. PATCHing
    is_main=true makes a photo the public card image (previous main is
    cleared atomically in the model)."""

    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffCabinImageSerializer

    def get_queryset(self):
        qs = CabinImage.objects.select_related("cabin").order_by(
            "-is_main", "sort_order", "id"
        )
        if self.request.query_params.get("cabin"):
            qs = qs.filter(cabin_id=self.request.query_params["cabin"])
        return qs


class StaffGalleryImageViewSet(viewsets.ModelViewSet):
    """Public-gallery photos (dashboard Gallery page). Multipart upload like
    room/cabin images; staff write a caption on each photo and can hide one
    from the website (is_active=false) without deleting it. Unpaginated: the
    gallery is a bounded set the page reads in one request."""

    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffGalleryImageSerializer
    queryset = GalleryImage.objects.select_related("ship").order_by(
        "sort_order", "id"
    )


class StaffKidPricingRuleViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffKidPricingRuleSerializer
    queryset = KidPricingRule.objects.all().order_by("min_age")


class StaffForeignerSurchargeView(generics.RetrieveUpdateAPIView):
    """GET/PATCH the one global foreigner-surcharge row.

    Not a ViewSet: there is exactly one row and it can be neither created nor
    deleted, so a list/detail router would offer operations that do not exist.
    get_object() creates it on first read, so the settings page never has to
    handle "no row yet".
    """

    permission_classes = [IsAdminUser]
    serializer_class = StaffForeignerSurchargeSerializer

    def get_object(self):
        return ForeignerSurcharge.get_solo()


class StaffFoodMenuItemViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAdminUser]
    pagination_class = None
    serializer_class = StaffFoodMenuItemSerializer
    queryset = FoodMenuItem.objects.select_related("ship").order_by(
        "ship", "day", "meal_type", "order", "id"
    )


class StaffInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = StaffInvoiceSerializer
    pagination_class = StaffPagination

    def get_queryset(self):
        qs = Invoice.objects.select_related("booking").order_by("-created_at")
        if self.request.query_params.get("booking"):
            qs = qs.filter(booking_id=self.request.query_params["booking"])
        return qs

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        """Stream the invoice PDF through the API, behind IsAdminUser.

        The PDF used to be handed out as a raw MEDIA_URL link — served by
        django.views.static.serve with no access check at all under DEBUG, and
        a dead 404 without it (QA C1). It is now only ever reachable through an
        authenticated view.
        """
        invoice = self.get_object()
        if not invoice.pdf_file:
            raise Http404("This invoice has no PDF.")
        return FileResponse(
            invoice.pdf_file.open("rb"),
            content_type="application/pdf",
            filename=f"{invoice.number}.pdf",
        )

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        invoice = self.get_object()
        invoices.send_invoice_email(invoice)
        return Response({"detail": "Invoice email sent."})


class StaffOverviewView(APIView):
    """Aggregate stats for the dashboard landing page."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        today = timezone.localdate()
        week_start = today - timezone.timedelta(days=today.weekday())

        active = Booking.objects.exclude(status=Booking.Status.CANCELLED)
        totals = active.aggregate(paid=Sum("paid_amount"), due=Sum("due_amount"))
        total_collected = totals["paid"] or Decimal("0.00")
        total_due = totals["due"] or Decimal("0.00")
        total_expected = total_collected + total_due
        collection_rate = (
            (total_collected / total_expected * 100).quantize(Decimal("0.1"))
            if total_expected > 0
            else Decimal("0.0")
        )

        upcoming = Package.objects.filter(
            status=Package.Status.OPEN, end_date__gte=today
        ).count()

        # Money the company owes back to customers (cancelled-with-deposit,
        # money settled on dead sessions). As visible as money it is owed —
        # refunds are manual phone calls, so the dashboard is the only nudge.
        refunds_owed = Booking.objects.filter(refund_required=True).aggregate(
            count=Count("pk"), paid_total=Sum("paid_amount")
        )

        # The refund LEDGER, which is a sharper number than the flag above: an
        # exact amount promised and not yet sent, rather than "this booking has
        # money on it and is cancelled". Both are reported — the flag still
        # catches bookings cancelled straight from the Django admin, where
        # nobody has decided a figure.
        from apps.refunds.models import CancellationRequest, Refund

        refund_liability = Refund.objects.filter(
            status=Refund.Status.PENDING
        ).aggregate(count=Count("pk"), total=Sum("amount"))
        pending_cancellations = CancellationRequest.objects.filter(
            status=CancellationRequest.Status.PENDING
        ).aggregate(count=Count("pk"), total=Sum("refund_amount"))

        status_counts = dict(
            Booking.objects.values_list("status").annotate(c=Count("pk")).order_by()
        )
        bookings_by_status = {
            value: status_counts.get(value, 0) for value, _ in Booking.Status.choices
        }

        recent_bookings = [
            {
                "id": b.id,
                "booking_code": b.booking_code,
                "customer_name": b.customer_name,
                "package_title": b.package.marketing_title or str(b.package),
                "room_number": ", ".join(
                    br.room.room_number for br in b.rooms.all()
                ),
                "status": b.status,
                "total_amount": b.total_amount,
                "paid_amount": b.paid_amount,
                "due_amount": b.due_amount,
                "created_at": b.created_at,
            }
            for b in Booking.objects.select_related("package")
            .prefetch_related("rooms__room")
            .order_by("-created_at")[:8]
        ]

        recent_payments = [
            {
                "id": pay.id,
                "booking_code": pay.booking.booking_code,
                "amount": pay.amount,
                "gateway": pay.gateway,
                "paid_at": pay.paid_at,
            }
            for pay in Payment.objects.filter(status=Payment.Status.SUCCESS)
            .select_related("booking")
            .order_by("-paid_at")[:6]
        ]

        by_ship = [
            {
                "ship_id": s.id,
                "ship_name": s.name,
                "upcoming_packages": s.upcoming_packages or 0,
                "active_bookings": s.active_bookings or 0,
                "paid_total": s.paid_total or Decimal("0.00"),
                "due_total": s.due_total or Decimal("0.00"),
            }
            for s in ship_stats_queryset()
        ]

        per_package = []
        for p in package_stats_queryset()[:8]:
            bookings_count = p.bookings_count or 0
            rooms_total = p.rooms_total or 0
            occupancy_pct = (
                (Decimal(bookings_count) / Decimal(rooms_total) * 100).quantize(
                    Decimal("0.1")
                )
                if rooms_total > 0
                else Decimal("0.0")
            )
            per_package.append(
                {
                    "id": p.id,
                    "title": p.marketing_title or str(p),
                    "start_date": p.start_date,
                    "status": p.status,
                    "is_bookable": p.is_bookable(),
                    "paid_total": p.paid_total or Decimal("0.00"),
                    "due_total": p.due_total or Decimal("0.00"),
                    "bookings_count": bookings_count,
                    "occupancy_pct": occupancy_pct,
                }
            )

        return Response(
            {
                "upcoming_packages": upcoming,
                "active_bookings": active.count(),
                "total_collected": total_collected,
                "total_due": total_due,
                "total_revenue_expected": total_expected,
                "collection_rate": collection_rate,
                "pending_payment_bookings": status_counts.get(
                    Booking.Status.PENDING, 0
                ),
                "refunds_owed_count": refunds_owed["count"] or 0,
                "refunds_owed_paid_total": refunds_owed["paid_total"]
                or Decimal("0.00"),
                "refund_liability_count": refund_liability["count"] or 0,
                "refund_liability_total": refund_liability["total"]
                or Decimal("0.00"),
                "pending_cancellation_count": pending_cancellations["count"] or 0,
                "pending_cancellation_refund_total": pending_cancellations["total"]
                or Decimal("0.00"),
                "bookings_today": Booking.objects.filter(
                    created_at__date=today
                ).count(),
                "bookings_this_week": Booking.objects.filter(
                    created_at__date__gte=week_start
                ).count(),
                "bookings_by_status": bookings_by_status,
                "recent_bookings": recent_bookings,
                "recent_payments": recent_payments,
                "by_ship": by_ship,
                "packages": per_package,
            }
        )
