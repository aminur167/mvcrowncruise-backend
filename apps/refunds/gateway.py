"""Reconciling our refund register against what SSLCommerz actually did.

A gateway refund is a process, not an event: SSLCommerz accepts it ("Initiate"),
the issuing bank works through it ("In processing"), and days later the money
reaches the customer. There is no IPN for any of that — nothing tells us when
it lands, or when it is abandoned — so it has to be asked for.

Without asking, our register showed only that staff had requested the refund,
and a refund the gateway later CANCELLED was indistinguishable from one that
succeeded. That is the failure this module exists to stop: books that say a
customer was paid when they were not.

The gateway is the authority here, and this module never writes money. It only
records what the gateway reports and leaves the amounts alone.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.bookings.sslcommerz import GatewayError, query_refund_status

from .models import GatewayRefundStatus, PayoutMethod, Refund

logger = logging.getLogger(__name__)

#: What SSLCommerz's `status` field can say, mapped onto ours. Anything not
#: listed is treated as unknown and left alone rather than guessed at — a
#: refund wrongly marked complete is worse than one left unconfirmed.
_STATUS_MAP = {
    "refunded": GatewayRefundStatus.REFUNDED,
    "processing": GatewayRefundStatus.PROCESSING,
    "cancelled": GatewayRefundStatus.CANCELLED,
    "canceled": GatewayRefundStatus.CANCELLED,
}


def pending_gateway_refunds():
    """Refunds settled through the gateway whose fate is not yet known.

    A refund already reported `refunded` or `cancelled` is finished — those are
    terminal at the gateway and asking again would only spend a request.
    """
    return (
        Refund.objects.filter(method=PayoutMethod.GATEWAY)
        .exclude(reference_no="")
        .exclude(
            gateway_refund_status__in=[
                GatewayRefundStatus.REFUNDED,
                GatewayRefundStatus.CANCELLED,
            ]
        )
        .select_related("booking")
        .order_by("created_at")
    )


def _parse_refunded_at(value):
    """SSLCommerz's `refunded_on`, as a datetime, or None.

    It arrives as "2026-09-18 03:05:40" in Bangladesh time, and is absent (or
    the literal "In processing") while the refund is still in flight.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not text[0].isdigit():
        return None
    parsed = timezone.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


@transaction.atomic
def apply_gateway_status(refund, data):
    """Record what the gateway reported about one refund. Returns the status.

    Only ever writes the gateway's own account of things. It does not move the
    refund's own status: staff asking for a refund and the bank completing it
    are two different facts, and collapsing them would lose the one this
    module was built to surface.
    """
    locked = Refund.objects.select_for_update().get(pk=refund.pk)
    reported = str(data.get("status", "")).strip().lower()
    mapped = _STATUS_MAP.get(reported, "")

    locked.gateway_checked_at = timezone.now()
    locked.gateway_payload = data
    if mapped:
        locked.gateway_refund_status = mapped
    if mapped == GatewayRefundStatus.REFUNDED:
        locked.gateway_refunded_at = _parse_refunded_at(
            data.get("refunded_on")
        ) or timezone.now()

    locked.save(
        update_fields=[
            "gateway_refund_status",
            "gateway_refunded_at",
            "gateway_checked_at",
            "gateway_payload",
            "updated_at",
        ]
    )

    if mapped == GatewayRefundStatus.CANCELLED:
        # The one outcome nobody can be allowed to miss: the register says this
        # customer was paid, and the gateway has just said they were not.
        logger.error(
            "Refund %s on booking %s was CANCELLED at the gateway (ref %s) — "
            "the customer has NOT been paid and the register still says they "
            "were. Reason: %s",
            locked.pk,
            locked.booking.booking_code,
            locked.reference_no,
            data.get("errorReason") or "not given",
        )
    elif not mapped:
        logger.warning(
            "Refund %s: gateway answered with an unrecognised status %r — "
            "left unconfirmed rather than guessed at.",
            locked.pk,
            reported,
        )

    return locked.gateway_refund_status


def poll_gateway_refunds(limit=None):
    """Ask the gateway about every unfinished gateway refund.

    Returns (checked, refunded, cancelled, failed). One refund's network
    failure must not stop the rest: a gateway that times out on the first of
    twenty would otherwise leave nineteen unasked.
    """
    refunds = pending_gateway_refunds()
    if limit:
        refunds = refunds[:limit]

    checked = refunded = cancelled = failed = 0
    for refund in refunds:
        try:
            data = query_refund_status(refund.reference_no)
        except (GatewayError, OSError, ValueError) as exc:
            failed += 1
            logger.warning(
                "Refund %s (ref %s): could not reach the gateway — %s",
                refund.pk,
                refund.reference_no,
                exc,
            )
            continue

        status = apply_gateway_status(refund, data)
        checked += 1
        if status == GatewayRefundStatus.REFUNDED:
            refunded += 1
        elif status == GatewayRefundStatus.CANCELLED:
            cancelled += 1

    return checked, refunded, cancelled, failed
