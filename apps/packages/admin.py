from django.contrib import admin, messages
from django.http import HttpResponse

from .models import ForeignerSurcharge, KidPricingRule, Package, PackageRoom


class PackageRoomInline(admin.TabularInline):
    model = PackageRoom
    extra = 0


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "start_date",
        "end_date",
        "booking_cutoff_datetime",
        "status",
        "is_booking_open",
        "is_bookable",
    )
    list_filter = ("status", "ship", "is_booking_open")
    date_hierarchy = "start_date"
    inlines = [PackageRoomInline]
    actions = ["close_booking", "reopen_booking", "generate_rooms", "guide_report"]
    fieldsets = (
        (None, {"fields": ("ship", "start_date", "end_date", "adult_price")}),
        (
            "Booking control",
            {"fields": ("status", "is_booking_open", "booking_cutoff_datetime")},
        ),
        (
            "Displayed duration",
            {
                "fields": ("duration_days", "duration_nights"),
                "description": (
                    "Leave both blank to auto-calculate from the dates "
                    "(nights = end − start, days = nights + 1). Set to override "
                    "the '3 Days · 2 Nights' text on the public package card."
                ),
            },
        ),
        (
            "Marketing (public website)",
            {
                "fields": (
                    "marketing_title",
                    "marketing_description",
                    "hero_image",
                    "highlights",
                ),
                "description": "Optional — shown on the public /packages page.",
            },
        ),
    )

    @admin.action(description="Close booking (manual override)")
    def close_booking(self, request, queryset):
        updated = queryset.update(is_booking_open=False)
        self.message_user(request, f"Booking closed for {updated} package(s).")

    @admin.action(description="Reopen booking (manual override)")
    def reopen_booking(self, request, queryset):
        updated = queryset.update(is_booking_open=True)
        self.message_user(request, f"Booking reopened for {updated} package(s).")

    @admin.action(description="Download guide collection report (PDF)")
    def guide_report(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request, "Select exactly one package.", level=messages.ERROR
            )
            return None
        from apps.bookings.reports import generate_guide_report_pdf

        package = queryset.first()
        response = HttpResponse(
            generate_guide_report_pdf(package), content_type="application/pdf"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="guide-report-{package.start_date}.pdf"'
        )
        return response

    @admin.action(description="Generate package rooms from ship's rooms")
    def generate_rooms(self, request, queryset):
        created = 0
        for package in queryset:
            for room in package.ship.rooms.all():
                _, was_created = PackageRoom.objects.get_or_create(
                    package=package, room=room
                )
                created += was_created
        self.message_user(request, f"Created {created} package room(s).")


@admin.register(KidPricingRule)
class KidPricingRuleAdmin(admin.ModelAdmin):
    list_display = ("__str__", "min_age", "max_age", "charge_type", "amount")
    list_editable = ("min_age", "max_age", "amount")


@admin.register(ForeignerSurcharge)
class ForeignerSurchargeAdmin(admin.ModelAdmin):
    """Singleton — the admin must offer edit only. "Add" would appear to make a
    second policy row (save() would silently overwrite the first), and "delete"
    would leave pricing with no policy to read."""

    list_display = ("__str__", "adult_amount", "kid_amount", "updated_at")

    def has_add_permission(self, request):
        return not ForeignerSurcharge.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
