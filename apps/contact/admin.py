from django.contrib import admin

from .models import ContactMessage


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ("name", "inquiry_type", "email", "phone", "status", "created_at")
    list_filter = ("status", "inquiry_type", "created_at")
    search_fields = ("name", "email", "phone", "message")
    readonly_fields = (
        "name",
        "inquiry_type",
        "email",
        "phone",
        "message",
        "departure_date",
        "guests",
        "created_at",
    )
