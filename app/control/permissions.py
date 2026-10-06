"""Who may do what: the permission matrix, decision levels, offered actions and the
comment rule (DOC-02 section 2.1, CR-01, CR-02, CR-16, FR-REV-04, FR-REV-10).

These functions decide nothing about an item. They answer one question each and raise
`ControlViolation` when the answer is no. Services call them; routers never do, so a
new route cannot skip a check (ADR-02). Inputs are plain mappings (database rows work),
which keeps every rule testable without a database.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.control.errors import ControlViolation
from app.domain import exceptions as exception_catalog

STAFF, SENIOR, CONTROLLER = "ROL-01", "ROL-02", "ROL-03"
ROLE_NAMES = {STAFF: "Staff Accountant", SENIOR: "Senior Accountant", CONTROLLER: "Controller"}

# Actions, as named in the permission matrix.
IMPORT = "import and start a run"
DECIDE_FIRST = "decide a CAT-01..04 item"
BATCH = "approve or reject an exact-match batch"
ESCALATE = "escalate an item"
DECIDE_SENIOR = "decide a CAT-05 or escalated item"
PROPOSE_ADJUSTMENT = "propose an adjustment"
DECIDE_ADJUSTMENT = "approve or reject an adjustment"
SIGN_OFF = "sign off and close the period"
REQUEST_REOPEN = "request reopening"
DECIDE_REOPEN = "approve or reject reopening"
VIEW = "view items, reports and the audit log"

# Human roles only. ROL-04 (the system) acts through services, never through this matrix.
PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    IMPORT: frozenset({STAFF, SENIOR}),
    DECIDE_FIRST: frozenset({STAFF, SENIOR}),
    BATCH: frozenset({STAFF, SENIOR}),
    ESCALATE: frozenset({STAFF, SENIOR}),
    DECIDE_SENIOR: frozenset({SENIOR, CONTROLLER}),
    PROPOSE_ADJUSTMENT: frozenset({STAFF, SENIOR}),
    DECIDE_ADJUSTMENT: frozenset({SENIOR, CONTROLLER}),
    SIGN_OFF: frozenset({CONTROLLER}),
    REQUEST_REOPEN: frozenset({STAFF, SENIOR}),
    DECIDE_REOPEN: frozenset({CONTROLLER}),
    VIEW: frozenset({STAFF, SENIOR, CONTROLLER}),
}

DECISIONS = ("approve", "reject", "modify", "escalate", "unresolved")

# FR-REV-04: the actions offered per category at first level, and at senior level.
# 'reject' means "this pairing is wrong" and 'modify' means "a different candidate is
# right", so both apply to proposed matches only. An exception has no pairing to
# reject: its disposition is approved, escalated or left unresolved.
FIRST_LEVEL_ACTIONS: dict[str, frozenset[str]] = {
    "CAT-01": frozenset({"approve", "reject", "escalate"}),
    "CAT-02": frozenset({"approve", "reject", "modify", "escalate"}),
    "CAT-03": frozenset({"approve", "reject", "modify", "escalate", "unresolved"}),
    "CAT-04": frozenset({"approve", "escalate", "unresolved"}),
}
SENIOR_ACTIONS = frozenset({"approve", "reject", "modify", "unresolved"})
MATCH_ONLY_ACTIONS = frozenset({"reject", "modify"})

# Exceptions whose only sound outcome is investigation (duplicates, unexplained items,
# capped group searches) cannot be "approved": approving would mark them reconciled
# while the money is still unexplained. The reviewer pairs them by hand, escalates them,
# or records them as unresolved, which keeps them on the statement.
NOT_APPROVABLE_DISPOSITIONS = frozenset({"unresolved", "investigate"})

COMMENT_REQUIRED = frozenset({"reject", "modify", "escalate", "unresolved"})   # CR-16


def is_permitted(role_code: str, action: str) -> bool:
    if action not in PERMISSION_MATRIX:
        raise ValueError(f"unknown action {action!r}")
    return role_code in PERMISSION_MATRIX[action]


def require_role(user: Mapping[str, Any], action: str) -> None:
    """Refuse an action the user's role does not carry, or any action by an inactive user."""
    if not user["is_active"]:
        raise ControlViolation("ROLE", f"user {user['user_id']} is deactivated and cannot {action}")
    if not is_permitted(user["role_code"], action):
        allowed = ", ".join(ROLE_NAMES[r] for r in sorted(PERMISSION_MATRIX[action]))
        raise ControlViolation(
            "ROLE", f"{ROLE_NAMES.get(user['role_code'], user['role_code'])} cannot {action}; "
                    f"permitted for {allowed}")


def decision_level(recommendation: Mapping[str, Any], item_status: str) -> str:
    """CR-02: CAT-05 and escalated items need a senior decision; everything else is first level."""
    if recommendation["category_code"] == "CAT-05" or item_status == "escalated":
        return "senior"
    return "first"


def offered_actions(recommendation: Mapping[str, Any], item_status: str) -> frozenset[str]:
    """The decisions the interface offers for this item (FR-REV-04)."""
    if decision_level(recommendation, item_status) == "senior":
        actions = SENIOR_ACTIONS
    else:
        actions = FIRST_LEVEL_ACTIONS[recommendation["category_code"]]
    kind = recommendation["kind"] if "kind" in recommendation.keys() else "match"
    if kind == "exception":
        actions = actions - MATCH_ONLY_ACTIONS
        code = recommendation["exception_code"] if "exception_code" in recommendation.keys() else None
        if code and exception_catalog.get(code).default_disposition in NOT_APPROVABLE_DISPOSITIONS:
            actions = actions - {"approve"}
    return actions


def require_decision_permitted(user: Mapping[str, Any], recommendation: Mapping[str, Any],
                               item_status: str, decision: str) -> str:
    """Role, level and offered action for one decision. Returns the decision level."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    level = decision_level(recommendation, item_status)
    if level == "senior":
        if not is_permitted(user["role_code"], DECIDE_SENIOR):
            raise ControlViolation(
                "CR-02", f"{recommendation['category_code']} and escalated items require a "
                         "Senior Accountant or Controller decision")
        require_role(user, DECIDE_SENIOR)
    else:
        require_role(user, ESCALATE if decision == "escalate" else DECIDE_FIRST)

    if decision not in offered_actions(recommendation, item_status):
        raise ControlViolation(
            "FR-REV-04", f"'{decision}' is not offered for a {level}-level "
                         f"{recommendation['category_code']} item")
    return level


def require_manual_match_permitted(user: Mapping[str, Any], recommendation: Mapping[str, Any],
                                   item_status: str) -> str:
    """A reviewer pairs an exception by hand. Same roles and levels as any decision.

    Recorded as a 'modify' decision, so the comment and detail-view rules apply as they
    do to selecting a different candidate. Matches already have candidates to choose
    from; this action is for exceptions only.
    """
    kind = recommendation["kind"] if "kind" in recommendation.keys() else "match"
    if kind != "exception":
        raise ControlViolation("FR-REV-04", "manual pairing is for exceptions; for a proposed match, "
                                            "select one of its candidates instead")
    level = decision_level(recommendation, item_status)
    if level == "senior":
        if not is_permitted(user["role_code"], DECIDE_SENIOR):
            raise ControlViolation(
                "CR-02", f"{recommendation['category_code']} and escalated items require a "
                         "Senior Accountant or Controller decision")
        require_role(user, DECIDE_SENIOR)
    else:
        require_role(user, DECIDE_FIRST)
    return level


def require_comment(decision: str, risk_level: str, comment: str | None) -> None:
    """CR-16: reject, modify, escalate, unresolved, and approving a High-risk item need a comment."""
    needed = decision in COMMENT_REQUIRED or (decision == "approve" and risk_level == "high")
    if needed and not (comment or "").strip():
        reason = "approving a High-risk item" if decision == "approve" else f"'{decision}'"
        raise ControlViolation("CR-16", f"{reason} requires a comment")


def require_detail_opened(recommendation: Mapping[str, Any], decision: str,
                          opened_at: str | None) -> None:
    """FR-REV-10: approving or modifying a non-exact item requires its detail view first.

    Exact matches (CAT-01, produced by a deterministic rule) are exempt, which is what
    makes batch approval possible. Everything scored by the AI layer is not.
    """
    exact = recommendation["category_code"] == "CAT-01" and recommendation["source"] == "rule"
    if decision in ("approve", "modify") and not exact and not opened_at:
        raise ControlViolation(
            "FR-REV-10", "approving a non-exact item requires opening its detail view first")


def require_batch_eligible(run_id: int, recommendations: list[Mapping[str, Any]],
                           item_statuses: Mapping[int, str]) -> None:
    """CR-11 (DD-02, pending): a batch holds only open CAT-01 Low-risk items of one run.

    `item_statuses` maps recommendation_id to the subject item's current status. The
    reviewer may remove items before approving; whatever remains must all qualify, and
    one ineligible item refuses the whole batch rather than being skipped silently.
    """
    if not recommendations:
        raise ControlViolation("CR-11", "a batch must contain at least one item")
    problems = []
    for row in recommendations:
        rec_id = row["recommendation_id"]
        if row["run_id"] != run_id:
            problems.append(f"recommendation {rec_id} belongs to another run")
        elif row["category_code"] != "CAT-01" or row["risk_level"] != "low":
            problems.append(f"recommendation {rec_id} is {row['category_code']} "
                            f"with {row['risk_level']} risk")
        elif row["status"] != "open" or item_statuses.get(rec_id) != "proposed":
            problems.append(f"recommendation {rec_id} has already been decided")
    if problems:
        raise ControlViolation("CR-11", "a batch may contain only open exact matches with Low risk", problems)
