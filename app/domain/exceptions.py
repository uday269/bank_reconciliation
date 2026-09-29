"""Exception categories EXC-01 to EXC-08 and the next action for each (BR-16).

One place decides what an exception means, what a reviewer should do about it, and
which accounts an adjustment would use. Services and reports read from here rather
than each holding their own copy of the mapping.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExceptionCategory:
    code: str
    name: str
    side: str                       # bank, ledger or either
    next_action: str
    adjustment_debit: str | None    # GL account codes for a proposed entry, if any
    adjustment_credit: str | None
    carries_forward: bool           # appears on the reconciliation statement
    default_disposition: str        # what CR-07 expects once approved


CATEGORIES: dict[str, ExceptionCategory] = {
    "EXC-01": ExceptionCategory(
        "EXC-01", "Outstanding check", "ledger",
        "Carry forward as a reconciling item; investigate if older than the stale limit",
        None, None, True, "carry_forward"),
    "EXC-02": ExceptionCategory(
        "EXC-02", "Deposit in transit", "ledger",
        "Carry forward as a reconciling item; investigate if older than the stale limit",
        None, None, True, "carry_forward"),
    "EXC-03": ExceptionCategory(
        "EXC-03", "Bank fee", "bank",
        "Propose an adjustment: debit bank service charges, credit cash",
        "6810", "1010", False, "adjust"),
    "EXC-04": ExceptionCategory(
        "EXC-04", "Bank interest", "bank",
        "Propose an adjustment: debit cash, credit interest income",
        "1010", "7010", False, "adjust"),
    "EXC-05": ExceptionCategory(
        "EXC-05", "Duplicate", "either",
        "Investigate and confirm or dismiss the duplicate",
        None, None, False, "investigate"),
    "EXC-06": ExceptionCategory(
        "EXC-06", "Missing ledger entry", "bank",
        "Identify the source and propose an adjustment",
        "6820", "1010", False, "adjust"),
    "EXC-07": ExceptionCategory(
        "EXC-07", "Unexplained item", "either",
        "Escalate and keep unresolved until explained",
        "9990", "1010", False, "unresolved"),
    "EXC-08": ExceptionCategory(
        "EXC-08", "Possible group beyond the search limit", "either",
        "Investigate manually; the automated group search was capped",
        None, None, False, "investigate"),
}


def get(code: str) -> ExceptionCategory:
    if code not in CATEGORIES:
        raise KeyError(f"unknown exception code {code!r}")
    return CATEGORIES[code]


def next_action(code: str) -> str:
    return get(code).next_action


def proposes_adjustment(code: str) -> bool:
    """Whether this category has a suggested entry for the reviewer to confirm."""
    category = get(code)
    return category.adjustment_debit is not None and category.adjustment_credit is not None


def carries_forward(code: str) -> bool:
    """Whether the item appears on the bank-to-book statement (BR-17)."""
    return get(code).carries_forward


def statement_section(code: str) -> str | None:
    """Where the item belongs on the reconciliation statement, if anywhere."""
    return {"EXC-01": "outstanding_checks", "EXC-02": "deposits_in_transit",
            "EXC-03": "bank_originated", "EXC-04": "bank_originated",
            "EXC-06": "bank_originated"}.get(code)
