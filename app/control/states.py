"""Allowed status transitions, copied from the DOC-03 state models.

Every status change goes through `require_item_transition` or `require_run_transition`,
so code cannot move an item or a run along a path the state models do not show. A
transition missing from these tables is a design change: update DOC-03 first.
"""

from __future__ import annotations

from app.control.errors import ControlViolation

# Item lifecycle (DOC-03, state_item_lifecycle). Self-loop on 'proposed' is a reject
# that requeues the item with its next candidate or as an exception.
ITEM_TRANSITIONS: dict[str, frozenset[str]] = {
    "imported": frozenset({"excluded", "validated"}),
    "validated": frozenset({"proposed"}),
    "proposed": frozenset({"approved", "escalated", "proposed", "unresolved"}),
    "escalated": frozenset({"approved", "proposed", "unresolved"}),
    "approved": frozenset({"report_verified"}),
    "report_verified": frozenset({"reconciled"}),
    "reconciled": frozenset({"proposed"}),                       # reopened and reversed
    "unresolved": frozenset({"proposed"}),                       # new evidence or reopened
    "excluded": frozenset(),
}

# Run and period lifecycle (DOC-03, state_period_lifecycle).
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"validating"}),
    "validating": frozenset({"validation_failed", "processing"}),
    "validation_failed": frozenset({"validating"}),
    "processing": frozenset({"in_review"}),
    "in_review": frozenset({"ready_to_close"}),
    "ready_to_close": frozenset({"in_review", "closed"}),
    "closed": frozenset({"reopen_requested"}),
    "reopen_requested": frozenset({"closed", "in_review"}),
}

# Statuses an item can hold while awaiting a human decision (input to CR-10).
AWAITING_DECISION = frozenset({"proposed", "escalated"})


def require_item_transition(current: str, new: str) -> None:
    if current not in ITEM_TRANSITIONS:
        raise ValueError(f"unknown item status {current!r}")
    if new not in ITEM_TRANSITIONS[current]:
        raise ControlViolation("STATE", f"an item cannot move from '{current}' to '{new}'")


def require_run_transition(current: str, new: str) -> None:
    if current not in RUN_TRANSITIONS:
        raise ValueError(f"unknown run status {current!r}")
    if new not in RUN_TRANSITIONS[current]:
        raise ControlViolation("STATE", f"a run cannot move from '{current}' to '{new}'")
