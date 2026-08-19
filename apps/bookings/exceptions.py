from rest_framework.exceptions import APIException


class RoomUnavailable(APIException):
    """The requested room was taken (or withheld) — including losing a
    concurrent-booking race. 409 so the frontend can prompt a re-pick."""

    status_code = 409
    default_detail = "Room is no longer available."
    default_code = "room_unavailable"
