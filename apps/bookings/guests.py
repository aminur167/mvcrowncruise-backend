"""Foreign-national guest list — normalisation and validation.

`BookingRoom.foreign_guests` is a JSON list of the foreign nationals in one
cabin, each carrying that guest's passport. It is BOTH a pricing input (its
counts are the foreigner-surcharge quantity) and the record staff/immigration
read off the manifest, so exactly one module owns its shape — imported by the
model's clean() (the create path) and by the quote serializer (which has no
row to clean), so a quote can never accept a party the booking would reject.

Client policy: passport number is REQUIRED for every foreign guest; name,
nationality and expiry are captured when offered but never demanded.
"""

import re
from datetime import date

from django.core.exceptions import ValidationError

ADULT = "adult"
KID = "kid"
GUEST_TYPES = (ADULT, KID)

#: Passport numbers are alphanumeric across issuing states (US = 9 digits,
#: UK = 9 alphanumeric, BD = 9 with a letter prefix). Separators customers type
#: are stripped before this is applied, so the bounds cover the raw identifier
#: only. Deliberately permissive on format and strict on shape: rejecting a
#: valid foreign passport at checkout costs a booking, and the number is
#: transcribed by staff at the pier regardless.
PASSPORT_RE = re.compile(r"^[A-Z0-9]{5,20}$")

#: ISO 3166-1 alpha-2. Nationality is optional, but when given it must be a
#: real code — a free-text country field turns the immigration manifest into
#: unusable prose ("USA" / "United States" / "us" for one nationality).
ISO_3166_1_ALPHA2 = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ
    BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR
    CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
    MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
    PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI
    SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR
    TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
    """.split()
)

#: Bangladeshi nationals are not foreign guests — listing one here would apply
#: the surcharge to a local, which is exactly the mistake the flag exists to
#: avoid. (The ship sails from Bangladesh; "foreign" means non-BD.)
HOME_COUNTRY = "BD"

MAX_NAME_LEN = 100

#: Hard ceiling on list length. This field reaches the DB from an ANONYMOUS
#: endpoint, so it is bounded independently of the pax checks below — those
#: compare against adult_count/kid_details, which are themselves bounded by the
#: room type, but a malformed request must be rejected on size before any of
#: that is trusted.
MAX_GUESTS = 20


def normalise_passport(value):
    """Strip the separators customers type and upper-case the rest.

    "a1234567", "A 123 4567" and "A-1234567" are one passport, and must compare
    equal — otherwise the duplicate check below is trivially defeated and the
    same person is billed twice for one surcharge.
    """
    return re.sub(r"[\s\-/]", "", str(value)).upper()


def clean_foreign_guests(raw, *, adult_count, kid_count, require_passport=True):
    """Validate and normalise one cabin's foreign-guest list.

    Returns a new, canonical list (passports upper-cased, unknown keys dropped,
    blank optionals omitted). Raises ValidationError with a plain message; the
    caller keys it onto its own field ("foreign_guests" on the model, the room
    index on the serializer).

    `adult_count` / `kid_count` are the cabin's TOTAL pax. Foreign guests are a
    subset of them, never additional people, so each type's count is capped by
    the matching total — otherwise a cabin could be billed a surcharge for a
    guest who is not on board.

    `require_passport=False` is for the QUOTE path only. A quote is a price
    preview that stores nothing, and the price depends solely on how many
    foreign guests of each fare type there are — identity is a requirement of
    *booking*, not of pricing. Demanding a passport to see a price would mean
    the customer cannot learn the surcharge until they have typed every
    document number, and would re-price on each keystroke.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValidationError(
            'foreign_guests must be a list, e.g. [{"guest_type": "adult", '
            '"passport_number": "A1234567"}].'
        )
    if len(raw) > MAX_GUESTS:
        raise ValidationError(f"At most {MAX_GUESTS} foreign guests per room.")

    cleaned = []
    seen_passports = set()
    for index, entry in enumerate(raw):
        position = index + 1
        if not isinstance(entry, dict):
            raise ValidationError(f"Foreign guest {position} must be an object.")

        guest_type = entry.get("guest_type")
        if guest_type not in GUEST_TYPES:
            raise ValidationError(
                f'Foreign guest {position}: guest_type must be "adult" or "kid".'
            )

        passport = normalise_passport(entry.get("passport_number") or "")
        if not passport and require_passport:
            raise ValidationError(
                f"Foreign guest {position}: passport number is required."
            )
        # A blank passport is only ever tolerated on the quote path; the format
        # and uniqueness rules still apply to anything actually supplied, so a
        # quote can never accept a passport the booking would reject.
        if passport:
            if not PASSPORT_RE.match(passport):
                raise ValidationError(
                    f"Foreign guest {position}: passport number must be 5-20 "
                    "letters and digits."
                )
            if passport in seen_passports:
                raise ValidationError(
                    f"Foreign guest {position}: passport {passport} is listed twice."
                )
            seen_passports.add(passport)

        guest = {"guest_type": guest_type, "passport_number": passport}

        # ── Optional fields: captured when offered, never demanded ──────────
        full_name = (entry.get("full_name") or "").strip()
        if full_name:
            if len(full_name) > MAX_NAME_LEN:
                raise ValidationError(
                    f"Foreign guest {position}: name is too long "
                    f"(max {MAX_NAME_LEN} characters)."
                )
            guest["full_name"] = full_name

        nationality = (entry.get("nationality") or "").strip().upper()
        if nationality:
            if nationality not in ISO_3166_1_ALPHA2:
                raise ValidationError(
                    f"Foreign guest {position}: '{nationality}' is not a valid "
                    "2-letter country code."
                )
            if nationality == HOME_COUNTRY:
                raise ValidationError(
                    f"Foreign guest {position}: Bangladeshi nationals are not "
                    "foreign guests — remove them from this list."
                )
            guest["nationality"] = nationality

        expiry = entry.get("passport_expiry")
        if expiry not in (None, ""):
            guest["passport_expiry"] = _clean_expiry(expiry, position)

        cleaned.append(guest)

    foreign_adults = sum(1 for g in cleaned if g["guest_type"] == ADULT)
    foreign_kids = len(cleaned) - foreign_adults
    if foreign_adults > adult_count:
        raise ValidationError(
            f"{foreign_adults} foreign adults listed but the room has only "
            f"{adult_count} adult(s)."
        )
    if foreign_kids > kid_count:
        raise ValidationError(
            f"{foreign_kids} foreign children listed but the room has only "
            f"{kid_count} child(ren)."
        )
    return cleaned


def _clean_expiry(value, position):
    """Optional passport expiry → ISO date string.

    An already-expired passport is REJECTED rather than stored: the guest
    cannot board on it, and finding out at the pier is worse than at checkout.
    """
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            raise ValidationError(
                f"Foreign guest {position}: passport expiry must be a date "
                "like 2031-04-09."
            ) from None
    if parsed <= date.today():
        raise ValidationError(
            f"Foreign guest {position}: passport expired on {parsed:%d %b %Y} — "
            "it cannot be used to board."
        )
    return parsed.isoformat()


def guest_counts(foreign_guests):
    """(foreign_adults, foreign_kids) for a cleaned list — the surcharge
    quantities. Tolerates the empty list that every pre-feature booking has."""
    adults = sum(
        1 for g in foreign_guests or [] if g.get("guest_type") == ADULT
    )
    return adults, len(foreign_guests or []) - adults


def mask_passport(passport):
    """Passport as shown on customer-facing surfaces (confirmation page, email).

    A passport number is sensitive identity data and those surfaces are reached
    with only a booking code, so the full number is never rendered there — only
    enough for the customer to recognise which of their guests a row is. Staff
    APIs and the immigration manifest PDF (private bucket) carry it in full.
    """
    passport = (passport or "").strip()
    if len(passport) <= 4:
        return "*" * len(passport)
    return "*" * (len(passport) - 4) + passport[-4:]
