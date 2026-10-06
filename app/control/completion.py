"""The six-condition reconciliation rule (CR-07, brief section 14).

An item is reconciled only when all six hold. The report service gathers the evidence
for each item and calls `require_reconciled` before the final status change; nothing
else may set 'reconciled'. The conditions are evaluated individually so an item that
falls short shows which condition failed.

Exception dispositions: an item whose approved disposition is an exception (a bank
fee, for example) counts as reconciled once all six conditions hold. This reading is
awaiting the sponsor's confirmation; if it changes, `approval_satisfied` is the place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.control.errors import ControlViolation

CONDITIONS = (
    "Source data passed validation",
    "A match or exception disposition was proposed",
    "The required human approval is recorded",
    "Supporting evidence is linked",
    "The audit event was written",
    "The item appears correctly in the reconciliation report",
)


@dataclass(frozen=True)
class ItemEvidence:
    item_ref: str                   # display identifier, e.g. BT-0412
    validated: bool                 # (1) not excluded; validated before proposal
    disposition_proposed: bool      # (2) a recommendation exists for the item
    approval_recorded: bool         # (3) see approval_satisfied
    evidence_linked: bool           # (4) candidate members or exception code, and an explanation
    audit_written: bool             # (5) a DECISION event exists for the approving decision
    report_verified: bool           # (6) the report check found the item with the expected values

    def flags(self) -> tuple[bool, ...]:
        return (self.validated, self.disposition_proposed, self.approval_recorded,
                self.evidence_linked, self.audit_written, self.report_verified)


def evaluate(evidence: ItemEvidence) -> list[tuple[int, str, bool]]:
    return [(number, label, met)
            for number, (label, met) in enumerate(zip(CONDITIONS, evidence.flags()), start=1)]


def is_reconciled(evidence: ItemEvidence) -> bool:
    return all(evidence.flags())


def require_reconciled(evidence: ItemEvidence) -> None:
    unmet = [f"{number}. {label}" for number, label, met in evaluate(evidence) if not met]
    if unmet:
        raise ControlViolation("CR-07", f"{evidence.item_ref} does not meet every reconciliation condition", unmet)


def approval_satisfied(decisions: Sequence[Mapping[str, Any]], requires_senior: bool) -> bool:
    """Condition 3: the latest decision approves, at senior level where CR-02 requires it.

    `decisions` is the item's history, oldest first. A later reject or escalation
    supersedes an earlier approval, which is why only the latest decision counts.
    """
    if not decisions:
        return False
    latest = decisions[-1]
    if latest["decision"] not in ("approve", "modify"):
        return False
    return latest["decision_level"] == "senior" or not requires_senior
