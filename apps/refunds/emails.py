"""Transactional email for the cancellation/refund flow.

Plain-text, in the same register as the balance reminder and cancellation
notices in ``apps.bookings.invoices`` — these are short factual statements about
someone's money, and a customer forwarding one to their bank should see numbers,
not a rendered brochure.

Nothing here may raise. Every send is dispatched after commit, so a mail outage
must never undo a cancellation, a rejection or a recorded payout; the worst
acceptable outcome is a logged failure and a customer who gets told by phone.

The approval path has no function here on purpose: approving cancels the
booking, and ``Booking.save()`` already emails the customer — enriched with the
exact charge and refund figures once a Refund row exists. Two emails about one
event is how people learn to ignore both.
"""

import logging

from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)


def _safe(fn):
    """Run a send, swallow anything. See the module docstring."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("Refund email failed: %s", fn.__name__)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _ship(obj):
    return obj.booking.package.ship


@_safe
def send_request_received(request):
    """Acknowledge a cancellation request, and put it in front of staff.

    The acknowledgement matters more than it looks: the customer has just asked
    to cancel a paid holiday and nothing visible happened (by design — the
    booking is untouched until a human decides). Without this they assume the
    form failed and either submit again or phone in.
    """
    booking = request.booking
    ship = _ship(request)
    package = booking.package
    body = (
        f"Dear {booking.customer_name},\n\n"
        f"We have received your request to cancel booking {booking.booking_code} "
        f"for the {package.start_date:%d %b %Y} departure.\n\n"
        "Based on our cancellation policy at the time of your request:\n\n"
        f"Paid:                 {request.paid_amount} BDT\n"
        f"Cancellation charge:  {request.cancellation_charge} BDT"
        f"  ({request.policy_snapshot.get('charge_percent', '—')}%"
        f" — {request.policy_snapshot.get('tier_label', '')})\n"
        f"Refund due to you:    {request.refund_amount} BDT\n\n"
        "These figures are now fixed for your request and will not change "
        "while we review it.\n\n"
        "Your booking is NOT cancelled yet — our team reviews every request, "
        "usually within one working day, and you will get a confirmation email "
        "either way. Your cabin stays reserved for you until then, so if you "
        "change your mind, just reply to this email.\n\n"
        f"{ship.name}"
    )
    EmailMultiAlternatives(
        subject=f"{ship.name} — cancellation request received ({booking.booking_code})",
        body=body,
        to=[booking.email],
    ).send()
    _notify_staff_new_request(request)


@_safe
def _notify_staff_new_request(request):
    """Nudge the desk. Refunds are money leaving the company, so nobody should
    have to discover the queue by visiting it."""
    recipient = _ship(request).contact_notify_recipient
    if not recipient:
        return
    booking = request.booking
    body = (
        f"Cancellation request — {booking.booking_code}\n\n"
        f"Customer:   {booking.customer_name} ({booking.phone})\n"
        f"Departure:  {booking.package.start_date:%d %b %Y}\n"
        f"Reason:     {request.get_reason_code_display()}\n"
        f"{request.reason_note or ''}\n\n"
        f"Paid:       {request.paid_amount} BDT\n"
        f"Charge:     {request.cancellation_charge} BDT\n"
        f"Refund:     {request.refund_amount} BDT\n\n"
        "Approve or reject it in the staff dashboard. The cabin stays held "
        "until you do."
    )
    EmailMultiAlternatives(
        subject=(
            f"Cancellation request: {booking.booking_code} — "
            f"{request.refund_amount} BDT refund"
        ),
        body=body,
        to=[recipient],
        reply_to=[booking.email] if booking.email else None,
    ).send(fail_silently=True)


@_safe
def send_request_rejected(request):
    """Decline, with the reason staff gave. The booking is untouched, so the
    important thing to convey is that their trip is still on."""
    booking = request.booking
    ship = _ship(request)
    body = (
        f"Dear {booking.customer_name},\n\n"
        f"We were unable to approve your request to cancel booking "
        f"{booking.booking_code}.\n\n"
        f"Reason: {request.decision_note}\n\n"
        "Your booking remains active and your cabin is still reserved — "
        "nothing has changed and no charge has been applied. If you would like "
        "to discuss this, please call us.\n\n"
        f"{ship.name}"
    )
    EmailMultiAlternatives(
        subject=f"{ship.name} — about your cancellation request ({booking.booking_code})",
        body=body,
        to=[booking.email],
    ).send()


@_safe
def send_refund_paid(refund):
    """Confirm the payout, with the transaction reference.

    The reference is the point of the email: it is what the customer quotes back
    when the money has not appeared, and it is what staff match against the
    settlement report.
    """
    booking = refund.booking
    ship = _ship(refund)
    destination = (
        f"{refund.get_method_display()} account ending "
        f"{refund.account_number[-4:]}"
        if refund.account_number
        else refund.get_method_display()
    )
    body = (
        f"Dear {booking.customer_name},\n\n"
        f"We have refunded {refund.amount} BDT against booking "
        f"{booking.booking_code}.\n\n"
        f"Sent to:      {destination}\n"
        f"Reference:    {refund.reference_no}\n"
        f"Date:         {refund.paid_at:%d %b %Y}\n\n"
        "Please allow your provider a little time to post it. If it has not "
        "reached you within a few days, reply to this email quoting the "
        "reference above.\n\n"
        f"{ship.name}"
    )
    EmailMultiAlternatives(
        subject=f"{ship.name} — refund sent ({booking.booking_code})",
        body=body,
        to=[booking.email],
    ).send()
