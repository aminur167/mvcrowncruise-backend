from rest_framework import serializers

from .models import ContactMessage


class ContactMessageCreateSerializer(serializers.ModelSerializer):
    """Public contact-form submission. Only the customer-supplied fields are
    writable — status/created_at are server-owned."""

    class Meta:
        model = ContactMessage
        fields = [
            "id",
            "name",
            "email",
            "phone",
            "inquiry_type",
            "message",
            "departure_date",
            "guests",
        ]
        # Optional on submission; the model default ("general") applies when the
        # customer leaves it unset.
        extra_kwargs = {"inquiry_type": {"required": False}}

    def validate(self, attrs):
        # A lead we can't reply to is useless — require at least one channel
        # back to the customer. (Both blank=True on the model so either alone
        # is fine.)
        if not attrs.get("email") and not attrs.get("phone"):
            raise serializers.ValidationError(
                "Please provide an email or phone number so we can reply."
            )
        return attrs


class StaffContactMessageSerializer(serializers.ModelSerializer):
    """Full record for the dashboard's Messages queue. Staff may only change
    the status (new / read / archived); everything else is what the customer
    submitted and is read-only."""

    inquiry_type_display = serializers.CharField(
        source="get_inquiry_type_display", read_only=True
    )

    class Meta:
        model = ContactMessage
        fields = [
            "id",
            "name",
            "email",
            "phone",
            "inquiry_type",
            "inquiry_type_display",
            "message",
            "departure_date",
            "guests",
            "status",
            "created_at",
        ]
        read_only_fields = [
            "name",
            "email",
            "phone",
            "inquiry_type",
            "message",
            "departure_date",
            "guests",
            "created_at",
        ]
