"""SSLCommerz HTTP client — the only module that talks to the gateway.

Endpoints and credentials come from settings (which read .env); nothing is
hardcoded, and switching sandbox → live is a .env change only.
"""

import hashlib
import hmac

import requests
from django.conf import settings


class GatewayError(Exception):
    """Session creation or validation could not be completed."""


#: Field lengths SSLCommerz's integration document specifies for the session
#: request. Exceeding one has the whole session refused, with a message that
#: does not name the offending field — so they are enforced here instead.
_NAME_MAX = 50
_EMAIL_MAX = 50
_PHONE_MAX = 20


#: Cardholder-data fields SSLCommerz returns that we neither use nor want to
#: persist. The PAN is already masked by the gateway (PCI), but we still keep no
#: card data at rest: it is surfaced to every staff user via the payment API and
#: the dashboard needs none of it. Verification (_verdict_is_valid) reads only
#: status/tran_id/currency/amount, so dropping these cannot affect crediting
#: (Phase 8a, F4).
_CARD_DATA_FIELDS = frozenset(
    {
        "card_no",
        "card_issuer",
        "card_brand",
        "card_sub_brand",
        "card_type",
        "card_category",
        "card_issuer_country",
        "card_issuer_country_code",
    }
)


def _strip_card_fields(data):
    """Return a copy of a gateway response dict with cardholder-data fields
    removed. Non-dicts pass through unchanged. Applied at the gateway boundary
    so card data never reaches Payment.gateway_payload or the staff API."""
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if key not in _CARD_DATA_FIELDS}


def _fit(value, limit):
    """Trim a session field to the length SSLCommerz accepts.

    The integration document caps several customer fields; an over-length value
    has the whole session refused, so an ordinary long name would stop a booking
    dead. Trimming is only safe for fields that are labels on a receipt — see
    create_session for the one field that is refused rather than trimmed.
    """
    text = (value or "").strip()
    return text[:limit]


def create_session(payment):
    """Create a gateway checkout session; returns the GatewayPageURL."""
    booking = payment.booking
    # An address that no longer identifies the customer is worse than a refused
    # session: a shortened email is simply the wrong address, and the receipt
    # and every later notice would go nowhere. Refuse it with an explanation.
    if len(booking.email or "") > _EMAIL_MAX:
        raise GatewayError(
            f"The email address on this booking is longer than the {_EMAIL_MAX} "
            "characters the payment gateway accepts. Please use a shorter "
            "address and try again."
        )
    payload = {
        "store_id": settings.SSLCOMMERZ_STORE_ID,
        "store_passwd": settings.SSLCOMMERZ_STORE_PASSWORD,
        "total_amount": str(payment.amount),
        "currency": "BDT",
        "tran_id": payment.transaction_id,
        "success_url": f"{settings.BACKEND_URL}/api/payments/success/",
        "fail_url": f"{settings.BACKEND_URL}/api/payments/fail/",
        "cancel_url": f"{settings.BACKEND_URL}/api/payments/cancel/",
        "ipn_url": f"{settings.BACKEND_URL}/api/payments/ipn/",
        "cus_name": _fit(booking.customer_name, _NAME_MAX),
        "cus_email": booking.email,
        "cus_phone": _fit(booking.phone, _PHONE_MAX),
        "cus_add1": "N/A",
        "cus_city": "N/A",
        "cus_country": "Bangladesh",
        "shipping_method": "NO",
        # Floored at 1: a booking with no rooms attached would send 0, and the
        # gateway refuses the session outright — costing the customer their
        # payment to buy us an accurate line on a report.
        "num_of_item": max(1, booking.rooms.count()),
        "product_name": f"Ship package {booking.booking_code}",
        "product_category": "Travel",
        # "general" needs no vertical-specific extra fields (travel-vertical
        # demands hotel_name etc., which don't fit a ship tour).
        "product_profile": "general",
        "value_a": booking.booking_code,
    }
    response = requests.post(settings.SSLCOMMERZ_SESSION_URL, data=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "SUCCESS" or not data.get("GatewayPageURL"):
        raise GatewayError(data.get("failedreason") or "Gateway session failed.")
    return data["GatewayPageURL"]


def validate_payment(val_id):
    """Server-to-server validation of a payment by val_id.

    This authenticated outbound call is the ONLY source of truth about a
    payment's outcome — IPN/redirect POST data is never trusted directly.
    """
    response = requests.get(
        settings.SSLCOMMERZ_VALIDATION_URL,
        params={
            "val_id": val_id,
            "store_id": settings.SSLCOMMERZ_STORE_ID,
            "store_passwd": settings.SSLCOMMERZ_STORE_PASSWORD,
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    return _strip_card_fields(response.json())


def query_transaction(tran_id):
    """Look up a transaction at the gateway by OUR tran_id (no val_id needed).

    Used by the reconciliation job and the fail/cancel redirects: it answers
    "did any money actually move on this session?" straight from the gateway,
    so PENDING payments can be settled or closed definitively even when the
    IPN (and its val_id) never arrived. Returns the list of attempt records
    (possibly empty — the customer may never have attempted payment).
    """
    response = requests.get(
        settings.SSLCOMMERZ_TXN_QUERY_URL,
        params={
            "tran_id": tran_id,
            "store_id": settings.SSLCOMMERZ_STORE_ID,
            "store_passwd": settings.SSLCOMMERZ_STORE_PASSWORD,
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("APIConnect") != "DONE":
        raise GatewayError(data.get("APIConnect") or "Transaction query failed.")
    if data.get("no_of_trans_found") in (0, "0", None) and not data.get("element"):
        return []
    element = data.get("element") or []
    # The API returns a bare object when exactly one attempt exists.
    attempts = [element] if isinstance(element, dict) else list(element)
    return [_strip_card_fields(attempt) for attempt in attempts]


def query_refund_status(refund_ref_id):
    """Ask the gateway what became of a refund, by its refund_ref_id.

    SSLCommerz's refund is not an event, it is a process: the merchant panel
    shows "Initiate" / "In processing" for days while the issuing bank works,
    and only then does the money reach the customer. Nothing pushes that change
    to us — there is no IPN for refunds — so it has to be asked for.

    Returns the response dict. `status` is one of:

        processing  — accepted, money not back yet
        refunded    — the customer has it
        cancelled   — it is NOT happening, and somebody has to know

    Raises GatewayError when the API itself could not be reached or refused
    the request, which is different from a refund that is merely still in
    flight — the caller must not treat one as the other.
    """
    response = requests.get(
        settings.SSLCOMMERZ_REFUND_URL,
        params={
            "refund_ref_id": refund_ref_id,
            "store_id": settings.SSLCOMMERZ_STORE_ID,
            "store_passwd": settings.SSLCOMMERZ_STORE_PASSWORD,
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("APIConnect") != "DONE":
        raise GatewayError(
            data.get("errorReason") or data.get("APIConnect") or "Refund query failed."
        )
    return _strip_card_fields(data)


def verify_ipn_signature(data):
    """Check the verify_sign/verify_key hash SSLCommerz sends with every IPN.

    Scheme (per SSLCommerz docs): verify_key names the POSTed params covered
    by the hash; those key=value pairs plus store_passwd=md5(store password)
    are sorted by key, joined with '&', and MD5-hashed to produce verify_sign.

    The IPN endpoint is unauthenticated by nature — this signature is what
    proves a notification genuinely came from SSLCommerz. Anything failing
    here must be ignored BEFORE any state change (a forged status=FAILED
    could otherwise kill a live payment session).
    """
    verify_sign = data.get("verify_sign")
    verify_key = data.get("verify_key")
    if not verify_sign or not verify_key:
        return False
    keys = [key for key in str(verify_key).split(",") if key]
    if not keys:
        return False
    # verify_key NAMES the fields covered by the hash; one that is absent from
    # the POST is not hashed at all. Defaulting it to "" instead adds a bare
    # "key=" term the gateway never signed, and every such IPN is rejected as
    # forged — the payment then sits PENDING while the gateway retries.
    pairs = {key: str(data[key]) for key in keys if key in data}
    pairs["store_passwd"] = hashlib.md5(
        settings.SSLCOMMERZ_STORE_PASSWORD.encode()
    ).hexdigest()
    signable = "&".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    expected = hashlib.md5(signable.encode()).hexdigest()
    return hmac.compare_digest(expected, str(verify_sign).lower())
