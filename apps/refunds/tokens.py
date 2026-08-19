"""Signed quote tokens.

The preview endpoint hands the customer a figure; the submit endpoint has to
know that the figure they clicked "I agree" on is the one it is about to store.
The token is a signed, short-lived record of that quote — not a secret and not
an input: the server recomputes the real numbers regardless, and only uses the
token to detect that they MOVED between the two requests (a tier boundary
crossed at midnight, an admin editing the schedule, a stale browser tab).

When they have moved, the submit is refused and the fresh quote is returned, so
the customer re-confirms against the real amount instead of being silently
charged more than the screen promised.

django.core.signing gives us tamper-proofing and expiry for free; the payload is
signed, not encrypted, and deliberately contains nothing that is not already on
the customer's own screen.
"""

from django.core import signing

SALT = "refunds.cancellation-quote"

#: A preview is good for 30 minutes. Long enough to read the policy page and
#: talk to your spouse; short enough that a tab left open overnight is
#: re-quoted rather than honoured across a tier boundary.
MAX_AGE_SECONDS = 30 * 60


def issue(booking, quote):
    return signing.dumps(
        {
            "code": booking.booking_code,
            "charge": str(quote.cancellation_charge),
            "refund": str(quote.refund_amount),
            "on": quote.computed_on,
        },
        salt=SALT,
    )


class QuoteTokenError(Exception):
    """Token missing, tampered with, expired, or for a different quote."""


def verify(token, booking, quote):
    """Raise unless `token` describes the same money as `quote`."""
    try:
        payload = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.SignatureExpired as exc:
        raise QuoteTokenError(
            "Your cancellation quote has expired. Please review the updated "
            "figures and confirm again."
        ) from exc
    except signing.BadSignature as exc:
        raise QuoteTokenError(
            "This cancellation quote is not valid. Please start again."
        ) from exc

    if payload.get("code") != booking.booking_code:
        raise QuoteTokenError("This quote belongs to a different booking.")
    if payload.get("charge") != str(quote.cancellation_charge) or payload.get(
        "refund"
    ) != str(quote.refund_amount):
        raise QuoteTokenError(
            "The cancellation charge has changed since you were quoted "
            "(the amount depends on how close the departure is). Please review "
            "the updated figures and confirm again."
        )
