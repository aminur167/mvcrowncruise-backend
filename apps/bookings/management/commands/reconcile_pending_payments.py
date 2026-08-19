"""Drive stale PENDING payments to a terminal state via the gateway.

A payment can strand in PENDING when the IPN's Validation API call times out,
when the IPN never arrives (and its retries fail), or when the customer walks
away from the checkout page. This job asks SSLCommerz's Transaction Query API
(by tran_id — no val_id needed) what actually happened and settles or closes
each one, so:

- settled money is never invisible (the customer paid; credit it), and
- a room hold is only ever released after the gateway has definitively
  confirmed no money is coming (expire_stale_bookings refuses to cancel any
  booking that still has an unreconciled PENDING payment).

Run on cron BEFORE (and more often than) expire_stale_bookings.
"""

from datetime import timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.bookings import payment_service
from apps.bookings.models import Payment
from apps.bookings.sslcommerz import GatewayError


class Command(BaseCommand):
    help = (
        "Settle or close stale PENDING payments by querying the gateway. "
        "Schedule before expire_stale_bookings."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--grace-minutes",
            type=int,
            default=5,
            help=(
                "Leave payments younger than this alone — the customer is "
                "probably still mid-checkout and the IPN will do the work."
            ),
        )
        parser.add_argument(
            "--max-attempts",
            type=int,
            default=settings.PAYMENT_MAX_RECONCILE_ATTEMPTS,
            help=(
                "After this many consecutive gateway failures on one payment, "
                "escalate it for manual review (it is then retried on a slow "
                "back-off rather than abandoned)."
            ),
        )
        parser.add_argument(
            "--escalated-retry-minutes",
            type=int,
            default=settings.PAYMENT_ESCALATED_RETRY_MINUTES,
            help=(
                "How long to wait between gateway queries for a payment already "
                "escalated for manual review. It keeps being retried because a "
                "gateway outage ends, and a payment nobody asks about holds its "
                "cabin out of inventory forever."
            ),
        )

    def handle(self, *args, **options):
        now = timezone.now()
        grace_cutoff = now - timedelta(minutes=options["grace_minutes"])
        # Sessions older than the gateway session lifetime with NO attempt on
        # record can be closed outright — nothing can settle on them anymore.
        session_cutoff = now - timedelta(minutes=settings.PAYMENT_SESSION_MINUTES)

        max_attempts = options["max_attempts"]

        # Escalated payments are NOT abandoned — they are backed off.
        #
        # Excluding them outright was wrong (QA H7): a payment stuck PENDING
        # blocks its cabin from ever being released (expire_stale_bookings
        # refuses to cancel a booking with a PENDING payment), so if we also
        # stop querying it, the room is out of inventory forever even after the
        # gateway comes back up. Gateway outages end. So keep retrying escalated
        # rows, just rarely — a recovered gateway auto-resolves the backlog, and
        # only genuinely undecidable payments wait for the human who has already
        # been told about them.
        retry_cutoff = now - timedelta(minutes=options["escalated_retry_minutes"])
        stale = (
            Payment.objects.filter(
                status=Payment.Status.PENDING,
                created_at__lt=grace_cutoff,
            )
            .filter(
                Q(needs_manual_review=False)
                | Q(needs_manual_review=True, last_reconcile_at__isnull=True)
                | Q(needs_manual_review=True, last_reconcile_at__lt=retry_cutoff)
            )
            .order_by("created_at")
        )

        settled = closed = left = errors = escalated = 0
        for payment in stale:
            # Stamp the attempt BEFORE making it, so a payment that makes the
            # gateway hang cannot be re-picked on every run and starve the rest.
            Payment.objects.filter(pk=payment.pk).update(last_reconcile_at=now)
            try:
                outcome, _ = payment_service.resolve_payment_with_gateway(
                    payment,
                    close_unattempted=payment.created_at < session_cutoff,
                )
            except (requests.RequestException, GatewayError, ValueError) as exc:
                errors += 1
                self.stderr.write(
                    f"gateway error for {payment.transaction_id}: {exc}"
                )
                if payment_service.record_reconcile_failure(
                    payment, exc, max_attempts
                ):
                    escalated += 1
                    self.stderr.write(
                        self.style.ERROR(
                            f"ESCALATED {payment.transaction_id}: gateway has "
                            f"failed {max_attempts}x — flagged for manual review "
                            "in the staff dashboard (resolve it there to release "
                            "its room). Still retried on a slow back-off."
                        )
                    )
                continue
            # A successful query clears any earlier failure streak.
            if payment.reconcile_attempts:
                payment_service.clear_reconcile_failures(payment)
            if outcome == "settled":
                settled += 1
                self.stdout.write(f"settled {payment.transaction_id}")
            elif outcome == "closed":
                closed += 1
                self.stdout.write(f"closed {payment.transaction_id}")
            else:
                left += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{settled} settled, {closed} closed, {left} still pending, "
                f"{errors} gateway error(s), {escalated} escalated."
            )
        )
        # A payment stuck PENDING for a day is holding a cabin hostage. This
        # should always be zero; make it impossible to not notice if it isn't.
        stuck = Payment.objects.filter(
            status=Payment.Status.PENDING,
            created_at__lt=now - timedelta(days=1),
        ).count()
        if stuck:
            self.stderr.write(
                self.style.ERROR(
                    f"ALERT: {stuck} payment(s) still PENDING after 24h — each "
                    "is holding a room out of inventory. Review them in the "
                    "staff dashboard (needs_manual_review)."
                )
            )
