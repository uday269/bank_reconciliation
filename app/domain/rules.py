"""Deterministic matching rules BR-01 to BR-12 (P3, FR-MAT-01..04).

Pure functions over plain records: no database, no configuration file, no clock. The
caller supplies items and parameters and receives decisions, which is what makes these
rules reproducible and testable on their own (ADR-04).

Order matters. Rules run before any scoring, so the AI layer never reopens something a
rule already settled (ADR-05). A rule either produces a certain result or declines,
and anything a rule declines becomes work for Stage 7.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from app.domain.normalize import business_days_between

# Bank transaction codes and description keywords that identify items the bank
# originated, which by definition have no ledger entry yet (BR-06).
FEE_CODES = frozenset({"FEE"})
INTEREST_CODES = frozenset({"INT"})
RETURN_CODES = frozenset({"RTN"})

# Codes that identify ordinary customer and vendor activity. When the statement supplies
# one of these, the description is not consulted: a payment from "Timpanogos Coffee" is
# not a fee just because the word contains those letters.
SETTLED_CODES = frozenset({"CHK", "ACH-DR", "ACH-CR", "WIRE", "DEP", "POS"})

FEE_KEYWORDS = ("FEE", "SERVICE CHARGE", "ANALYSIS CHARGE", "MAINTENANCE CHARGE")
INTEREST_KEYWORDS = ("INTEREST",)
RETURN_KEYWORDS = ("RETURNED", "NSF", "REVERSAL")


@dataclass(frozen=True)
class Item:
    """One side of a potential match, in the form the rules need.

    Built from a database row by `from_row`, so the rules never see SQL types or
    column names and can be exercised with literals in tests.
    """

    item_type: str            # bank, ledger, carry_in
    item_id: int
    external_id: str
    business_date: date
    amount_cents: int
    direction: str            # debit or credit as the source states it
    description: str          # normalized
    reference: str | None     # normalized
    payee: str | None         # normalized
    type_code: str | None = None
    is_possible_duplicate: bool = False
    carry_in_type: str | None = None    # outstanding_check or deposit_in_transit

    @property
    def cash_effect(self) -> int:
        """Signed effect on cash: positive increases the balance.

        A bank credit and a ledger debit both increase cash, which is how the two
        sides are compared without rewriting either source's direction (DOC-04 3.2).
        """
        if self.item_type == "bank":
            return self.amount_cents if self.direction == "credit" else -self.amount_cents
        return self.amount_cents if self.direction == "debit" else -self.amount_cents

    @staticmethod
    def from_row(item_type: str, row: Any) -> "Item":
        date_column = {"bank": "transaction_date", "ledger": "posting_date",
                       "carry_in": "original_date"}[item_type]
        id_column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                     "carry_in": "carry_in_item_id"}[item_type]
        external_column = {"bank": "external_txn_id", "ledger": "external_entry_id",
                           "carry_in": "external_item_id"}[item_type]
        payee_column = {"bank": "payee_normalized", "ledger": "counterparty_normalized",
                        "carry_in": None}[item_type]
        keys = row.keys()
        return Item(
            item_type=item_type,
            item_id=row[id_column],
            external_id=row[external_column],
            business_date=date.fromisoformat(row[date_column]),
            amount_cents=row["amount_cents"],
            direction=row["direction"],
            description=row["description_normalized"] or "",
            reference=row["reference_normalized"],
            payee=row[payee_column] if payee_column and payee_column in keys else None,
            type_code=row["bank_type_code"] if "bank_type_code" in keys else None,
            is_possible_duplicate=bool(row["is_possible_duplicate"]) if "is_possible_duplicate" in keys else False,
            carry_in_type=row["item_type"] if item_type == "carry_in" else None,
        )


@dataclass(frozen=True)
class RuleMatch:
    """A pairing a rule is certain about."""

    rule_name: str
    relationship: str                     # one_to_one
    subject: Item
    counterpart: Item
    explanation: str


@dataclass(frozen=True)
class RuleException:
    """An item a rule can classify but not match."""

    rule_name: str
    subject: Item
    exception_code: str
    explanation: str
    suggested_action: str


@dataclass
class RuleOutcome:
    matches: list[RuleMatch] = field(default_factory=list)
    exceptions: list[RuleException] = field(default_factory=list)
    unmatched_bank: list[Item] = field(default_factory=list)
    unmatched_ledger: list[Item] = field(default_factory=list)
    unmatched_carry_in: list[Item] = field(default_factory=list)

    @property
    def matched_ids(self) -> set[tuple[str, int]]:
        ids: set[tuple[str, int]] = set()
        for match in self.matches:
            ids.add((match.subject.item_type, match.subject.item_id))
            ids.add((match.counterpart.item_type, match.counterpart.item_id))
        return ids


# ---------------------------------------------------------------------------
# BR-06 bank-originated items
# ---------------------------------------------------------------------------

def classify_bank_originated(item: Item) -> tuple[str, str] | None:
    """BR-06: fee, interest or returned item, by transaction code or description.

    Returns (exception_code, reason) or None. The code is trusted first because it is
    structured; keywords catch statements that do not supply one.
    """
    code = (item.type_code or "").upper()
    text = item.description.upper()

    if code in FEE_CODES:
        return "EXC-03", "bank fee identified by transaction code"
    if code in INTEREST_CODES:
        return "EXC-04", "interest identified by transaction code"
    if code in RETURN_CODES:
        return "EXC-06", "returned item identified by transaction code"

    # Keywords are a fallback for statements that supply no code, or an unrecognized one.
    # They are matched on whole phrases: "COFFEE" must not read as "FEE" (SCN-01 regression).
    if code in SETTLED_CODES:
        return None
    if _contains_phrase(text, FEE_KEYWORDS):
        return "EXC-03", "bank fee identified by description"
    if _contains_phrase(text, INTEREST_KEYWORDS):
        return "EXC-04", "interest identified by description"
    if _contains_phrase(text, RETURN_KEYWORDS):
        return "EXC-06", "returned item identified by description"
    return None


def _contains_phrase(text: str, phrases: Sequence[str]) -> bool:
    """Whole-word phrase match, so a keyword never fires on part of a longer word."""
    words = text.split()
    for phrase in phrases:
        parts = phrase.split()
        if len(parts) == 1:
            if parts[0] in words:
                return True
        else:
            for index in range(len(words) - len(parts) + 1):
                if words[index:index + len(parts)] == parts:
                    return True
    return False


# ---------------------------------------------------------------------------
# BR-01 and BR-02 exact matching
# ---------------------------------------------------------------------------

def _exact_key(item: Item) -> tuple[int, int, str] | None:
    """Equal cash effect, same business date, equal non-empty reference (BR-01)."""
    if not item.reference:
        return None
    return (item.cash_effect, item.business_date.toordinal(), item.reference)


def find_exact_matches(bank_items: Sequence[Item],
                       ledger_items: Sequence[Item]) -> tuple[list[RuleMatch], set[tuple[str, int]]]:
    """BR-01 with the BR-02 uniqueness test.

    A pair is exact only when the key identifies exactly one item on each side. Where
    an amount, date and reference repeat, every candidate is left for scoring, which
    is what stops a false exact match on the dataset's repeated-amount pairs.
    """
    bank_by_key: dict[tuple, list[Item]] = defaultdict(list)
    ledger_by_key: dict[tuple, list[Item]] = defaultdict(list)
    for item in bank_items:
        key = _exact_key(item)
        if key:
            bank_by_key[key].append(item)
    for item in ledger_items:
        key = _exact_key(item)
        if key:
            ledger_by_key[key].append(item)

    matches: list[RuleMatch] = []
    consumed: set[tuple[str, int]] = set()
    for key, bank_candidates in bank_by_key.items():
        ledger_candidates = ledger_by_key.get(key, [])
        if len(bank_candidates) != 1 or len(ledger_candidates) != 1:
            continue                       # BR-02: ambiguous, so not exact
        bank_item, ledger_item = bank_candidates[0], ledger_candidates[0]
        if bank_item.is_possible_duplicate or ledger_item.is_possible_duplicate:
            continue                       # flagged items are reviewed, never auto-matched
        matches.append(RuleMatch(
            rule_name="BR-01",
            relationship="one_to_one",
            subject=bank_item,
            counterpart=ledger_item,
            explanation=(
                f"Amount {bank_item.amount_cents} cents, reference {bank_item.reference} and "
                f"date {bank_item.business_date.isoformat()} agree, and no other item on either "
                f"side shares them (BR-01, BR-02)")))
        consumed.add((bank_item.item_type, bank_item.item_id))
        consumed.add((ledger_item.item_type, ledger_item.item_id))
    return matches, consumed


# ---------------------------------------------------------------------------
# BR-11 carry-in clearing
# ---------------------------------------------------------------------------

def find_carry_in_clearings(bank_items: Sequence[Item], carry_in_items: Sequence[Item],
                            timing_window_days: int) -> tuple[list[RuleMatch], set[tuple[str, int]]]:
    """BR-11: a current-period bank item clears a prior-period outstanding item.

    Matched on equal amount, opposite-side cash effect and, where both have one, an
    equal reference. A carry-in item clears at most once.
    """
    by_amount: dict[int, list[Item]] = defaultdict(list)
    for item in carry_in_items:
        by_amount[item.amount_cents].append(item)

    matches: list[RuleMatch] = []
    consumed: set[tuple[str, int]] = set()
    for bank_item in bank_items:
        for carry_item in by_amount.get(bank_item.amount_cents, []):
            if (carry_item.item_type, carry_item.item_id) in consumed:
                continue
            # An outstanding check leaves cash, a deposit in transit brings it in.
            expected_direction = "debit" if carry_item.carry_in_type == "outstanding_check" else "credit"
            if bank_item.direction != expected_direction:
                continue
            if bank_item.reference and carry_item.reference and bank_item.reference != carry_item.reference:
                continue
            matches.append(RuleMatch(
                rule_name="BR-11",
                relationship="one_to_one",
                subject=bank_item,
                counterpart=carry_item,
                explanation=(
                    f"Clears prior-period {carry_item.carry_in_type.replace('_', ' ')} "
                    f"{carry_item.external_id} of {carry_item.amount_cents} cents "
                    f"dated {carry_item.business_date.isoformat()} (BR-11)")))
            consumed.add((bank_item.item_type, bank_item.item_id))
            consumed.add((carry_item.item_type, carry_item.item_id))
            break
    return matches, consumed


# ---------------------------------------------------------------------------
# BR-07 duplicates, BR-08 to BR-10 period-end items, BR-12 missing vs unexplained
# ---------------------------------------------------------------------------

def classify_duplicate(item: Item) -> RuleException | None:
    """BR-07: an item validation flagged as a possible duplicate goes to a reviewer."""
    if not item.is_possible_duplicate:
        return None
    return RuleException(
        rule_name="BR-07", subject=item, exception_code="EXC-05",
        explanation=("Another item on the same side has the same amount, direction and "
                     "description within one business day (BR-07). The system cannot tell "
                     "which copy is genuine."),
        suggested_action="Investigate and confirm or dismiss the duplicate")


def classify_period_end_item(item: Item, period_end: date, stale_check_days: int,
                             stale_deposit_days: int) -> RuleException | None:
    """BR-08, BR-09 and BR-10: unmatched ledger items at the period end.

    A payment the bank has not seen is an outstanding check; a receipt it has not seen
    is a deposit in transit. Either becomes stale once it has been waiting too long.
    """
    if item.item_type not in ("ledger", "carry_in"):
        return None

    if item.item_type == "carry_in":
        is_payment = item.carry_in_type == "outstanding_check"
    else:
        is_payment = item.cash_effect < 0

    age_days = (period_end - item.business_date).days
    if is_payment:
        stale = age_days > stale_check_days
        return RuleException(
            rule_name="BR-10" if stale else "BR-08", subject=item, exception_code="EXC-01",
            explanation=(f"Payment of {item.amount_cents} cents dated "
                         f"{item.business_date.isoformat()} has not cleared the bank, "
                         f"{age_days} days outstanding"
                         + (f", beyond the {stale_check_days} day limit (BR-10)" if stale else " (BR-08)")),
            suggested_action="Investigate the stale check" if stale else "Carry forward as a reconciling item")

    stale = business_days_between(item.business_date, period_end) > stale_deposit_days
    return RuleException(
        rule_name="BR-10" if stale else "BR-09", subject=item, exception_code="EXC-02",
        explanation=(f"Receipt of {item.amount_cents} cents dated "
                     f"{item.business_date.isoformat()} is not on the statement"
                     + (f", beyond the {stale_deposit_days} business day limit (BR-10)" if stale else " (BR-09)")),
        suggested_action="Investigate the delayed deposit" if stale else "Carry forward as a reconciling item")


def classify_unmatched_bank_item(item: Item, known_payees: Iterable[str]) -> RuleException:
    """BR-12: an unmatched bank item is either a missing ledger entry or unexplained.

    A recognizable counterparty means the entry is probably missing from the ledger and
    can be corrected. No counterpart and no recognizable payee means the reviewer has
    nothing to act on yet, so the item is escalated and stays unresolved.
    """
    payee = (item.payee or "").strip()
    if payee and payee in set(known_payees):
        return RuleException(
            rule_name="BR-12", subject=item, exception_code="EXC-06",
            explanation=(f"Bank item for {payee} has no ledger entry, but the counterparty "
                         f"appears elsewhere in the period, so the entry is likely missing (BR-12)"),
            suggested_action="Identify the source and propose an adjustment")
    return RuleException(
        rule_name="BR-12", subject=item, exception_code="EXC-07",
        explanation=(f"No ledger candidate and no recognizable counterparty for "
                     f"{item.description or item.external_id} (BR-12)"),
        suggested_action="Escalate and keep unresolved until explained")


def classify_unmatched_ledger_item(item: Item) -> RuleException:
    """An unmatched ledger entry that is not a period-end timing item is unexplained."""
    return RuleException(
        rule_name="BR-12", subject=item, exception_code="EXC-07",
        explanation=(f"No bank candidate for ledger entry {item.external_id} "
                     f"of {item.amount_cents} cents (BR-12)"),
        suggested_action="Escalate and keep unresolved until explained")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def apply_rules(bank_items: Sequence[Item], ledger_items: Sequence[Item],
                carry_in_items: Sequence[Item], *, period_end: date,
                timing_window_days: int, stale_check_days: int,
                stale_deposit_days: int) -> RuleOutcome:
    """Run every deterministic rule in order and report what is left.

    Sequence: bank-originated items, exact matches, carry-in clearings, duplicates,
    then period-end classification. Whatever remains is passed to scoring.
    """
    outcome = RuleOutcome()
    consumed: set[tuple[str, int]] = set()

    # BR-06: bank-originated items never have a counterpart, so they are removed first.
    for item in bank_items:
        classification = classify_bank_originated(item)
        if classification:
            code, reason = classification
            action = {"EXC-03": "Propose an adjustment: debit expense, credit cash",
                      "EXC-04": "Propose an adjustment: debit cash, credit income",
                      "EXC-06": "Identify the source and propose an adjustment"}[code]
            outcome.exceptions.append(RuleException(
                rule_name="BR-06", subject=item, exception_code=code,
                explanation=f"{reason}; no ledger entry exists for it (BR-06)",
                suggested_action=action))
            consumed.add((item.item_type, item.item_id))

    remaining_bank = [item for item in bank_items if (item.item_type, item.item_id) not in consumed]

    # BR-01 and BR-02.
    exact_matches, exact_consumed = find_exact_matches(remaining_bank, ledger_items)
    outcome.matches.extend(exact_matches)
    consumed |= exact_consumed

    # BR-11 carry-in clearing, on what the exact rule did not take.
    remaining_bank = [item for item in remaining_bank if (item.item_type, item.item_id) not in consumed]
    carry_matches, carry_consumed = find_carry_in_clearings(
        remaining_bank, carry_in_items, timing_window_days)
    outcome.matches.extend(carry_matches)
    consumed |= carry_consumed

    # Anything still unmatched.
    outcome.unmatched_bank = [i for i in bank_items if (i.item_type, i.item_id) not in consumed]
    outcome.unmatched_ledger = [i for i in ledger_items if (i.item_type, i.item_id) not in consumed]
    outcome.unmatched_carry_in = [i for i in carry_in_items if (i.item_type, i.item_id) not in consumed]

    # BR-07 duplicates are classified but stay in the unmatched lists: the scoring layer
    # may still find a counterpart for the genuine copy.
    for item in outcome.unmatched_bank + outcome.unmatched_ledger:
        duplicate = classify_duplicate(item)
        if duplicate:
            outcome.exceptions.append(duplicate)

    # BR-08 to BR-10 for carry-in items that did not clear.
    for item in outcome.unmatched_carry_in:
        exception = classify_period_end_item(item, period_end, stale_check_days, stale_deposit_days)
        if exception:
            outcome.exceptions.append(exception)

    return outcome
