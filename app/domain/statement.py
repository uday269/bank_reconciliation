"""Bank-to-book reconciliation statement (BR-17, DD-12).

    adjusted bank balance = bank ending balance + deposits in transit - outstanding checks
    adjusted book balance = book ending balance + bank-originated credits - bank-originated debits
    unresolved difference = adjusted bank balance - adjusted book balance

Every reconciling item is listed under the section that moved the balance, so the
statement can be traced line by line. Pure computation: the period and report services
gather the lines, this module only adds them up, which is why it can be tested with
literals and why RPT-09 and the sign-off record always agree.

Which items are reconciling items is decided by the reviewer's disposition, not by the
ground truth: an item the reviewer left unresolved is not carried as a deposit in
transit, whatever its proposed category. It is listed as unresolved instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SECTIONS = ("deposits_in_transit", "outstanding_checks", "bank_originated")


@dataclass(frozen=True)
class StatementLine:
    item_ref: str
    section: str               # one of SECTIONS, or "unresolved"
    amount_cents: int          # always positive
    cash_effect_cents: int     # signed effect on cash, from the item's own side
    exception_code: str | None
    description: str
    business_date: str
    status: str


@dataclass(frozen=True)
class Statement:
    bank_ending_balance_cents: int
    book_ending_balance_cents: int
    deposits_in_transit_cents: int
    outstanding_checks_cents: int
    bank_originated_credits_cents: int
    bank_originated_debits_cents: int
    lines: list[StatementLine] = field(default_factory=list)

    @property
    def bank_originated_net_cents(self) -> int:
        return self.bank_originated_credits_cents - self.bank_originated_debits_cents

    @property
    def adjusted_bank_balance_cents(self) -> int:
        return (self.bank_ending_balance_cents + self.deposits_in_transit_cents
                - self.outstanding_checks_cents)

    @property
    def adjusted_book_balance_cents(self) -> int:
        return self.book_ending_balance_cents + self.bank_originated_net_cents

    @property
    def unresolved_difference_cents(self) -> int:
        return self.adjusted_bank_balance_cents - self.adjusted_book_balance_cents

    def section(self, name: str) -> list[StatementLine]:
        return [line for line in self.lines if line.section == name]

    @property
    def unresolved_item_count(self) -> int:
        return len(self.section("unresolved"))

    def as_dict(self) -> dict[str, int]:
        """The figures recorded on the sign-off and in the audit event."""
        return {
            "bank_ending_balance_cents": self.bank_ending_balance_cents,
            "deposits_in_transit_cents": self.deposits_in_transit_cents,
            "outstanding_checks_cents": self.outstanding_checks_cents,
            "adjusted_bank_balance_cents": self.adjusted_bank_balance_cents,
            "book_ending_balance_cents": self.book_ending_balance_cents,
            "bank_originated_credits_cents": self.bank_originated_credits_cents,
            "bank_originated_debits_cents": self.bank_originated_debits_cents,
            "adjusted_book_balance_cents": self.adjusted_book_balance_cents,
            "unresolved_difference_cents": self.unresolved_difference_cents,
            "unresolved_item_count": self.unresolved_item_count,
        }


def build_statement(bank_ending_balance_cents: int, book_ending_balance_cents: int,
                    lines: list[StatementLine]) -> Statement:
    for line in lines:
        if line.section not in SECTIONS + ("unresolved",):
            raise ValueError(f"unknown statement section {line.section!r}")
        if line.amount_cents <= 0:
            raise ValueError(f"{line.item_ref}: statement amounts are positive")
    bank_originated = [line for line in lines if line.section == "bank_originated"]
    return Statement(
        bank_ending_balance_cents=bank_ending_balance_cents,
        book_ending_balance_cents=book_ending_balance_cents,
        deposits_in_transit_cents=sum(l.amount_cents for l in lines if l.section == "deposits_in_transit"),
        outstanding_checks_cents=sum(l.amount_cents for l in lines if l.section == "outstanding_checks"),
        bank_originated_credits_cents=sum(l.amount_cents for l in bank_originated if l.cash_effect_cents > 0),
        bank_originated_debits_cents=sum(l.amount_cents for l in bank_originated if l.cash_effect_cents < 0),
        lines=sorted(lines, key=lambda l: (l.section, l.business_date, l.item_ref)))


def money(cents: int) -> str:
    """$1,234.56 or -$1,234.56, for reports and messages."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"
