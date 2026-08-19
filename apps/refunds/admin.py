"""Django admin for the cancellation schedule and the refund ledger.

The schedule is fully editable here — that is the whole point of it being data.
The ledger is not: requests and refunds are decided and paid through the staff
API, which takes locks, writes the audit log and emails the customer. Editing
those rows in the admin would move money with none of that happening, so the
ledger models are registered read-only, for looking things up.
"""

from django.contrib import admin

from .models import CancellationRequest, CancellationRule, Refund, RefundStatusLog


@admin.register(CancellationRule)
class CancellationRuleAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "ship",
        "days_before_start",
        "individual_percent",
        "group_percent",
        "is_active",
    )
    list_filter = ("ship", "is_active")
    ordering = ("ship", "-days_before_start")
    fieldsets = (
        (
            None,
            {
                "fields": ("ship", "days_before_start", "label", "is_active"),
                "description": (
                    "Leave <b>ship</b> blank for the default schedule used by "
                    "every ship. A tier applies when the cancellation lands "
                    "that many whole days or more before departure; use 0 for "
                    "the final-day catch-all. Editing these percentages only "
                    "affects future cancellations — every quote already given "
                    "carries its own frozen copy."
                ),
            },
        ),
        ("Charge", {"fields": ("individual_percent", "group_percent")}),
    )


class ReadOnlyLedgerAdmin(admin.ModelAdmin):
    """Look, don't touch. See the module docstring."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CancellationRequest)
class CancellationRequestAdmin(ReadOnlyLedgerAdmin):
    list_display = (
        "booking",
        "status",
        "source",
        "reason_code",
        "paid_amount",
        "cancellation_charge",
        "refund_amount",
        "requested_at",
    )
    list_filter = ("status", "source", "reason_code")
    search_fields = ("booking__booking_code", "booking__customer_name", "booking__phone")
    date_hierarchy = "requested_at"
    list_select_related = ("booking",)


class RefundStatusLogInline(admin.TabularInline):
    model = RefundStatusLog
    extra = 0
    can_delete = False
    readonly_fields = ("old_status", "new_status", "changed_by", "note", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Refund)
class RefundAdmin(ReadOnlyLedgerAdmin):
    list_display = (
        "booking",
        "reason",
        "amount",
        "status",
        "method",
        "reference_no",
        "created_at",
        "paid_at",
    )
    list_filter = ("status", "reason", "method")
    search_fields = (
        "booking__booking_code",
        "booking__customer_name",
        "booking__phone",
        "reference_no",
    )
    date_hierarchy = "created_at"
    list_select_related = ("booking",)
    inlines = [RefundStatusLogInline]
