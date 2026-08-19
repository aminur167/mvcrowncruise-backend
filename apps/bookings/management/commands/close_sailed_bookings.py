"""Close out bookings whose tour has finished (QA M4).

Booking.Status.COMPLETED is read as a terminal state in three places
(initiate_payment, PaymentInitiateSerializer, StaffPaymentSerializer) but was
never set by anything — so a sailed booking stayed FULLY_PAID forever, and a
sailed booking with an unpaid balance stayed PARTIALLY_PAID forever, quietly
inflating the open-receivables figures with history nobody would ever collect.

This job runs after a package's end_date has passed:

- FULLY_PAID  → COMPLETED. The tour was delivered and settled; nothing more
  is owed either way, so the booking leaves the live set.
- PARTIALLY_PAID → left alone, deliberately. The tour WAS delivered, so the
  outstanding balance is a real debt — the guide was meant to collect it in
  cash on board (that is exactly what the guide collection report is for).
  Cancelling it would erase a genuine receivable, so instead it is reported
  here and stays visible in the staff dashboard until someone records the
  cash payment (which flips it to FULLY_PAID, and the next run completes it).

Run on cron daily, after the other jobs.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking, Payment


def _complete(booking_pk):
    """Complete one sailed, fully paid booking under a row lock — a late
    payment or a cancellation can land between the scan and this write."""
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update()
            .select_related("package")
            .filter(pk=booking_pk)
            .first()
        )
        if (
            booking is None
            or booking.status != Booking.Status.FULLY_PAID
            or booking.package.end_date >= timezone.localdate()
            # An unresolved session may still be settling money on it.
            or booking.payments.filter(status=Payment.Status.PENDING).exists()
        ):
            return None
        booking.status = Booking.Status.COMPLETED
        booking.save(update_fields=["status", "updated_at"])
        return booking


class Command(BaseCommand):
    help = (
        "Mark fully paid bookings COMPLETED once their tour has sailed, and "
        "report sailed bookings that still owe a balance. Schedule daily."
    )

    def handle(self, *args, **options):
        today = timezone.localdate()

        sailed = Booking.objects.filter(package__end_date__lt=today).select_related(
            "package"
        )

        completed = 0
        for booking_pk in list(
            sailed.filter(status=Booking.Status.FULLY_PAID).values_list(
                "pk", flat=True
            )
        ):
            if _complete(booking_pk) is not None:
                completed += 1

        # Real money the guide was supposed to collect on board. Never
        # cancelled — the tour was delivered, so the debt is genuine.
        uncollected = sailed.filter(status=Booking.Status.PARTIALLY_PAID)
        for booking in uncollected:
            self.stderr.write(
                f"UNCOLLECTED {booking.booking_code} — sailed "
                f"{booking.package.end_date:%d %b %Y}, still owes "
                f"{booking.due_amount} BDT ({booking.customer_name}, "
                f"{booking.phone})"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{completed} booking(s) completed, "
                f"{uncollected.count()} sailed booking(s) still owing a balance."
            )
        )
