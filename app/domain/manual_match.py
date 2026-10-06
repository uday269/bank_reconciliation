"""Checks for a pairing a reviewer selects by hand (FR-REV-07, DD-07).

When the automated group search hits its cap (EXC-08), or misses a many-to-one group,
the reviewer can do the search. The system does not judge whether the pairing is
plausible; it verifies the arithmetic and the structure, and refuses anything that
would not reconcile:

  * counterparts come from the other side (bank against ledger or carry-in items)
  * every record appears once
  * the signed cash effects agree to the cent

The DD-07 member cap does not apply here. The cap exists because an exhaustive search
over subsets is exponential; a person selecting records needs no search, and a real
deposit can combine more than four receipts.
"""

from __future__ import annotations

from app.domain.rules import Item
from app.domain.statement import money

BANK_SIDE = frozenset({"bank"})
BOOK_SIDE = frozenset({"ledger", "carry_in"})


def check_manual_match(subject: Item, counterparts: list[Item]) -> list[str]:
    """Every problem with the proposed pairing, or an empty list if it reconciles."""
    problems: list[str] = []
    if not counterparts:
        return ["select at least one record to pair with this item"]

    keys = [(item.item_type, item.item_id) for item in counterparts]
    if len(set(keys)) != len(keys):
        problems.append("a record is selected more than once")
    if (subject.item_type, subject.item_id) in keys:
        problems.append("the item cannot be paired with itself")

    other_side = BOOK_SIDE if subject.item_type in BANK_SIDE else BANK_SIDE
    wrong_side = [item.external_id for item in counterparts if item.item_type not in other_side]
    if wrong_side:
        side = "ledger or carry-in" if other_side is BOOK_SIDE else "bank"
        problems.append(f"{', '.join(wrong_side)} must be {side} records")

    total = sum(item.cash_effect for item in counterparts)
    if not wrong_side and total != subject.cash_effect:
        problems.append(f"the selected records total {money(total)} against "
                        f"{money(subject.cash_effect)}; a pairing must agree to the cent")
    return problems
