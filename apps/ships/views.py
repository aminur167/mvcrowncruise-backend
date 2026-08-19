from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import Cabin, GalleryImage, RoomType, Ship
from .serializers import (
    CabinDetailSerializer,
    CabinListSerializer,
    FoodMenuSerializer,
    GalleryImageSerializer,
    RoomTypeSerializer,
    ShipLayoutSerializer,
    ShipSerializer,
)


class ShipViewSet(viewsets.ReadOnlyModelViewSet):
    # Tiny bounded catalog the frontend reads as a bare array — opt out of the
    # project-wide default paginator (QA phase8b F3). Read-only browsing, so the
    # generous `read` throttle bucket, not the shared anon one (QA phase8b F1).
    pagination_class = None
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"
    queryset = Ship.objects.filter(status=Ship.Status.ACTIVE)
    serializer_class = ShipSerializer

    @action(detail=True)
    def layout(self, request, pk=None):
        ship = self.get_object()
        serializer = ShipLayoutSerializer(ship, context={"request": request})
        return Response(serializer.data)

    @action(detail=True, url_path="food-menu")
    def food_menu(self, request, pk=None):
        ship = self.get_object()
        serializer = FoodMenuSerializer(ship, context={"request": request})
        return Response(serializer.data)


class CabinViewSet(viewsets.ReadOnlyModelViewSet):
    """Public cabin showcase for the /cabins pages — staff-managed marketing
    content (name, features, gallery). Looked up by slug so the frontend URL
    (/cabins/premier-balcony-suite) maps straight onto the API. Price-free by
    design: pricing/availability belong to the booking flow."""

    # Small bounded catalog read as a bare array; read-only browsing bucket.
    pagination_class = None
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"
    lookup_field = "slug"
    queryset = (
        Cabin.objects.filter(is_active=True)
        .select_related("room_type")
        .prefetch_related("images")
    )

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CabinDetailSerializer
        return CabinListSerializer


class GalleryImageViewSet(viewsets.ReadOnlyModelViewSet):
    """Public /gallery page photos — staff-managed content (image + caption).
    Active images only; hidden ones stay in the dashboard but never render
    on the website."""

    # Bounded set the page reads in one request as a bare array; read-only
    # browsing bucket like the other public catalogs.
    pagination_class = None
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"
    serializer_class = GalleryImageSerializer
    queryset = GalleryImage.objects.filter(is_active=True)


class RoomTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """Public catalog of room types (2/3/4-Person Room) for marketing pages
    like /cabins — availability is always package-specific, so this is
    static catalog data only (base_price, pax limits), never bookings."""

    # Static catalog (2/3/4-person), read as a bare array by the frontend —
    # opt out of the project-wide default paginator (QA phase8b F3), and use the
    # generous read-only throttle bucket (QA phase8b F1).
    pagination_class = None
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "read"
    queryset = RoomType.objects.all().order_by("max_adults")
    serializer_class = RoomTypeSerializer
