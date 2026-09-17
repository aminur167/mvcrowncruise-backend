"""Ask SSLCommerz what became of the refunds we asked it for.

There is no IPN for a refund. SSLCommerz accepts one, shows "Initiate", then
"In processing" for days while the issuing bank works, and the money reaches
the customer at the end of that — and nothing ever tells us. Until this job
runs, "staff requested the refund" is the only fact the register holds, and a
refund the gateway CANCELS looks exactly like one that succeeded.

Cheap and idempotent: it only asks about refunds whose fate is still unknown,
and a refund reported refunded or cancelled is never asked about again. Safe to
run hourly. Run it at least daily, or the cancelled ones are found by the
customer rather than by us.
"""

from django.core.management.base import BaseCommand

from apps.refunds.gateway import poll_gateway_refunds


class Command(BaseCommand):
    help = "Reconcile gateway refunds against SSLCommerz's refund status API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Check at most this many (oldest first).",
        )

    def handle(self, *args, **options):
        checked, refunded, cancelled, failed = poll_gateway_refunds(
            limit=options["limit"]
        )

        if not checked and not failed:
            self.stdout.write("No gateway refunds are waiting on an answer.")
            return

        self.stdout.write(
            f"Checked {checked} refund(s): {refunded} now refunded, "
            f"{cancelled} cancelled, {failed} unreachable."
        )
        if cancelled:
            # Loud, because this is the case where our books are wrong: the
            # register says the customer was paid and the gateway says no.
            self.stdout.write(
                self.style.ERROR(
                    f"{cancelled} refund(s) were CANCELLED at the gateway. Those "
                    f"customers have NOT been paid — settle them by hand."
                )
            )
