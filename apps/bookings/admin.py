from django.contrib import admin, messages
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils.html import format_html

from . import invoice_access
from .invoices import generate_invoice_pdf, send_invoice_email
from .models import Booking, BookingRoom, BookingStatusLog, Invoice, Payment


class BookingRoomInline(admin.TabularInline):
    model = BookingRoom
    extra = 0
    can_delete = False
    # room/pax define the booking; the priced subtotal is computed server-side.
    fields = (
        "room", "adult_count", "kid_details", "foreign_guests",
        "room_subtotal", "is_active",
    )
    readonly_fields = ("room_subtotal", "is_active")

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    can_delete = False
    readonly_fields = (
        "amount",
        "payment_type",
        "gateway",
        "transaction_id",
        "status",
        "paid_at",
    )
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


class BookingStatusLogInline(admin.TabularInline):
    model = BookingStatusLog
    extra = 0
    can_delete = False
    readonly_fields = ("old_status", "new_status", "changed_by", "created_at")
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "booking_code",
        "customer_name",
        "phone",
        "package",
        "total_pax",
        "total_amount",
        "paid_amount",
        "due_amount",
        "status",
        "refund_required",
    )
    list_filter = ("status", "refund_required", "package")
    search_fields = ("booking_code", "customer_name", "phone", "email")
    # Amounts are always computed server-side (pricing service / payments),
    # never entered by hand.
    readonly_fields = ("booking_code", "total_amount", "paid_amount", "due_amount")
    inlines = [BookingRoomInline, PaymentInline, BookingStatusLogInline]
    fieldsets = (
        ("Customer", {"fields": ("customer_name", "phone", "email")}),
        (
            "Trip",
            {"fields": ("package", "special_requests")},
        ),
        (
            "Amounts (auto-calculated)",
            {"fields": ("total_amount", "paid_amount", "due_amount")},
        ),
        ("Status", {"fields": ("status", "booking_code")}),
        (
            "Refund (manual process — clear the flag once the customer is paid)",
            {"fields": ("refund_required", "refund_note")},
        ),
    )

    def save_model(self, request, obj, form, change):
        obj.save(changed_by=request.user)


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "booking",
        "amount",
        "payment_type",
        "gateway",
        "status",
        "paid_at",
    )
    list_filter = ("status", "payment_type", "gateway")
    search_fields = ("booking__booking_code", "transaction_id")
    readonly_fields = ("gateway_payload",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "number", "booking", "total_amount", "paid_amount", "due_amount",
        "booking_status", "sent_at", "pdf_link",
    )
    list_filter = ("sent_via", "booking_status")
    search_fields = ("number", "booking__booking_code")
    # An issued invoice is an immutable financial record: the money it states
    # is frozen at issue time and must never be edited after the fact.
    readonly_fields = (
        "number", "booking", "payment", "total_amount", "paid_amount",
        "due_amount", "booking_status", "pdf_file", "created_at",
    )
    actions = ["resend_email", "regenerate_pdf"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="PDF")
    def pdf_link(self, invoice):
        if invoice.pdf_file:
            # The token-bearing endpoint, not the raw media path (QA C1). The
            # token is signed and expires in 30 minutes, so an admin page left
            # open (or its HTML saved) does not become a permanent key.
            return format_html(
                '<a href="{}" target="_blank">Download</a>',
                reverse(
                    "invoice-download",
                    kwargs={"token": invoice_access.issue(invoice)},
                ),
            )
        return "—"

    @admin.action(description="Resend invoice email")
    def resend_email(self, request, queryset):
        sent = 0
        for invoice in queryset.select_related("booking"):
            try:
                send_invoice_email(invoice)
                sent += 1
            except Exception as exc:
                self.message_user(
                    request, f"{invoice}: {exc}", level=messages.ERROR
                )
        self.message_user(request, f"{sent} invoice email(s) sent.")

    @admin.action(description="Regenerate PDF")
    def regenerate_pdf(self, request, queryset):
        # Safe to re-run: the PDF renders from the invoice's own frozen figures
        # and the booking's price snapshot, so a regenerated document is
        # identical to the one the customer received.
        for invoice in queryset.select_related("booking"):
            invoice.pdf_file.save(
                f"{invoice.number}.pdf",
                ContentFile(generate_invoice_pdf(invoice)),
                save=True,
            )
        self.message_user(request, f"{queryset.count()} PDF(s) regenerated.")
