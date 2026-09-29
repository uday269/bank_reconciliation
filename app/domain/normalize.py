"""Normalization rules NRM-DATE through NRM-REF (DOC-04 section 5).

Every function is pure: text in, text out, no database and no clock. That makes the
rules testable in isolation and means the same input always normalizes the same way,
which the reproducibility requirement depends on (NFR-01).

Originals are never modified. `normalize_item` returns the new values together with a
change record for each field it altered, and the caller stores both (FR-NRM-05).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Rule NRM-ABBREV: bank abbreviations seen in statement descriptions
# ---------------------------------------------------------------------------

ABBREVIATIONS: dict[str, str] = {
    "GRCRY": "GROCERY", "GROC": "GROCERY", "GRO": "GROCERY",
    "MKT": "MARKET", "MRKT": "MARKET",
    "CO": "COMPANY", "CORP": "CORPORATION", "INC": "INCORPORATED", "LLC": "LLC",
    "INTL": "INTERNATIONAL", "NATL": "NATIONAL",
    "MFG": "MANUFACTURING", "DIST": "DISTRIBUTION", "WHSL": "WHOLESALE",
    "SVC": "SERVICE", "SVCS": "SERVICES", "SERV": "SERVICE",
    "EQP": "EQUIPMENT", "EQUIP": "EQUIPMENT", "RPR": "REPAIR",
    "SPLY": "SUPPLY", "SUP": "SUPPLY", "PROD": "PRODUCE", "PRODUCE": "PRODUCE",
    "PROV": "PROVISIONS", "PROVSNS": "PROVISIONS", "PROVISNS": "PROVISIONS",
    "BKRY": "BAKERY", "BAKRY": "BAKERY", "CAFE": "CAFE", "CF": "CAFE",
    "RESTR": "RESTAURANT", "RSTRNT": "RESTAURANT", "BSTRO": "BISTRO", "BSTR": "BISTRO",
    "CATER": "CATERING", "CTRG": "CATERING", "CTR": "CENTER",
    "CRMRY": "CREAMERY", "DRY": "DAIRY", "BEV": "BEVERAGE",
    "FRGHT": "FREIGHT", "FRT": "FREIGHT", "LOG": "LOGISTICS",
    "TRDG": "TRADING", "TRADE": "TRADING", "PST": "POST",
    "PKG": "PACKAGING", "PAPR": "PAPER", "PRNT": "PRINT", "WKS": "WORKS",
    "STRG": "STORAGE", "CLD": "COLD", "CLN": "CLEANING", "INS": "INSURANCE",
    "PAYRL": "PAYROLL", "UTIL": "UTILITIES", "SPC": "SPICES",
    "MTN": "MOUNTAIN", "MT": "MOUNTAIN", "VLY": "VALLEY", "CYN": "CANYON",
    "SPG": "SPRINGS", "SPGS": "SPRINGS", "ISL": "ISLAND", "CRK": "CREEK",
    "RDG": "RIDGE", "PK": "PEAK", "TBL": "TABLE", "GRL": "GRILL", "DNR": "DINER",
    "COF": "COFFEE", "FDS": "FOODS", "FD": "FOOD", "DELI": "DELI", "GRP": "GROUP",
    "WTR": "WATER", "SPR": "SUPPER", "CLB": "CLUB", "KTCHN": "KITCHEN", "KTCH": "KITCHEN",
    "PTNR": "PARTNERS", "PTNRS": "PARTNERS", "ASSOC": "ASSOCIATES", "BROS": "BROTHERS",
    "MGMT": "MANAGEMENT", "TRANSP": "TRANSPORT", "WHSE": "WAREHOUSE", "ELEC": "ELECTRIC",
    "PWR": "POWER", "TELCO": "TELECOM", "ACCT": "ACCOUNT", "MAINT": "MAINTENANCE",
    "ADJ": "ADJUSTMENT", "RTN": "RETURNED", "DEPT": "DEPARTMENT", "NO": "NUMBER",
}

# Words that describe the transaction rather than the counterparty. Removed when the
# payee is extracted, so "CANYON RIDGE GROCERY INVOICE 8842" and "ACH DEP CANYON RIDGE
# GRCRY 8842" both yield the same payee (NRM-PAYEE).
NON_PAYEE_TOKENS: frozenset[str] = frozenset({
    "INVOICE", "CHECK", "DEPOSIT", "PAYMENT", "REMITTANCE", "SETTLEMENT", "BATCH",
    "TRANSFER", "NUMBER", "REFERENCE", "ACCOUNT", "RETURNED", "ITEM", "ENTRY",
})

# ---------------------------------------------------------------------------
# Rule NRM-NOISE: bank channel prefixes, stripped into their own field
# ---------------------------------------------------------------------------

CHANNEL_PREFIXES: tuple[str, ...] = (
    "ACH DEPOSIT", "ACH CREDIT", "ACH DEBIT", "ACH DEP", "ACH",
    "REMOTE DEPOSIT", "REMOTE DEP", "COUNTER CREDIT", "DEPOSIT",
    "CHECK PAID", "CHECK", "CHK",
    "POS DEBIT", "POS DEB", "POS",
    "WIRE OUT", "WIRE IN", "WIRE",
    "RETURNED DEPOSIT", "RETURNED ITEM",
)

REFERENCE_PREFIXES: tuple[str, ...] = ("CHK#", "CHK", "CHECK#", "CHECK", "REF#", "REF",
                                       "INV#", "INV", "INVOICE", "DEP#", "DEP", "NO.", "NO")

_PUNCTUATION = re.compile(r"[^A-Z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")
_TRAILING_NUMBER = re.compile(r"\b\w*\d{3,}\w*\b")   # 8842, DEP44821, INV8842


class NormalizationError(ValueError):
    """A value could not be normalized and the row must be excluded (FR-VAL-02)."""


@dataclass(frozen=True)
class FieldChange:
    """One recorded transformation, stored in normalization_change (RPT-03)."""

    field_name: str
    original_value: str | None
    revised_value: str | None
    rule_name: str


# ---------------------------------------------------------------------------
# NRM-DATE and NRM-AMOUNT
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%Y/%m/%d", "%m-%d-%Y")


def normalize_date(value: str) -> str:
    """NRM-DATE: parse a supplied date to ISO 8601. Raises when unparseable."""
    text = (value or "").strip()
    if not text:
        raise NormalizationError("date is empty")
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise NormalizationError(f"date {value!r} is not a recognized format")


def normalize_amount_to_cents(value: str) -> int:
    """NRM-AMOUNT: convert a supplied amount to positive integer cents.

    Accepts 1234.56, $1,234.56, (1,234.56) and 1234. The sign is dropped: direction
    carries it, and the schema requires a positive amount.
    """
    text = (value or "").strip()
    if not text:
        raise NormalizationError("amount is empty")

    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("$", "").replace(",", "").replace(" ", "")
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]
    if not re.fullmatch(r"\d+(\.\d{1,2})?", cleaned):
        raise NormalizationError(f"amount {value!r} is not numeric")

    whole, _, fraction = cleaned.partition(".")
    cents = int(whole) * 100 + int((fraction or "0").ljust(2, "0"))
    if cents == 0:
        raise NormalizationError("amount is zero")
    return cents  # negative is recorded by the caller as a direction, never as a sign


def normalize_direction(value: str) -> str:
    text = (value or "").strip().lower()
    if text in ("debit", "dr", "d"):
        return "debit"
    if text in ("credit", "cr", "c"):
        return "credit"
    raise NormalizationError(f"direction {value!r} must be debit or credit")


# ---------------------------------------------------------------------------
# NRM-CASE, NRM-PUNCT, NRM-ABBREV, NRM-NOISE, NRM-PAYEE
# ---------------------------------------------------------------------------

def normalize_text(value: str) -> str:
    """NRM-CASE and NRM-PUNCT: uppercase, strip punctuation, collapse whitespace."""
    text = (value or "").upper()
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def expand_abbreviations(value: str) -> str:
    """NRM-ABBREV: expand known bank abbreviations, token by token."""
    return " ".join(ABBREVIATIONS.get(token, token) for token in value.split())


def split_channel(value: str) -> tuple[str, str | None]:
    """NRM-NOISE: separate a leading channel phrase from the rest of the description."""
    text = value.strip()
    for prefix in CHANNEL_PREFIXES:                  # longest forms are listed first
        if text == prefix:
            return "", prefix
        if text.startswith(prefix + " "):
            return text[len(prefix) + 1:].strip(), prefix
    return text, None


def extract_payee(value: str) -> str:
    """NRM-PAYEE: the counterparty, with reference numbers and transaction words removed."""
    without_numbers = _TRAILING_NUMBER.sub(" ", value)
    tokens = [token for token in without_numbers.split() if token not in NON_PAYEE_TOKENS]
    return " ".join(tokens).strip()


def normalize_reference(value: str | None) -> str | None:
    """NRM-REF: strip known prefixes and leading zeros. Empty becomes None."""
    if value is None:
        return None
    text = normalize_text(value)
    if not text:
        return None
    for prefix in REFERENCE_PREFIXES:
        bare = prefix.replace("#", "")
        if text.startswith(bare + " "):
            text = text[len(bare) + 1:].strip()
            break
        if text.startswith(bare) and text[len(bare):].isdigit():
            text = text[len(bare):]
            break
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text or None


# ---------------------------------------------------------------------------
# Item-level normalization
# ---------------------------------------------------------------------------

def normalize_description(description: str) -> tuple[str, str | None, str]:
    """Return (normalized description, channel, payee) for one description."""
    cleaned = expand_abbreviations(normalize_text(description))
    body, channel = split_channel(cleaned)
    payee = extract_payee(body)
    normalized = body if body else (channel or cleaned)
    return normalized, channel, payee


def normalize_item(description: str, reference: str | None) -> tuple[dict[str, Any], list[FieldChange]]:
    """Normalize the text fields of one item.

    Returns the values to store and one change record per field that actually changed.
    Fields that normalize to themselves produce no record, so RPT-03 lists real
    transformations rather than every field on every row.
    """
    normalized_description, channel, payee = normalize_description(description)
    normalized_reference = normalize_reference(reference)

    values: dict[str, Any] = {
        "description_normalized": normalized_description,
        "reference_normalized": normalized_reference,
    }

    changes: list[FieldChange] = []
    if normalized_description != (description or "").strip():
        changes.append(FieldChange("description", description, normalized_description,
                                   "NRM-CASE, NRM-PUNCT, NRM-ABBREV, NRM-NOISE"))
    if channel:
        changes.append(FieldChange("channel", description, channel, "NRM-NOISE"))
    if payee and payee != normalized_description:
        changes.append(FieldChange("payee", description, payee, "NRM-PAYEE"))
    if normalized_reference != (reference or None):
        changes.append(FieldChange("reference", reference, normalized_reference, "NRM-REF"))

    return values, changes


def business_days_between(start: date, end: date) -> int:
    """Business days from start to end, ignoring holidays (ASM-02).

    Used by the timing window (PRM-02) and the duplicate window (BR-07). Signed, so a
    bank item posting after its ledger entry gives a positive gap.
    """
    if start == end:
        return 0
    step = 1 if end > start else -1
    days = 0
    cursor = start
    while cursor != end:
        cursor = date.fromordinal(cursor.toordinal() + step)
        if cursor.weekday() < 5:
            days += step
    return days
