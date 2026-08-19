from django.db import transaction
from rest_framework import mixins, viewsets
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.throttling import ScopedRateThrottle

from .emails import send_contact_notification
from .models import ContactMessage
from .serializers import (
    ContactMessageCreateSerializer,
    StaffContactMessageSerializer,
)


class ContactMessageCreateView(
    mixins.CreateModelMixin, viewsets.GenericViewSet
):
    """Public contact-form endpoint (create-only).

    Anonymous, so it carries its own tight throttle (`contact` scope) to keep
    the staff inbox from being flooded. On create the message is saved and a
    best-effort notification email is fired after commit — a mail failure never
    fails the submission.
    """

    permission_classes = [AllowAny]
    serializer_class = ContactMessageCreateSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "contact"

    def perform_create(self, serializer):
        message = serializer.save()
        transaction.on_commit(lambda: send_contact_notification(message))


class StaffContactMessageViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Dashboard Messages queue: list/read enquiries, mark read/archived,
    delete. No create — messages only ever come from the public form."""

    permission_classes = [IsAdminUser]
    serializer_class = StaffContactMessageSerializer

    def get_queryset(self):
        qs = ContactMessage.objects.all()
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs
