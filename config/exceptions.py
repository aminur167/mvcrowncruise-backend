"""DRF's default exception handler serializes APIException.detail but drops
its .code (e.g. RoomUnavailable's "room_unavailable") — the frontend needs
that code to distinguish a lost booking race from other errors. Also maps
ProtectedError (deleting a row that PROTECT FKs point at) to a clean 409."""

from django.db.models import ProtectedError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    if isinstance(exc, ProtectedError):
        return Response(
            {
                "detail": "Cannot delete this — other records (e.g. bookings) still reference it.",
                "code": "protected",
            },
            status=409,
        )
    response = drf_exception_handler(exc, context)
    if response is not None and "code" not in response.data:
        code = getattr(getattr(exc, "detail", None), "code", None)
        if code:
            response.data["code"] = code
    return response
