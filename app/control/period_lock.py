"""Period lock and run-stage gates (CR-10, CR-17, CR-18, FR-PER-01..05).

The database refuses decisions, adjustments and imports in a closed run through
triggers. These checks run first, so a refusal names the rule and becomes a
BLOCKED_ATTEMPT event rather than a database error. The two layers are deliberate:
the trigger is the backstop if a code path ever forgets the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.control.errors import ControlViolation
from app.domain.statement import money

LOCKED = frozenset({"closed", "reopen_requested"})      # a pending reopen does not unlock
WORKING = frozenset({"in_review", "ready_to_close"})    # decisions and adjustments allowed
IMPORTABLE = frozenset({"created", "validating", "validation_failed"})


def require_writable(run: Mapping[str, Any], action: str) -> None:
    """CR-18: no decision or adjustment once closed, and none before matching completes."""
    status = run["status"]
    if status in LOCKED:
        raise ControlViolation("CR-18", f"the period is closed; it must be reopened before you can {action}")
    if status not in WORKING:
        raise ControlViolation("STATE", f"the run is '{status}'; you can {action} once matching has completed")


def require_importable(run: Mapping[str, Any]) -> None:
    status = run["status"]
    if status in LOCKED:
        raise ControlViolation("CR-18", "the period is closed; files cannot be imported")
    if status not in IMPORTABLE:
        raise ControlViolation("STATE", f"the run is '{status}'; files are imported before matching starts")


def require_matchable(run: Mapping[str, Any], has_file_level_failure: bool) -> None:
    """CR-17: matching starts only after every file-level validation check passes."""
    if has_file_level_failure or run["status"] != "processing":
        raise ControlViolation("CR-17", "matching starts only after every file-level validation check passes")


def require_reopen_requestable(run: Mapping[str, Any], pending_request: Mapping[str, Any] | None) -> None:
    if run["status"] != "closed":
        raise ControlViolation("STATE", f"only a closed period can be reopened; the run is '{run['status']}'")
    if pending_request is not None:
        raise ControlViolation("STATE", "a reopen request for this period is already awaiting a decision")


def require_reopen_decidable(run: Mapping[str, Any], request: Mapping[str, Any],
                             existing_decision: Mapping[str, Any] | None) -> None:
    if request["run_id"] != run["run_id"]:
        raise ValueError("reopen request belongs to a different run")
    if existing_decision is not None:
        raise ControlViolation("STATE", "this reopen request has already been decided")
    if run["status"] != "reopen_requested":
        raise ControlViolation("STATE", f"the run is '{run['status']}', not awaiting a reopen decision")


# ---------------------------------------------------------------------------
# Close conditions (CR-10)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloseFacts:
    """What the period service measures before close. Gathered on one connection."""

    items_awaiting_decision: int        # items 'proposed' (escalated counted separately)
    escalations_open: int               # items 'escalated'
    adjustments_pending: int            # adjustments still 'proposed'
    chain_intact: bool                  # audit chain verification result
    package_generated: bool             # RPT-01..13 generated for the current state
    approved_not_verified: int          # approved items the report check has not confirmed (FR-RPT-05)
    unresolved_difference_cents: int    # BR-17
    signoff_comment: str | None = None  # supplied only at sign-off


@dataclass(frozen=True)
class CloseCondition:
    number: int
    label: str
    met: bool
    detail: str


def evaluate_close_conditions(facts: CloseFacts) -> list[CloseCondition]:
    """Each CR-10 condition, individually, so the screen shows which one is unmet."""
    difference = facts.unresolved_difference_cents
    acknowledged = difference == 0 or bool((facts.signoff_comment or "").strip())
    return [
        CloseCondition(1, "No item awaiting decision", facts.items_awaiting_decision == 0,
                       f"{facts.items_awaiting_decision} awaiting decision"),
        CloseCondition(2, "Every escalation decided", facts.escalations_open == 0,
                       f"{facts.escalations_open} escalation(s) open"),
        CloseCondition(3, "Every adjustment approved or rejected", facts.adjustments_pending == 0,
                       f"{facts.adjustments_pending} adjustment(s) awaiting decision"),
        CloseCondition(4, "Audit chain verifies", facts.chain_intact,
                       "intact" if facts.chain_intact else "verification failed"),
        CloseCondition(5, "Report package generated and every approved item verified in it",
                       facts.package_generated and facts.approved_not_verified == 0,
                       ("not generated" if not facts.package_generated else
                        f"{facts.approved_not_verified} approved item(s) not yet verified")),
        CloseCondition(6, "Unresolved difference acknowledged with a comment", acknowledged,
                       ("no difference" if difference == 0 else
                        f"difference of {money(difference)}; comment required at sign-off")),
    ]


def ready_for_signoff(conditions: list[CloseCondition]) -> bool:
    """Conditions 1 to 5. The sixth is satisfied by the signer's comment at sign-off."""
    return all(condition.met for condition in conditions if condition.number <= 5)


def require_close_ready(conditions: list[CloseCondition]) -> None:
    unmet = [f"{c.number}. {c.label}: {c.detail}" for c in conditions if not c.met]
    if unmet:
        raise ControlViolation("CR-10", "the period cannot close until every close condition is met", unmet)
