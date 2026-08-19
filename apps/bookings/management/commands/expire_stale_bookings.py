"""Cancel unpaid PENDING bookings past the hold window, freeing their rooms.

Run periodically (cron on Railway), AFTER reconcile_pending_payments. A
booking with ANY successful payment is partially/fully paid and is never
touched here. A booking with ANY still-PENDING payment is also spared — no
matter how old: a PENDING payment means a gateway session was handed to a
customer, and only the gateway can say whether money is coming on it.
Releasing the room on a timer while that question is open is how a room gets
resold underneath a customer whose payment then settles (QA C1). The
reconciliation job is responsible for driving every PENDING payment to a
terminal state via the gateway's Transaction Query API; once it does, the
next run here reclaims the room.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, Payment


def expire_booking(booking_pk, cutoff):
    """Cancel one stale hold. Returns the booking if cancelled, else None.

    Every condition from the scan is re-checked under a row lock: an IPN can
    settle a payment between the scan and this write, and an unguarded
    full-field save would then cancel a paid booking and overwrite its
    paid_amount back to zero. Only status is ever written from here — a
    hold expiry must never touch money fields.
    """
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update().filter(pk=booking_pk).first()
        )
        if (
            booking is None
            or booking.status != Booking.Status.PENDING
            or booking.created_at >= cutoff
            or booking.payments.filter(status=Payment.Status.SUCCESS).exists()
            # An unresolved gateway session — however old — blocks release
            # until reconcile_pending_payments closes it with the gateway.
            or booking.payments.filter(status=Payment.Status.PENDING).exists()
        ):
            return None
        booking.status = Booking.Status.CANCELLED
        booking.save(  # logs the transition in BookingStatusLog
            update_fields=["status", "updated_at"],
            # No cancellation email: this is an abandoned checkout with no money
            # on it (both guarded above), and the visitor never completed a
            # booking to be told about (QA M6).
            silent=True,
        )
        return booking


class Command(BaseCommand):
    help = "Cancel PENDING bookings with no successful payment past the hold window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes",
            type=int,
            default=settings.BOOKING_HOLD_MINUTES,
            help="Hold window in minutes (default: BOOKING_HOLD_MINUTES).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        cutoff = now - timedelta(minutes=options["minutes"])
        stale_ids = list(
            Booking.objects.filter(
                status=Booking.Status.PENDING, created_at__lt=cutoff
            )
            .exclude(payments__status=Payment.Status.SUCCESS)
            .exclude(payments__status=Payment.Status.PENDING)
            .distinct()
            .values_list("pk", flat=True)
        )
        cancelled = 0
        for booking_pk in stale_ids:
            booking = expire_booking(booking_pk, cutoff)
            if booking is not None:
                cancelled += 1
                self.stdout.write(f"cancelled {booking.booking_code}")
        self.stdout.write(self.style.SUCCESS(f"{cancelled} stale booking(s) cancelled."))
