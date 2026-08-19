import re

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import Http404
from django.urls import include, path, re_path
from rest_framework.routers import DefaultRouter

from apps.bookings.views import (
    BookingViewSet,
    InvoiceDownloadView,
    PaymentCancelView,
    PaymentFailView,
    PaymentIPNView,
    PaymentSuccessView,
)
from apps.contact.views import ContactMessageCreateView
from apps.packages.views import CalendarView, PackageViewSet
from apps.refunds.views import CancellationPolicyView
from apps.ships.views import (
    CabinViewSet,
    GalleryImageViewSet,
    RoomTypeViewSet,
    ShipViewSet,
)

router = DefaultRouter()
router.register("packages", PackageViewSet, basename="package")
router.register("ships", ShipViewSet, basename="ship")
router.register("room-types", RoomTypeViewSet, basename="room-type")
router.register("cabins", CabinViewSet, basename="cabin")
router.register("gallery", GalleryImageViewSet, basename="gallery")
router.register("bookings", BookingViewSet, basename="booking")
router.register("contact-messages", ContactMessageCreateView, basename="contact-message")


def _no_static_invoices(request, path=None):
    """Invoices are never static files. See below."""
    raise Http404("Invoices are only served through an authenticated endpoint.")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/calendar/", CalendarView.as_view(), name="calendar"),
    # The published cancellation-charge schedule, so the policy page and the
    # backend can never disagree about what a cancellation costs.
    path(
        "api/cancellation-policy/",
        CancellationPolicyView.as_view(),
        name="cancellation-policy",
    ),
    path("api/payments/ipn/", PaymentIPNView.as_view(), name="payment-ipn"),
    path("api/payments/success/", PaymentSuccessView.as_view(), name="payment-success"),
    path("api/payments/fail/", PaymentFailView.as_view(), name="payment-fail"),
    path("api/payments/cancel/", PaymentCancelView.as_view(), name="payment-cancel"),
    # Customer's own invoice PDF, authorised by the invoice's capability token.
    path(
        "api/invoices/<str:token>/download/",
        InvoiceDownloadView.as_view(),
        name="invoice-download",
    ),
    path("api/staff/", include("apps.staff.urls")),
    path("api/", include(router.urls)),
]

if settings.DEBUG:
    # Invoice PDFs contain a customer's name, phone, email and payment history.
    # django.views.static.serve applies NO access check, so under DEBUG the
    # whole media tree — invoices included — was world-readable at a guessable
    # path (QA C1). Shadow invoices/ with a 404 BEFORE the static handler, so
    # the only way to a PDF is an authenticated/token-bearing endpoint, in
    # every configuration. Genuinely public media (hero images, logos) is
    # unaffected.
    urlpatterns += [
        re_path(
            r"^%sinvoices/" % re.escape(settings.MEDIA_URL.lstrip("/")),
            _no_static_invoices,
        )
    ]
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
