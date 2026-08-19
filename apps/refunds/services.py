"""State transitions for cancellations and refunds.

Every function here is the ONLY supported way to move the state it owns, and
every one of them takes the booking's row lock first. Two staff working the same
queue, or a customer double-submitting the confirm screen, must not be able to
cancel a booking twice or raise two payouts for the same money — the partial
unique constraints in models.py are the backstop, these locks are the front
door.

Nothing here recomputes a customer's quote from the live schedule: the figures
frozen on the request at submission time are what get honoured. The single
exception is documented in ``approve_cancellation`` (money that arrived AFTER
the quote), and it can only ever move the refund up.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.bookings.models import Booking

from . import policy
from .models import CancellationRequest, Refund, RefundStatusLog

ZERO = Decimal("0.00")


# --- Cancellation requests -------------------------------------------------


@transaction.atomic
def create_cancellation_request(
    booking,
    *,
    reason_code,
    reason_note="",
    refund_method="",
    refund_account_name="",
    refund_account_number="",
    bank_name="",
    branch_name="",
    source=CancellationRequest.Source.CUSTOMER,
    user=None,
):
    """Queue a cancellation request, or cancel outright when no money is on it.

    The quote is recomputed HERE, under the booking's lock, and it is that
    server-side figure that gets stored — whatever the customer saw on the
    preview screen is a display, never an input. A booking that crossed a tier
    boundary between preview and submit is quoted correctly, and the caller
    compares the two so the customer can be shown the change instead of being
    silently charged more (see the serializer's quote check).

    A booking with nothing paid needs no approval and no refund: it is cancelled
    on the spot, which releases the cabin immediately for resale rather than
    parking a dead hold in a human queue.
    """
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    quote = policy.quote_cancellation(booking)
    if not quote.allowed:
        raise ValidationError(
            {"detail": f"This booking cannot be cancelled ({quote.block_reason})."}
        )

    request = CancellationRequest(
        booking=booking,
        source=source,
        reason_code=reason_code,
        reason_note=reason_note,
        booking_type=booking.booking_type,
        total_amount=quote.total_amount,
        paid_amount=quote.paid_amount,
        cancellation_charge=quote.cancellation_charge,
        refund_amount=quote.refund_amount,
        forfeited_amount=quote.forfeited_amount,
        shortfall_amount=quote.shortfall_amount,
        policy_snapshot=quote.to_snapshot(),
        refund_method=refund_method,
        refund_account_name=refund_account_name,
        refund_account_number=refund_account_number,
        bank_name=bank_name,
        branch_name=branch_name,
    )

    if not quote.requires_approval:
        # Nothing was ever paid — no money decision to make.
        request.status = CancellationRequest.Status.APPROVED
        request.decided_at = timezone.now()
        request.decided_by = user
        request.decision_note = "Auto-approved: no payment on this booking."
        request.save()
        _cancel_booking(booking, user=user)
        return request

    request.save()
    _notify_request_received(request)
    return request


@transaction.atomic
def approve_cancellation(request, *, user, note=""):
    """Approve a pending request: cancel the booking and raise the payout.

    Returns the Refund, or None when there is nothing to pay back.
    """
    request = (
        CancellationRequest.objects.select_for_update()
        .select_related("booking")
        .get(pk=request.pk)
    )
    if request.status != CancellationRequest.Status.PENDING:
        raise ValidationError({"detail": "This request has already been decided."})

    booking = Booking.objects.select_for_update().get(pk=request.booking_id)

    # Money that landed AFTER the quote was frozen — the customer settled their
    # balance while the request sat in the queue. The CHARGE stays frozen (that
    # is the promise), but every taka received beyond it is still theirs, so the
    # refund is re-derived from the live paid_amount against the frozen charge.
    # This can only ever move the refund up.
    refund_amount = max(booking.paid_amount - request.cancellation_charge, ZERO)
    if refund_amount != request.refund_amount:
        request.refund_amount = policy.q2(refund_amount)
        request.forfeited_amount = policy.q2(
            min(booking.paid_amount, request.cancellation_charge)
        )
        request.shortfall_amount = policy.q2(
            max(request.cancellation_charge - booking.paid_amount, ZERO)
        )
        request.paid_amount = booking.paid_amount

    request.status = CancellationRequest.Status.APPROVED
    request.decided_at = timezone.now()
    request.decided_by = user
    request.decision_note = note
    request.save()

    refund = None
    if request.refund_amount > ZERO:
        refund = create_refund(
            booking,
            reason=Refund.Reason.CUSTOMER_CANCELLATION,
            amount=request.refund_amount,
            cancellation_charge=request.cancellation_charge,
            policy_snapshot=request.policy_snapshot,
            cancellation_request=request,
            method=request.refund_method,
            account_name=request.refund_account_name,
            account_number=request.refund_account_number,
            bank_name=request.bank_name,
            branch_name=request.branch_name,
            user=user,
            note="Raised on approval of the customer's cancellation request.",
        )

    # Last, so the cancellation email (fired on commit by Booking.save) can read
    # the refund row and quote the real figures instead of "we will contact you".
    _cancel_booking(booking, user=user)
    return refund


@transaction.atomic
def reject_cancellation(request, *, user, note):
    """Decline a request. The booking is untouched — it never left its status,
    so there is nothing to restore."""
    request = CancellationRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != CancellationRequest.Status.PENDING:
        raise ValidationError({"detail": "This request has already been decided."})
    if not note.strip():
        raise ValidationError(
            {"decision_note": "Give the customer a reason for the rejection."}
        )
    request.status = CancellationRequest.Status.REJECTED
    request.decided_at = timezone.now()
    request.decided_by = user
    request.decision_note = note
    request.save()
    _notify_request_rejected(request)
    return request


@transaction.atomic
def staff_cancel_booking(
    booking,
    *,
    user,
    reason_code,
    reason_note="",
    waive_charge=False,
    refund_method="",
    refund_account_name="",
    refund_account_number="",
    bank_name="",
    branch_name="",
):
    """Cancel on the customer's behalf — the phone call, not the web form.

    There is no approval step: a member of staff IS the approval. The record is
    still written as a CancellationRequest so the register, the reports and the
    customer's history look identical however the cancellation arrived.

    ``waive_charge`` drops the cancellation charge to zero — the documented
    escape hatch for a genuine case the schedule handles badly (a bereavement,
    our own error). It demands a note, because a waiver is money given away and
    the register has to say who decided that and why.
    """
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    quote = policy.quote_cancellation(booking, ignore_pending=True)
    if not quote.allowed:
        raise ValidationError(
            {"detail": f"This booking cannot be cancelled ({quote.block_reason})."}
        )
    if waive_charge and not reason_note.strip():
        raise ValidationError(
            {"reason_note": "A waived cancellation charge must be justified."}
        )

    charge = ZERO if waive_charge else quote.cancellation_charge
    refund_amount = policy.q2(max(booking.paid_amount - charge, ZERO))
    snapshot = quote.to_snapshot()
    if waive_charge:
        snapshot["charge_waived"] = True
        snapshot["waived_by"] = getattr(user, "username", "")
        snapshot["cancellation_charge"] = str(ZERO)
        snapshot["refund_amount"] = str(refund_amount)

    # Any request the customer had open is superseded by the staff decision —
    # leaving it pending would keep the booking looking un-decided forever and
    # trip the one-open-request constraint on the row we are about to write.
    for open_request in booking.cancellation_requests.filter(
        status=CancellationRequest.Status.PENDING
    ):
        open_request.status = CancellationRequest.Status.WITHDRAWN
        open_request.decided_at = timezone.now()
        open_request.decided_by = user
        open_request.decision_note = "Superseded by a staff cancellation."
        open_request.save()

    request = CancellationRequest.objects.create(
        booking=booking,
        source=CancellationRequest.Source.STAFF,
        reason_code=reason_code,
        reason_note=reason_note,
        booking_type=booking.booking_type,
        total_amount=booking.total_amount,
        paid_amount=booking.paid_amount,
        cancellation_charge=charge,
        refund_amount=refund_amount,
        forfeited_amount=policy.q2(min(booking.paid_amount, charge)),
        shortfall_amount=policy.q2(max(charge - booking.paid_amount, ZERO)),
        policy_snapshot=snapshot,
        refund_method=refund_method,
        refund_account_name=refund_account_name,
        refund_account_number=refund_account_number,
        bank_name=bank_name,
        branch_name=branch_name,
        status=CancellationRequest.Status.APPROVED,
        decided_at=timezone.now(),
        decided_by=user,
        decision_note="Cancelled by staff.",
    )

    if refund_amount > ZERO:
        create_refund(
            booking,
            reason=Refund.Reason.CUSTOMER_CANCELLATION,
            amount=refund_amount,
            cancellation_charge=charge,
            policy_snapshot=snapshot,
            cancellation_request=request,
            method=refund_method,
            account_name=refund_account_name,
            account_number=refund_account_number,
            bank_name=bank_name,
            branch_name=branch_name,
            user=user,
            note=reason_note or "Cancelled by staff.",
        )
    _cancel_booking(booking, user=user)
    return request


def _cancel_booking(booking, *, user=None):
    """Cancel through Booking.save() so every existing side effect still runs:
    room release, the status audit log, the refund-owed flag and the customer's
    cancellation email."""
    booking.status = Booking.Status.CANCELLED
    booking.save(changed_by=user)
    return booking


# --- Refunds ---------------------------------------------------------------


@transaction.atomic
def create_refund(
    booking,
    *,
    reason,
    amount,
    user=None,
    cancellation_charge=ZERO,
    policy_snapshot=None,
    cancellation_request=None,
    method="",
    account_name="",
    account_number="",
    bank_name="",
    branch_name="",
    note="",
    allow_outside_claim_window=False,
):
    """Raise a payout liability against a booking.

    `amount` is always supplied by a caller that computed it server-side — the
    policy engine for cancellations, paid−total for overpayments, a human for
    goodwill. It is checked against what the booking actually received: a refund
    can never exceed paid_amount, whatever the reason, because money that was
    never taken cannot be given back.
    """
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    amount = policy.q2(amount)
    if amount <= ZERO:
        raise ValidationError({"amount": "A refund must be greater than zero."})
    if amount > booking.paid_amount:
        raise ValidationError(
            {
                "amount": (
                    f"Refund of {amount} exceeds the {booking.paid_amount} BDT "
                    "actually received on this booking."
                )
            }
        )
    outstanding = _open_refund_total(booking)
    if amount + outstanding > booking.paid_amount:
        raise ValidationError(
            {
                "amount": (
                    "This booking already has refunds pending or paid totalling "
                    f"{outstanding} BDT; {amount} more would return more than was paid."
                )
            }
        )
    if not allow_outside_claim_window:
        within, days_since = policy.refund_claim_state(booking)
        if not within:
            raise ValidationError(
                {
                    "detail": (
                        f"This sailing ended {days_since} days ago, beyond the "
                        "claim window. Raise it with the override if it is genuine."
                    )
                }
            )

    refund = Refund.objects.create(
        booking=booking,
        cancellation_request=cancellation_request,
        reason=reason,
        amount=amount,
        cancellation_charge=policy.q2(cancellation_charge),
        policy_snapshot=policy_snapshot or {},
        method=method,
        account_name=account_name,
        account_number=account_number,
        bank_name=bank_name,
        branch_name=branch_name,
        note=note,
        created_by=user,
    )
    RefundStatusLog.objects.create(
        refund=refund,
        old_status="",
        new_status=refund.status,
        changed_by=user,
        note=note or f"Raised ({refund.get_reason_display()}).",
    )
    # Keep the flag the staff dashboard already keys on in step with the ledger.
    if not booking.refund_required:
        booking.refund_required = True
        booking.save(update_fields=["refund_required", "updated_at"])
    return refund


@transaction.atomic
def add_to_open_refund_or_create(booking, *, reason, amount, note, user=None):
    """Fold more owed money into the booking's open payout, or start one.

    Used when money arrives that nobody expected — a gateway session settling
    after the booking was already cancelled. A second `create_refund` would hit
    the one-open-refund-per-booking constraint and the amount would be lost to
    a log line, so an existing PENDING row is increased instead and the change
    is written to its status log.

    Only a PENDING refund is ever topped up. Once a payout has been PAID it is
    history: more money owed after that is a new row, never an edit.
    """
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    amount = policy.q2(amount)
    open_refund = booking.refunds.filter(status=Refund.Status.PENDING).first()
    if open_refund is None:
        return create_refund(
            booking,
            reason=reason,
            amount=amount,
            note=note,
            user=user,
            allow_outside_claim_window=True,
        )

    previous = open_refund.amount
    increased = policy.q2(previous + amount)
    if increased > booking.paid_amount:
        raise ValidationError(
            {
                "amount": (
                    f"Increasing this refund to {increased} would return more "
                    f"than the {booking.paid_amount} BDT received."
                )
            }
        )
    open_refund.amount = increased
    open_refund.note = f"{open_refund.note}\n{note}" if open_refund.note else note
    open_refund.save(update_fields=["amount", "note", "updated_at"])
    RefundStatusLog.objects.create(
        refund=open_refund,
        old_status=open_refund.status,
        new_status=open_refund.status,
        changed_by=user,
        note=f"Increased from {previous} to {increased} BDT. {note}",
    )
    return open_refund


@transaction.atomic
def mark_refund_paid(refund, *, user, method, reference_no, note=""):
    """Record that the money actually went out.

    `reference_no` is mandatory: a payout with no bKash/bank transaction id
    cannot be reconciled against the settlement report, which makes the refund
    register worthless as an accounting document.
    """
    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.status != Refund.Status.PENDING:
        raise ValidationError({"detail": "This refund is no longer pending."})
    if not method:
        raise ValidationError({"method": "Record how the money was sent."})
    if not reference_no.strip():
        raise ValidationError(
            {"reference_no": "Enter the transaction id of the payout."}
        )

    old_status = refund.status
    refund.status = Refund.Status.PAID
    refund.method = method
    refund.reference_no = reference_no.strip()
    refund.processed_by = user
    refund.paid_at = timezone.now()
    if note:
        refund.note = f"{refund.note}\n{note}" if refund.note else note
    refund.save(
        update_fields=[
            "status",
            "method",
            "reference_no",
            "processed_by",
            "paid_at",
            "note",
            "updated_at",
        ]
    )
    RefundStatusLog.objects.create(
        refund=refund,
        old_status=old_status,
        new_status=refund.status,
        changed_by=user,
        note=f"Paid via {refund.get_method_display()}, ref {refund.reference_no}.",
    )
    _clear_flag_if_settled(refund.booking_id)
    _notify_refund_paid(refund)
    return refund


@transaction.atomic
def void_refund(refund, *, user, note):
    """Cancel a payout that should never have been raised (duplicate row, wrong
    booking). Paid refunds are never voided — money that has left cannot be
    un-sent; correct it with a new record instead."""
    refund = Refund.objects.select_for_update().get(pk=refund.pk)
    if refund.status != Refund.Status.PENDING:
        raise ValidationError(
            {"detail": "Only a pending refund can be voided; this one is not."}
        )
    if not note.strip():
        raise ValidationError({"note": "Say why this refund is being voided."})
    old_status = refund.status
    refund.status = Refund.Status.VOID
    refund.processed_by = user
    refund.note = f"{refund.note}\n{note}" if refund.note else note
    refund.save(update_fields=["status", "processed_by", "note", "updated_at"])
    RefundStatusLog.objects.create(
        refund=refund,
        old_status=old_status,
        new_status=refund.status,
        changed_by=user,
        note=note,
    )
    _clear_flag_if_settled(refund.booking_id)
    return refund


def _open_refund_total(booking):
    """Everything already promised or paid on this booking, so a second refund
    cannot push the total past what was received."""
    from django.db.models import Sum

    return booking.refunds.filter(
        status__in=(Refund.Status.PENDING, Refund.Status.PAID)
    ).aggregate(total=Sum("amount"))["total"] or ZERO


def _clear_flag_if_settled(booking_id):
    """Drop Booking.refund_required once nothing is outstanding.

    The flag predates this ledger and the dashboard still reads it, so it is
    maintained rather than replaced — but the ledger is the truth, and the flag
    is only allowed to go down when the ledger says nothing is pending.
    """
    booking = Booking.objects.select_for_update().get(pk=booking_id)
    still_open = booking.refunds.filter(status=Refund.Status.PENDING).exists()
    if not still_open and booking.refund_required:
        booking.refund_required = False
        booking.save(update_fields=["refund_required", "updated_at"])


# --- Operator (involuntary) cancellation -----------------------------------


@transaction.atomic
def cancel_departure(package, *, user, reason_note, dry_run=False):
    """Cancel a whole sailing: weather, a technical fault, or too few passengers.

    Involuntary, so the tier schedule does not apply — every booking gets back
    everything it paid. Returns a summary the caller can show as a confirmation
    preview (dry_run=True) or as the result.
    """
    if not reason_note.strip():
        raise ValidationError(
            {"reason_note": "Record why the departure is being cancelled."}
        )

    bookings = list(
        Booking.objects.select_for_update()
        .filter(package=package)
        .exclude(status__in=(Booking.Status.CANCELLED, Booking.Status.COMPLETED))
        .select_related("package", "package__ship")
    )
    summary = {
        "package_id": package.pk,
        "bookings": len(bookings),
        "pax": sum(b.total_pax for b in bookings),
        "refund_total": policy.q2(sum((b.paid_amount for b in bookings), ZERO)),
        "refunds_raised": 0,
    }
    if dry_run:
        return summary

    for booking in bookings:
        quote = policy.operator_cancellation_quote(booking, note=reason_note)
        if quote.refund_amount > ZERO:
            create_refund(
                booking,
                reason=Refund.Reason.OPERATOR_CANCELLATION,
                amount=quote.refund_amount,
                policy_snapshot=quote.to_snapshot(),
                user=user,
                note=f"Departure cancelled: {reason_note}",
                allow_outside_claim_window=True,
            )
            summary["refunds_raised"] += 1
        _cancel_booking(booking, user=user)

    package.status = package.Status.CANCELLED
    package.is_booking_open = False
    package.save(update_fields=["status", "is_booking_open", "updated_at"])
    return summary


# --- Notification hooks ----------------------------------------------------
# Kept as thin wrappers so a mail failure can never roll back a state change:
# each one is dispatched after the transaction commits.


def _notify_request_received(request):
    from . import emails

    transaction.on_commit(lambda: emails.send_request_received(request))


def _notify_request_rejected(request):
    from . import emails

    transaction.on_commit(lambda: emails.send_request_rejected(request))


def _notify_refund_paid(refund):
    from . import emails

    transaction.on_commit(lambda: emails.send_refund_paid(refund))
