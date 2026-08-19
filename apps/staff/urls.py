from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.contact.views import StaffContactMessageViewSet

from .refunds_views import (
    StaffBookingCancelView,
    StaffCancellationRequestViewSet,
    StaffCancellationRuleViewSet,
    StaffDepartureCancelView,
    StaffRefundViewSet,
)
from .views import (
    StaffBookingViewSet,
    StaffCabinImageViewSet,
    StaffCabinViewSet,
    StaffFoodMenuItemViewSet,
    StaffForeignerSurchargeView,
    StaffGalleryImageViewSet,
    StaffInvoiceViewSet,
    StaffKidPricingRuleViewSet,
    StaffLoginView,
    StaffLogoutView,
    StaffOverviewView,
    StaffPackageViewSet,
    StaffPaymentViewSet,
    StaffRoomImageViewSet,
    StaffRoomTypeViewSet,
    StaffRoomViewSet,
    StaffShipViewSet,
    StaffTokenRefreshView,
)

router = DefaultRouter()
router.register("ships", StaffShipViewSet, basename="staff-ship")
router.register("packages", StaffPackageViewSet, basename="staff-package")
router.register("bookings", StaffBookingViewSet, basename="staff-booking")
router.register("payments", StaffPaymentViewSet, basename="staff-payment")
router.register("room-types", StaffRoomTypeViewSet, basename="staff-room-type")
router.register("rooms", StaffRoomViewSet, basename="staff-room")
router.register("room-images", StaffRoomImageViewSet, basename="staff-room-image")
router.register("cabins", StaffCabinViewSet, basename="staff-cabin")
router.register("cabin-images", StaffCabinImageViewSet, basename="staff-cabin-image")
router.register("gallery-images", StaffGalleryImageViewSet, basename="staff-gallery-image")
router.register("kid-pricing-rules", StaffKidPricingRuleViewSet, basename="staff-kid-rule")
router.register("food-menu-items", StaffFoodMenuItemViewSet, basename="staff-food-menu-item")
router.register("invoices", StaffInvoiceViewSet, basename="staff-invoice")
router.register(
    "contact-messages", StaffContactMessageViewSet, basename="staff-contact-message"
)
router.register(
    "cancellation-requests",
    StaffCancellationRequestViewSet,
    basename="staff-cancellation-request",
)
router.register("refunds", StaffRefundViewSet, basename="staff-refund")
router.register(
    "cancellation-rules",
    StaffCancellationRuleViewSet,
    basename="staff-cancellation-rule",
)

urlpatterns = [
    path("login/", StaffLoginView.as_view(), name="staff-login"),
    path("login/refresh/", StaffTokenRefreshView.as_view(), name="staff-token-refresh"),
    path("logout/", StaffLogoutView.as_view(), name="staff-logout"),
    path("overview/", StaffOverviewView.as_view(), name="staff-overview"),
    # Singleton: one row, no create/delete — a plain detail route, not a
    # router registration that would advertise a list and a POST.
    path(
        "foreigner-surcharge/",
        StaffForeignerSurchargeView.as_view(),
        name="staff-foreigner-surcharge",
    ),
    # Cancelling on a customer's behalf, and cancelling a whole sailing. Both
    # are declared before the router so their suffixes resolve ahead of the
    # generic detail routes.
    path(
        "bookings/<int:pk>/cancel/",
        StaffBookingCancelView.as_view(),
        name="staff-booking-cancel",
    ),
    path(
        "packages/<int:pk>/cancel-departure/",
        StaffDepartureCancelView.as_view(),
        name="staff-package-cancel-departure",
    ),
    path("", include(router.urls)),
]
