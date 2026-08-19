"""Remind partially-paid customers to settle their balance before departure.

Policy (client decision, QA H6): the remaining balance may be settled any time
"before the journey" — a customer who paid the required deposit is NOT
auto-cancelled for an unpaid balance. Anything still owed at sailing time is
collected on board by the guide (that is exactly what the guide collection
report exists for). So this job only ever *reminds*; it never cancels.

The deadline is data — noon, Package.balance_due_days_before_start days before
departure (admin-tunable per sailing). For partially paid bookings this job
sends ONE reminder email as that deadline approaches (BALANCE_DUE_REMINDER_DAYS
before it). It does not change any booking's status or free any room.

Bookings whose tour has already departed are left alone: the guide's collection
report handles on-board dues.

Run on cron (daily is enough; the deadline granularity is a day).
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.bookings import invoices
from apps.bookings.models import Booking


class Command(BaseCommand):
    help = (
        "Remind partially paid customers as their balance deadline approaches. "
        "Never cancels — unpaid balances are collected on board by the guide "
        "(client policy, QA H6). Schedule daily."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        today = timezone.localdate()
        reminder_window = timedelta(days=settings.BALANCE_DUE_REMINDER_DAYS)

        open_balances = Booking.objects.filter(
            status=Booking.Status.PARTIALLY_PAID,
            package__start_date__gte=today,
        ).select_related("package", "package__ship")

        reminded = 0
        for booking in open_balances:
            deadline = booking.package.balance_due_at()
            # One reminder, in the window leading up to the deadline. Past the
            # deadline we do nothing — the booking stands and the guide collects
            # the balance on board.
            if (
                now >= deadline - reminder_window
                and booking.due_reminder_sent_at is None
            ):
                try:
                    invoices.send_balance_reminder_email(booking)
                except Exception as exc:  # email trouble must not stop the run
                    self.stderr.write(
                        f"reminder failed for {booking.booking_code}: {exc}"
                    )
                    continue
                Booking.objects.filter(pk=booking.pk).update(
                    due_reminder_sent_at=now
                )
                reminded += 1
                self.stdout.write(f"reminded {booking.booking_code}")

        self.stdout.write(
            self.style.SUCCESS(f"{reminded} balance reminder(s) sent.")
        )
