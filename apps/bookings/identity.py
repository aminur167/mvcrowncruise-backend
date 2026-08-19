"""Proving you are the customer whose booking this is.

There are no customer accounts: a booking is authorised by its ``booking_code``,
64 bits of entropy handed out at checkout (see ``generate_booking_code``). That
is strong against guessing, but it is a bearer token — anyone who ends up with
it holds the booking. Codes travel through inboxes, WhatsApp screenshots and
family group chats, so anything that MOVES MONEY (or reveals where money would
be sent) asks for a second, low-friction factor as well: the last four digits of
the phone number on the booking. Airlines have used exactly this shape —
reference plus surname — for decades.

Read-only confirmation stays on the code alone: the customer reaches it from
their own email link, and demanding a challenge there would strand people at the
one moment they most need to see their booking.
"""

import re
from hmac import compare_digest

#: Codes are printed as BK-XXXXXXXXXXXXXXXX, and customers type them off a phone
#: screen: lowercase, stray spaces, a copied trailing newline, sometimes without
#: the prefix. All of that is the same booking.
_CODE_NOISE = re.compile(r"[^A-Z0-9]")


def normalize_booking_code(raw):
    """Whatever the customer typed → the canonical stored form.

    Hex has no O/I/L, so there is no 0-vs-O ambiguity to repair — only case,
    separators and the optional prefix.
    """
    if not raw:
        return ""
    code = _CODE_NOISE.sub("", str(raw).upper())
    if not code:
        return ""
    if not code.startswith("BK"):
        code = f"BK{code}"
    return f"BK-{code[2:]}"


def phone_digits(raw):
    return re.sub(r"\D", "", str(raw or ""))


def phone_last4_matches(booking, supplied):
    """Constant-time check of the last four digits of the booking's phone.

    Compared on digits only, so +880 1712-345678 and 01712345678 behave the
    same. Constant-time because this is an authentication check, however weak
    the factor: a length-or-timing oracle on four digits is a real shortcut.
    """
    expected = phone_digits(booking.phone)[-4:]
    got = phone_digits(supplied)[-4:]
    if not expected or len(got) < 4:
        return False
    return compare_digest(expected, got)
