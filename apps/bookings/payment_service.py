"""All money-moving logic in one place: initiate, confirm, fail.

Invariants:
- A Payment row is only ever created by initiate_payment(); IPN/redirects can
  never create one (so forged notifications can't mint credits).
- process_payment_result() is idempotent — duplicate IPNs are no-ops thanks to
  the row lock + status gate, and paid_amount is always recomputed as a SUM.
- Every gateway verdict is persisted to Payment.gateway_payload for audit.
"""

import logging
from decimal import Decimal, InvalidOperation

import requests
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from . import invoices, sslcommerz
from .models import Booking, Payment
from .sslcommerz import GatewayError

logger = logging.getLogger(__name__)


class PaymentGatewayUnavailable(APIException):
    status_code = 502
    default_detail = "Payment gateway is unavailable. Please try again."
    default_code = "gateway_unavailable"


def minimum_first_payment(booking):
    """The smallest acceptable FIRST payment on a booking (Decimal).

    Policy printed on every invoice: confirmation requires a deposit — the
    percentage is admin-configurable per package (Package.min_deposit_percent),
    never a constant. Top-ups toward an existing balance have no floor: once a
    valid deposit exists, any amount that chips away at the due is welcome.
    """
    if booking.paid_amount > 0:
        return Decimal("0.01")
    percent = booking.package.min_deposit_percent
    return (booking.total_amount * percent / Decimal("100")).quantize(Decimal("0.01"))


def balance_deadline_passed(booking):
    """True once the balance-due date has passed for a partially-paid booking.

    Client policy (QA H6): the balance may be settled any time BEFORE the
    journey — a deposit-paid customer is never auto-cancelled, and anything
    still owed is collected on board by the guide. So this does NOT block
    payment: the customer can always pay the balance online right up to
    sailing, and this flag is informational only (the frontend uses it to nudge
    a customer who is past the soft deadline). Online payment is gated solely by
    the booking cutoff (departure day), enforced separately.

    Returns False before the deadline and for any booking with nothing paid.
    """
    if booking.paid_amount <= 0:
        return False
    # Booking is still open for payment right up to departure; only stop taking
    # money once the package itself is no longer bookable (past the cutoff, at
    # sea). Everything in between is the guide's to collect.
    return timezone.now() >= booking.package.balance_due_at()


def _flag_refund_owed(booking_id, note):
    """Persist 'we owe this customer money' on the booking (H3): a queryable
    flag the staff dashboard surfaces, not a log line nobody reads. Must be
    called inside a transaction."""
    booking = Booking.objects.select_for_update().get(pk=booking_id)
    booking.refund_required = True
    booking.refund_note = (
        f"{booking.refund_note}\n{note}" if booking.refund_note else note
    )
    booking.save(update_fields=["refund_required", "refund_note", "updated_at"])


def _raise_refund_for_settled_on_cancelled(booking, payment, tran_id):
    """Ledger entry for money that landed after the booking was cancelled.

    An SSLCommerz session cannot be voided once handed out, so a customer can
    still pay a checkout page belonging to a booking that has since been
    cancelled — by the hold expiring, by staff, or by their own cancellation
    request being approved. That money is ours to give back, and it has to be
    visible where refunds are actually worked: the liability figure and the
    register, not only a note on the booking.

    Never raises. This runs inside the IPN transaction, and a bookkeeping
    problem must not 500 the notification — SSLCommerz would retry forever and
    the flag above has already recorded the condition either way.
    """
    from apps.refunds.models import Refund
    from apps.refunds.services import add_to_open_refund_or_create

    try:
        # Folded into the booking's existing payout when there is one — a
        # cancellation refund is usually already open, and a second row would
        # collide with the one-open-refund-per-booking constraint.
        add_to_open_refund_or_create(
            booking,
            reason=Refund.Reason.OVERPAYMENT,
            amount=payment.amount,
            note=(
                f"Payment {tran_id} settled after this booking was cancelled. "
                "Recorded automatically — the customer paid for a booking that "
                "no longer exists."
            ),
        )
    except Exception:
        logger.exception(
            "Could not raise a refund row for %s on cancelled booking %s; "
            "the refund_required flag still records it.",
            tran_id,
            booking.booking_code,
        )


def initiate_payment(booking, payment_type, amount=None):
    """Create a PENDING Payment and a gateway session; returns (payment, url).

    The amount is decided server-side: full → the current due; partial → the
    serializer-validated amount (0 < amount <= due).

    The serializer's status/due checks are check-then-act, so everything is
    re-verified here under a lock on the booking row — the expiry cron may
    cancel the booking between the two, and a live gateway session for a
    CANCELLED booking would feed real money onto a resold room.

    At most one live gateway session may exist per booking (QA H4). We cannot
    void an SSLCommerz session once handed out, so cancelling the local
    Payment row does NOT stop the customer paying on the checkout page it
    belongs to — that money would be captured and then (correctly) refused as
    a closed session, leaving them paid-up but holding nothing. So instead:

    - an identical live session (same type AND amount) is REUSED — this is
      the common double-click / back-button / reopened-tab case, and handing
      back the same gateway URL is exactly right;
    - a *different* request while a session is live is REFUSED. The customer
      must finish or abandon the open one first; the reconciliation cron
      closes abandoned sessions against the gateway's own answer.
    """
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        if booking.status in (Booking.Status.CANCELLED, Booking.Status.COMPLETED):
            raise ValidationError(
                {"payment_type": "This booking can no longer be paid."}
            )
        if booking.due_amount <= 0:
            raise ValidationError(
                {"payment_type": "Nothing is due on this booking."}
            )
        # A cancellation is awaiting a decision. Taking more money now is the
        # worst possible timing: the booking is about to be cancelled and every
        # taka received becomes refundable, so a payment here manufactures a
        # bigger payout to chase by hand. Withdraw the request to pay.
        if booking.has_pending_cancellation:
            raise ValidationError(
                {
                    "payment_type": (
                        "A cancellation request is pending on this booking. "
                        "Please contact us before making a payment."
                    )
                }
            )
        # Client policy (QA H6): the balance may be paid any time before the
        # journey — a deposit-paid booking is never cancelled for it, and
        # whatever is still owed the guide collects on board. So online balance
        # payment stays open right up to departure; only once the ship has
        # sailed does it stop (the guide handles it from there).
        if timezone.localdate() > booking.package.start_date:
            raise ValidationError(
                {
                    "payment_type": (
                        "This package has already departed — please settle any "
                        "balance with the guide on board."
                    )
                }
            )
        if payment_type == Payment.PaymentType.FULL:
            amount = booking.due_amount
        elif amount is None or amount > booking.due_amount:
            raise ValidationError(
                {"amount": f"Amount exceeds the due amount ({booking.due_amount})."}
            )
        else:
            # The serializer's floor check is check-then-act; re-verify under
            # the lock so a racing settlement can't let a sub-minimum first
            # deposit through. Without a real floor, 0.01 BDT would lock a
            # cabin forever (partially_paid is exempt from hold expiry).
            floor = minimum_first_payment(booking)
            if amount < floor:
                raise ValidationError(
                    {
                        "amount": (
                            f"Minimum first payment is {floor} BDT "
                            f"({booking.package.min_deposit_percent}% of the total)."
                        )
                    }
                )

        live = booking.payments.filter(status=Payment.Status.PENDING).first()
        if live is not None:
            if (
                live.payment_type == payment_type
                and live.amount == amount
                and live.gateway_url
            ):
                # Same request as the live session — hand back the very same
                # checkout page rather than opening a second payable one.
                logger.info(
                    "Reusing live session %s for %s",
                    live.transaction_id,
                    booking.booking_code,
                )
                return live, live.gateway_url
            # A different amount/type while a session is live. Cancelling the
            # row would not stop the customer paying on the old checkout page,
            # so refuse rather than create a second way to take their money.
            raise ValidationError(
                {
                    "payment_type": (
                        f"A payment of {live.amount} BDT is already in progress "
                        "for this booking. Complete or cancel it before starting "
                        "a different one."
                    )
                }
            )

        payment = Payment.objects.create(
            booking=booking,
            amount=amount,
            payment_type=payment_type,
            gateway="sslcommerz",
            status=Payment.Status.PENDING,
        )
        # pk exists only after create — tran_id is unique and booking-traceable.
        payment.transaction_id = f"{booking.booking_code}-P{payment.pk}"
        payment.save(update_fields=["transaction_id"])

    # The gateway HTTP call happens outside the lock — the PENDING payment is
    # committed, so the expiry cron already treats the hold as protected.
    try:
        gateway_url = sslcommerz.create_session(payment)
    except (requests.RequestException, GatewayError, ValueError) as exc:
        payment.status = Payment.Status.FAILED
        payment.gateway_payload = {"error": f"session: {exc}"}
        payment.save()
        logger.error("SSLCommerz session failed for %s: %s", payment.transaction_id, exc)
        raise PaymentGatewayUnavailable()
    # Persist the URL so an identical re-request reuses this exact session.
    payment.gateway_url = gateway_url
    payment.save(update_fields=["gateway_url"])
    return payment, gateway_url


def process_payment_result(tran_id, val_id):
    """Verify a gateway result and credit the payment. Idempotent.

    Called from the IPN listener and the success redirect (both may fire for
    the same payment, possibly concurrently).
    """
    if not tran_id or not val_id:
        return None
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(transaction_id=tran_id)
        except Payment.DoesNotExist:
            logger.warning("Gateway result for unknown tran_id %s", tran_id)
            return None

        if payment.status == Payment.Status.SUCCESS:
            return payment  # duplicate notification — already credited

        if payment.status != Payment.Status.PENDING:
            # Superseded/closed session — never credit it (crediting would
            # double-charge the two-tab customer). But if the gateway says
            # real money landed on it, flag it loudly for a manual refund.
            try:
                data = sslcommerz.validate_payment(val_id)
            except (requests.RequestException, ValueError) as exc:
                logger.error(
                    "Validation API error for closed payment %s: %s", tran_id, exc
                )
                return payment
            payment.gateway_payload = data
            if _verdict_is_valid(payment, tran_id, data):
                payment.gateway_payload = {**data, "requires_refund": True}
                # Not just a log line: flag the booking so the staff
                # dashboard's refunds-owed queue picks it up (H3).
                _flag_refund_owed(
                    payment.booking_id,
                    f"Gateway captured {payment.amount} BDT on closed session "
                    f"{tran_id} — money was NOT credited; refund the customer "
                    "at the gateway.",
                )
                logger.error(
                    "Money received for %s payment %s (booking %s) — NOT "
                    "credited; refund the customer at the gateway.",
                    payment.status,
                    tran_id,
                    payment.booking.booking_code,
                )
            payment.save(update_fields=["gateway_payload"])
            return payment

        try:
            data = sslcommerz.validate_payment(val_id)
        except (requests.RequestException, ValueError) as exc:
            # Leave PENDING: a later IPN retry / re-verify can still settle it.
            logger.error("Validation API error for %s: %s", tran_id, exc)
            return payment

        if _verdict_is_valid(payment, tran_id, data):
            payment.status = Payment.Status.SUCCESS
            payment.paid_at = timezone.now()
        else:
            logger.warning("Rejected gateway verdict for %s: %s", tran_id, data)
            payment.status = Payment.Status.FAILED
        payment.gateway_payload = data
        payment.save()  # SUCCESS → booking paid/due/status refresh (SUM-based)

        if payment.status == Payment.Status.SUCCESS:
            # refresh_paid_amount() synced this instance under the booking
            # row lock, so its status is current for the whole transaction.
            booking = payment.booking
            if booking.status == Booking.Status.CANCELLED:
                # Money-in-flight edge: the hold expired while the customer
                # was paying and the room may already be resold. Persist the
                # condition (H3) — and do NOT email an invoice implying the
                # customer holds a room they no longer have.
                _flag_refund_owed(
                    booking.pk,
                    f"Payment {tran_id} ({payment.amount} BDT) settled AFTER "
                    "this booking was cancelled — the room may be resold. "
                    "Refund or rebook the customer manually.",
                )
                # And put it in the LEDGER, not just on the flag. This is money
                # we are actually holding for a booking that no longer exists,
                # so it has to appear in the refund liability figure and in the
                # register that gets reconciled against the settlement report —
                # a flag with a note appears in neither.
                _raise_refund_for_settled_on_cancelled(booking, payment, tran_id)
                logger.error(
                    "Payment %s settled on CANCELLED booking %s — refund or "
                    "rebook manually.",
                    tran_id,
                    booking.booking_code,
                )
            else:
                # After commit so email trouble can never roll back the
                # payment. Duplicate IPNs never reach here (SUCCESS gate
                # above), so exactly one invoice per settled payment — and the
                # invoice records WHICH payment it attests to.
                settled = payment
                transaction.on_commit(
                    lambda: invoices.create_and_send_invoice(booking, payment=settled)
                )
    return payment


def _verdict_is_valid(payment, tran_id, data):
    """Every check must pass explicitly — anything missing fails closed."""
    try:
        amount_matches = Decimal(str(data.get("amount"))) == payment.amount
    except (InvalidOperation, TypeError):
        amount_matches = False
    return (
        data.get("status") in ("VALID", "VALIDATED")
        and data.get("tran_id") == tran_id
        and data.get("currency") == "BDT"
        and amount_matches
    )


def mark_payment_closed(tran_id, new_status):
    """Close out a PENDING payment. Never touches SUCCESS rows (a stray
    'fail' notification can't undo verified money).

    Only ever call this on a VERIFIED trigger — a signature-checked IPN or a
    gateway answer from query_transaction(). Closing a live session on an
    unverified trigger lets an attacker strand a customer's in-flight money
    on a dead row (QA C5)."""
    if not tran_id:
        return None
    updated = Payment.objects.filter(
        transaction_id=tran_id, status=Payment.Status.PENDING
    ).update(status=new_status)
    if updated:
        logger.info("Payment %s marked %s", tran_id, new_status)
        return Payment.objects.get(transaction_id=tran_id)
    return None


#: Gateway attempt statuses that mean "this session is definitively dead".
_GATEWAY_DEAD_STATUSES = {"FAILED", "CANCELLED", "EXPIRED"}


def resolve_payment_with_gateway(payment, close_unattempted=False):
    """Ask the gateway what really happened on a PENDING payment's tran_id
    and drive the payment to that state. May raise gateway/network errors —
    callers decide whether to swallow (redirect) or report (cron).

    Returns (outcome, payment) where outcome is "settled", "closed" or
    "pending".

    - Any VALID attempt settles through process_payment_result(), i.e. the
      full authenticated re-validation and crediting path.
    - Attempts that are all FAILED/CANCELLED/EXPIRED close the payment.
    - No attempt on record means the customer never reached the gateway's
      pay step; that session may still be live, so it is only closed when
      close_unattempted=True (the reconciliation job passes this once the
      gateway session lifetime has passed).
    """
    attempts = sslcommerz.query_transaction(payment.transaction_id)
    valid = next(
        (
            a
            for a in attempts
            if a.get("status") in ("VALID", "VALIDATED") and a.get("val_id")
        ),
        None,
    )
    if valid:
        return "settled", process_payment_result(
            payment.transaction_id, valid["val_id"]
        )
    if attempts and all(
        a.get("status") in _GATEWAY_DEAD_STATUSES for a in attempts
    ):
        new_status = (
            Payment.Status.CANCELLED
            if all(a.get("status") == "CANCELLED" for a in attempts)
            else Payment.Status.FAILED
        )
        closed = mark_payment_closed(payment.transaction_id, new_status)
        return "closed", closed or payment
    if not attempts and close_unattempted:
        closed = mark_payment_closed(payment.transaction_id, Payment.Status.FAILED)
        return "closed", closed or payment
    return "pending", payment


def record_reconcile_failure(payment, exc, max_attempts):
    """Count a failed gateway query on a PENDING payment; escalate at the cap.

    A payment the gateway will not answer for blocks its room from ever being
    released (expire_stale_bookings refuses to cancel a booking with a PENDING
    payment — that guard is what keeps a paid room from being resold). Left
    alone, the cabin is locked out of inventory forever and nobody is told
    (QA H5). So after max_attempts we stop guessing and hand it to a human.

    Returns True if this call escalated the payment.
    """
    attempts = payment.reconcile_attempts + 1
    escalate = attempts >= max_attempts and not payment.needs_manual_review
    with transaction.atomic():
        Payment.objects.filter(pk=payment.pk).update(
            reconcile_attempts=attempts,
            last_reconcile_error=f"{timezone.now():%Y-%m-%d %H:%M} — {exc}",
            needs_manual_review=escalate or payment.needs_manual_review,
        )
        if escalate:
            _flag_refund_owed(
                payment.booking_id,
                f"Payment {payment.transaction_id} ({payment.amount} BDT) cannot "
                f"be resolved with the gateway after {attempts} attempts "
                f"({exc}). Its room stays held until this is settled. Check the "
                "SSLCommerz merchant panel: if money was taken, credit or refund "
                "it; if not, cancel the booking to release the cabin.",
            )
            logger.error(
                "Payment %s escalated for manual review after %d failed gateway "
                "queries — its room is held out of inventory.",
                payment.transaction_id,
                attempts,
            )
    payment.reconcile_attempts = attempts
    payment.needs_manual_review = escalate or payment.needs_manual_review
    return escalate


def clear_reconcile_failures(payment):
    """The gateway answered — a previous failure streak is no longer relevant."""
    Payment.objects.filter(pk=payment.pk).update(
        reconcile_attempts=0, last_reconcile_error=""
    )


def flag_payment_for_review(tran_id, reason):
    """Hand a payment we cannot process to a human, without raising.

    Called from the IPN handler's catch-all: whatever went wrong, the gateway
    must still get a 200 (an error response makes SSLCommerz retry the same
    poisoned notification forever while real money sits captured), and the
    payment must not silently vanish. Best-effort by construction — it runs on
    an error path and must never raise from there.
    """
    if not tran_id:
        return None
    try:
        payment = Payment.objects.filter(transaction_id=tran_id).first()
        if payment is None or payment.status == Payment.Status.SUCCESS:
            return payment
        with transaction.atomic():
            Payment.objects.filter(pk=payment.pk).update(
                needs_manual_review=True,
                last_reconcile_error=f"{timezone.now():%Y-%m-%d %H:%M} — {reason}",
            )
            _flag_refund_owed(
                payment.booking_id,
                f"Payment {tran_id} ({payment.amount} BDT) could not be "
                f"processed ({reason}). Money may have been captured at the "
                "gateway without being credited — check the SSLCommerz "
                "merchant panel and resolve this payment from the staff "
                "dashboard.",
            )
        return payment
    except Exception:  # pragma: no cover — never raise from an error path
        logger.exception("Could not flag payment %s for review", tran_id)
        return None


def resolve_payment_manually(payment, new_status, staff_user=None, note=""):
    """Staff resolution of a payment the gateway would not settle (QA H7).

    A payment escalated to needs_manual_review is PENDING forever: the expiry
    job refuses to release a room while a PENDING payment exists (that guard is
    what stops a paid room being resold), and the reconciliation job backs off
    from escalated rows. Without a human control the cabin is out of inventory
    permanently. So staff, having checked the SSLCommerz merchant panel, can:

    - CLOSE it (FAILED/CANCELLED) — the gateway confirms no money moved; the
      next expire_stale_bookings run reclaims the cabin; or
    - SETTLE it (SUCCESS) — money did move; credit the customer, which flows
      through refresh_paid_amount() exactly like a gateway settlement.

    Everything is re-checked under the booking row lock.
    """
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == new_status:
            return payment
        if payment.status != Payment.Status.PENDING:
            raise ValidationError(
                {
                    "status": (
                        f"This payment is already {payment.get_status_display().lower()}"
                        " — it cannot be resolved again."
                    )
                }
            )
        if new_status == Payment.Status.SUCCESS:
            booking = Booking.objects.select_for_update().get(pk=payment.booking_id)
            if booking.status == Booking.Status.CANCELLED:
                raise ValidationError(
                    {
                        "status": (
                            "This booking is cancelled and its room may be "
                            "resold — crediting money to it would strand the "
                            "customer. Refund at the gateway instead."
                        )
                    }
                )
            payment.paid_at = timezone.now()
        elif new_status not in (Payment.Status.FAILED, Payment.Status.CANCELLED):
            raise ValidationError(
                {"status": "A payment can only be resolved to success, failed or cancelled."}
            )

        stamp = f"{timezone.now():%Y-%m-%d %H:%M}"
        who = getattr(staff_user, "username", None) or "staff"
        payment.status = new_status
        payment.needs_manual_review = False
        payment.last_reconcile_error = (
            f"{stamp} — resolved manually to {new_status} by {who}"
            + (f": {note}" if note else "")
        )
        payment.gateway_payload = {
            **(payment.gateway_payload or {}),
            "manual_resolution": {
                "status": new_status,
                "by": who,
                "at": stamp,
                "note": note,
            },
        }
        payment.save()  # SUCCESS → booking paid/due/status refresh (SUM-based)
    return payment


def close_payment_from_redirect(tran_id):
    """Browser landed on the fail/cancel redirect. The redirect itself is
    attacker-controllable (anyone can POST it with someone else's tran_id),
    so it is treated as presentation-only: the payment is closed exclusively
    on the gateway's own answer about the session. On gateway trouble the
    payment stays PENDING and the reconciliation cron resolves it later."""
    if not tran_id:
        return None
    payment = Payment.objects.filter(transaction_id=tran_id).first()
    if payment is None or payment.status != Payment.Status.PENDING:
        return payment
    try:
        _, payment = resolve_payment_with_gateway(payment)
    except (requests.RequestException, GatewayError, ValueError) as exc:
        logger.warning(
            "Could not confirm redirect close of %s with the gateway: %s",
            tran_id,
            exc,
        )
    return payment
