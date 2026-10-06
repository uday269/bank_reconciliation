"""The one exception every control refusal raises.

A refusal names the rule that blocked the action and carries a stable error code
(DOC-05 section 11, range E-CTL-001..099). Services catch it, roll back the action,
and record a BLOCKED_ATTEMPT audit event in its own transaction, so a refused action
leaves evidence that the control fired (ADR-12) and nothing else.

Messages state the rule, not the user's fault: "requires a comment (CR-16)", never
"you forgot the comment".
"""

from __future__ import annotations

# Rule identifier -> error code. Rule identifiers are the IDs used in DOC-02.
ERROR_CODES: dict[str, str] = {
    "ROLE": "E-CTL-001",        # permission matrix, DOC-02 section 2.1
    "CR-02": "E-CTL-002",       # senior decision required
    "CR-03": "E-CTL-003",       # senior approver is not the reviewer or escalator
    "CR-04": "E-CTL-004",       # adjustment approver is not the preparer
    "CR-05": "E-CTL-005",       # signer is not the only first-level reviewer
    "CR-06": "E-CTL-006",       # reopen approver is not the requester
    "CR-07": "E-CTL-007",       # six reconciliation conditions
    "CR-10": "E-CTL-010",       # close conditions
    "CR-11": "E-CTL-011",       # batch contents
    "CR-16": "E-CTL-016",       # comment required
    "CR-17": "E-CTL-017",       # matching before validation
    "CR-18": "E-CTL-018",       # locked period
    "FR-REV-04": "E-CTL-020",   # action not offered for this category
    "FR-REV-10": "E-CTL-021",   # detail view not opened
    "FR-ADJ-01": "E-CTL-022",   # adjustment proposed for an item that does not take one
    "STATE": "E-CTL-030",       # transition not in the DOC-03 state models
}


class ControlViolation(Exception):
    """An action refused by a control rule. Nothing has been written when it is raised."""

    def __init__(self, rule: str, message: str, details: list[str] | None = None):
        if rule not in ERROR_CODES:
            raise ValueError(f"unknown control rule {rule!r}")
        self.rule = rule
        self.code = ERROR_CODES[rule]
        self.message = message
        self.details = list(details or [])
        super().__init__(f"{self.code} [{rule}] {message}")
