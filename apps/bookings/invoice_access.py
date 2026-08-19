"""Short-lived download links for invoice PDFs.

An invoice carries a permanent 256-bit ``access_token``. That token names the
PDF in storage, and it used to be handed out in the download URL as well — one
link, valid forever. Unguessable and scoped to a single invoice, so not a hole:
but a link that never expires is a bearer credential to a document holding a
customer's name, phone, email and payment history, and URLs leak in ways their
holders do not intend — browser history on a shared machine, a forwarded mail,
a screenshot, the reverse proxy's access log.

So the permanent token stays private (storage key only) and the URL now carries
a SIGNED, EXPIRING token instead. A leaked link is dead within the window, and
what remains in a log file is a spent credential.

The customer's emailed invoice is unaffected: that email ATTACHES the PDF and
has never contained a link, so nothing older can break.
"""

from django.core import signing

SALT = "bookings.invoice-download"

#: 30 minutes, matching the cancellation quote token. Long enough that a
#: download page left open while someone finds their bank details still works;
#: short enough that a link which escapes is worthless by the time it does.
MAX_AGE_SECONDS = 30 * 60


def issue(invoice):
    """A signed, expiring download token for this invoice.

    Signs the primary key, not the access_token: there is no reason to put the
    permanent secret inside a value we hand out, even signed.
    """
    return signing.dumps({"id": invoice.pk}, salt=SALT)


class InvoiceLinkExpired(Exception):
    """The signature was ours and valid, but the window has closed."""


def resolve_id(token):
    """Signed token → invoice pk.

    Raises ``InvoiceLinkExpired`` for a genuine link that has aged out (the
    caller can say so, because the holder did nothing wrong), and returns None
    for anything else — a forged or mangled token gets the same blank 404 as a
    token for an invoice that does not exist.
    """
    try:
        payload = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
    except signing.SignatureExpired as exc:
        raise InvoiceLinkExpired() from exc
    except signing.BadSignature:
        return None
    return payload.get("id")


def harden(response):
    """Response headers for a document containing personal data.

    ``as_attachment`` is set by the caller; these stop the PDF being cached by
    a shared proxy, indexed, or leaking its own URL through a Referer header.
    """
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    return response
