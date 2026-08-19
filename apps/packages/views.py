from calendar import monthrange
from datetime import date, timedelta

from django.db.models import Exists, OuterRef
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.bookings.models import BookingRoom
from apps.ships.models import Cabin

from .models import Package, PackageRoom
from .serializers import (
    PackageDetailSerializer,
    PackageListSerializer,
    PackageRoomSerializer,
)


def _cabin_images_by_room_type(ship):
    """Showcase photos for this ship's cabin types, keyed by room type.

    Built in ONE query and handed to the serializer, because the alternative is
    a lookup per cabin — 31 extra queries on the availability endpoint, which is
    the most-hit read in the whole app.

    Only active cabins count: an inactive one is hidden from the showcase pages,
    so it should not quietly reappear as a booking preview.
    """
    cabins = (
        Cabin.objects.filter(ship=ship, is_active=True)
        .prefetch_related("images")
        .order_by("sort_order", "id")
    )
    by_type = {}
    for cabin in cabins:
        # First active cabin of a type wins; ordering above makes that stable.
        by_type.setdefault(cabin.room_type_id, list(cabin.images.all()))
    return by_type


class PackageViewSet(viewsets.ReadOnlyModelViewSet):
    # Public list is a small, bounded set (a few open sailings) the frontend
    # reads as a bare array; opt out of the project-wide default paginator so
    # its response stays a plain list (QA phase8b F3). The default protects
    # future endpoints; this one is intentionally whole.
    pagination_class = None
    # Read-only browsing + the availability (`rooms`) search get the generous
    # `read` bucket, not the shared 100/min anon one (QA phase8b F1).
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"

    def get_queryset(self):
        return Package.objects.public().select_related("ship").order_by("start_date")

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PackageDetailSerializer
        return PackageListSerializer

    @action(detail=True)
    def rooms(self, request, pk=None):
        package = self.get_object()
        # A room is booked if any active booking holds it. is_active mirrors
        # "still held" (cancelling frees it), so this needs no status exclude.
        active_booking = BookingRoom.objects.filter(
            package_id=OuterRef("package_id"),
            room_id=OuterRef("room_id"),
            is_active=True,
        )
        package_rooms = (
            PackageRoom.objects.filter(package=package)
            .select_related("room__room_type")
            .prefetch_related("room__images")
            .annotate(is_booked=Exists(active_booking))
            .order_by("room__floor_number", "room__room_number")
        )
        rooms = list(package_rooms)
        # Only reach for the room-type fallback when some cabin actually lacks
        # its own photos. `room.images.all()` is prefetched above, so deciding
        # this costs nothing, and a fully photographed deck pays no extra query
        # at all. This is the app's most-hit read.
        needs_fallback = any(not pr.room.images.all() for pr in rooms)
        serializer = PackageRoomSerializer(
            rooms,
            many=True,
            context={
                "cabin_images_by_room_type": (
                    _cabin_images_by_room_type(package.ship) if needs_fallback else {}
                )
            },
        )
        return Response(serializer.data)


class CalendarView(APIView):
    """Monthly calendar data: which dates have a package (PRD §5.3).

    GET /api/calendar/?year=2026&month=8 — defaults to the current month.
    Every day of a package's start–end range that falls inside the requested
    month is listed, so packages spanning a month boundary show up in both.
    """

    # Read-only browsing endpoint — same generous bucket as package/availability
    # browsing rather than the shared anon budget (QA phase8b F1).
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"

    def get(self, request):
        # Asia/Dhaka "today", like every other availability decision — the
        # server OS clock (UTC on Railway) lags Dhaka by 6 hours.
        today = timezone.localdate()
        try:
            year = int(request.query_params.get("year", today.year))
            month = int(request.query_params.get("month", today.month))
            if not (1 <= month <= 12 and 2000 <= year <= 2100):
                raise ValueError
        except (TypeError, ValueError):
            return Response(
                {"detail": "Invalid year/month."}, status=400
            )

        first_day = date(year, month, 1)
        last_day = date(year, month, monthrange(year, month)[1])

        packages = (
            Package.objects.public()
            .filter(start_date__lte=last_day, end_date__gte=first_day)
            .select_related("ship")
            .order_by("start_date")
        )

        dates = {}
        for package in packages:
            entry = {
                "id": package.id,
                "ship_name": package.ship.name,
                "start_date": package.start_date.isoformat(),
                "end_date": package.end_date.isoformat(),
                "is_bookable": package.is_bookable(),
            }
            day = max(package.start_date, first_day)
            stop = min(package.end_date, last_day)
            while day <= stop:
                dates.setdefault(day, []).append(entry)
                day += timedelta(days=1)

        return Response(
            {
                "year": year,
                "month": month,
                "dates": [
                    {"date": day.isoformat(), "packages": entries}
                    for day, entries in sorted(dates.items())
                ],
            }
        )
