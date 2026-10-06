"""Separation of duties (CR-03..CR-06, DD-05).

Each check compares user identifiers across records. Identity comes from the identity
selector, not a login (DD-05), so these rules prove the control logic, not who sat at
the keyboard; DOC-05 section 12 states that limit. The functions are pure: services
gather the identifiers from the repositories on the transaction's connection and pass
them in.
"""

from __future__ import annotations

from typing import Iterable

from app.control.errors import ControlViolation


def require_independent_senior(approver_id: int, escalated_by: int | None,
                               first_level_reviewers: Iterable[int]) -> None:
    """CR-03: the senior approver cannot be the user who reviewed or escalated the item."""
    if escalated_by is not None and approver_id == escalated_by:
        raise ControlViolation(
            "CR-03", "the senior decision must come from someone other than the user who escalated it")
    if approver_id in set(first_level_reviewers):
        raise ControlViolation(
            "CR-03", "the senior decision must come from someone other than a first-level reviewer of the item")


def require_independent_adjustment_approver(approver_id: int, prepared_by: int) -> None:
    """CR-04: the approver of an adjustment cannot be its preparer."""
    if approver_id == prepared_by:
        raise ControlViolation("CR-04", "an adjustment must be approved by someone other than its preparer")


def require_independent_signer(signer_id: int, first_level_deciders: Iterable[int]) -> None:
    """CR-05: the signer cannot be the only user who made first-level decisions in the run.

    A run with no first-level decisions passes this check; CR-10 refuses it instead,
    because items would still be awaiting a decision.
    """
    deciders = set(first_level_deciders)
    if deciders and deciders == {signer_id}:
        raise ControlViolation(
            "CR-05", "the period cannot be signed off by the only user who reviewed its items")


def require_independent_reopen_approver(approver_id: int, requested_by: int) -> None:
    """CR-06: the approver of a reopening cannot be its requester."""
    if approver_id == requested_by:
        raise ControlViolation("CR-06", "a reopening must be approved by someone other than its requester")
