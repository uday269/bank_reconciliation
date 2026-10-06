"""Proposed adjustments (UC-08, UC-09, P6, FR-ADJ-01..04).

An adjustment is a proposed correcting entry with an approval trail. The system never
posts it anywhere (CR-12, NOP-01): approved adjustments appear in the Adjustment
Report (RPT-07) for someone to enter in the general ledger, and nothing in this
codebase writes to a ledger.

Who
    Staff and Senior Accountants propose; a Senior Accountant or Controller who is not
    the preparer decides (CR-04). The system pre-fills a suggestion from the exception
    category (BR-16) but never creates an adjustment itself, so every adjustment has a
    human preparer and CR-04 always has someone to compare against.

What may be adjusted
    Exceptions in EXC-03 (bank fee), EXC-04 (bank interest) and EXC-06 (missing ledger
    entry), as UC-08 states. One side of the entry must be the bank account's cash
    account, because a bank reconciliation adjustment corrects book cash. Only one live
    adjustment (proposed or approved) per recommendation, so an item cannot be adjusted
    twice; a rejected adjustment can be replaced by a new proposal.

Refusals
    Missing or invalid fields raise `AdjustmentInputError` and nothing is written: that
    is a form to complete, not a control firing. Control refusals (role, lock, CR-04,
    category) raise `ControlViolation` and leave a BLOCKED_ATTEMPT event.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.control import ControlViolation
from app.control import duties, period_lock, permissions, states
from app.domain import exceptions as exception_catalog
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.repositories import Repositories
from app.services.blocked import record_blocked

ADJUSTABLE_EXCEPTIONS = frozenset({"EXC-03", "EXC-04", "EXC-06"})      # UC-08
LIVE_STATUSES = ("proposed", "approved")


class AdjustmentInputError(ValueError):
    """The proposal is incomplete or inconsistent. Every problem is listed at once."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class Suggestion:
    """Pre-filled values for the W5 form. The preparer confirms or changes them."""

    amount_cents: int
    debit_account_code: str
    credit_account_code: str
    rationale: str


class AdjustmentService:
    def __init__(self, database: Database, repositories: Repositories, audit: AuditLog):
        self.database = database
        self.repositories = repositories
        self.audit = audit

    # -- reading ------------------------------------------------------------
    def suggestion(self, recommendation_id: int) -> Suggestion | None:
        """BR-16: the entry the exception category suggests, or None if it suggests none."""
        recommendation = self.repositories.recommendations.get(recommendation_id)
        code = recommendation["exception_code"]
        if code not in ADJUSTABLE_EXCEPTIONS:
            return None
        category = exception_catalog.get(code)
        item = self.repositories.items.get(recommendation["subject_item_type"],
                                           recommendation["subject_item_id"])
        description = item["description_original"] if "description_original" in item.keys() else ""
        return Suggestion(item["amount_cents"], category.adjustment_debit,
                          category.adjustment_credit,
                          f"{category.name}: {description}".strip().rstrip(":"))

    def awaiting_approval(self, run_id: int) -> list[sqlite3.Row]:
        """The approver's queue: adjustments still proposed, oldest first."""
        return self.repositories.adjustments.for_run(run_id, ["proposed"])

    # -- propose (UC-08) ----------------------------------------------------
    def propose(self, recommendation_id: int, user_id: int, *, amount_cents: int,
                debit_account_code: str, credit_account_code: str, rationale: str,
                evidence_refs: str | None = None, at: str | None = None) -> int:
        at = at or utc_now()
        recommendation = self.repositories.recommendations.get(recommendation_id)
        run_id = recommendation["run_id"]
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(run_id, connection)
                recommendation = self.repositories.recommendations.get(recommendation_id, connection)

                permissions.require_role(user, permissions.PROPOSE_ADJUSTMENT)
                period_lock.require_writable(run, "propose an adjustment")
                self._require_adjustable(recommendation, connection)
                self._validate_entry(run, amount_cents, debit_account_code,
                                     credit_account_code, rationale, connection)

                adjustment_id = self.repositories.adjustments.create(
                    connection, run_id=run_id, recommendation_id=recommendation_id,
                    amount_cents=amount_cents, debit_account_code=debit_account_code,
                    credit_account_code=credit_account_code, rationale=rationale.strip(),
                    evidence_refs=evidence_refs, prepared_by=user_id, prepared_at=at)

                self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                    event_type="ADJUSTMENT_PROPOSED", process_code="P6", entity_type="adjustment",
                    entity_id=adjustment_id, actor_type="human", actor_user_id=user_id,
                    item_refs=[self._subject_ref(recommendation, connection)],
                    rule_name=recommendation["rule_name"], risk_level=recommendation["risk_level"],
                    explanation=rationale.strip(), approval_status="proposed",
                    evidence_refs=[f"recommendation:{recommendation_id}"]
                    + ([evidence_refs] if evidence_refs else []),
                    revised_values={"amount_cents": amount_cents,
                                    "debit_account_code": debit_account_code,
                                    "credit_account_code": credit_account_code,
                                    "exception_code": recommendation["exception_code"],
                                    "posted": False},
                    new_status="proposed", created_at=at))
                self._return_to_review(connection, run)
                return adjustment_id
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id,
                           process_code="P6", entity_type="recommendation",
                           entity_id=recommendation_id, attempted="propose adjustment",
                           violation=violation)
            raise

    # -- decide (UC-09) -----------------------------------------------------
    def decide(self, adjustment_id: int, user_id: int, decision: str,
               comment: str | None = None, at: str | None = None) -> str:
        """Approve or reject. Returns the new status. Approval never posts the entry."""
        if decision not in ("approve", "reject"):
            raise ValueError(f"an adjustment is approved or rejected, not {decision!r}")
        at = at or utc_now()
        adjustment = self.repositories.adjustments.get(adjustment_id)
        run_id = adjustment["run_id"]
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(run_id, connection)
                adjustment = self.repositories.adjustments.get(adjustment_id, connection)

                permissions.require_role(user, permissions.DECIDE_ADJUSTMENT)
                # The adjustment_decision table has no run column, so no trigger can lock
                # it: this check is the lock for adjustment decisions.
                period_lock.require_writable(run, "decide an adjustment")
                if adjustment["status"] != "proposed":
                    raise ControlViolation("STATE", f"adjustment {adjustment_id} has already been "
                                                    f"{adjustment['status']}")
                duties.require_independent_adjustment_approver(user_id, adjustment["prepared_by"])
                permissions.require_comment(decision, "low", comment)

                new_status = "approved" if decision == "approve" else "rejected"
                decision_id = self.repositories.adjustments.add_decision(
                    connection, adjustment_id=adjustment_id, decision=decision,
                    decided_by=user_id, decided_at=at, comment=comment)
                self.repositories.adjustments.set_status(connection, adjustment_id, new_status)

                recommendation = self.repositories.recommendations.get(
                    adjustment["recommendation_id"], connection)
                self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                    event_type="ADJUSTMENT_DECIDED", process_code="P6", entity_type="adjustment",
                    entity_id=adjustment_id, actor_type="human", actor_user_id=user_id,
                    item_refs=[self._subject_ref(recommendation, connection)],
                    decision=decision, comment=comment, approval_status=new_status,
                    evidence_refs=[f"adjustment_decision:{decision_id}",
                                   f"recommendation:{adjustment['recommendation_id']}"],
                    original_values={"status": "proposed", "prepared_by": adjustment["prepared_by"]},
                    revised_values={"status": new_status, "amount_cents": adjustment["amount_cents"],
                                    "debit_account_code": adjustment["debit_account_code"],
                                    "credit_account_code": adjustment["credit_account_code"],
                                    "posted": False},
                    previous_status="proposed", new_status=new_status, created_at=at))
                return new_status
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id,
                           process_code="P6", entity_type="adjustment", entity_id=adjustment_id,
                           attempted=f"{decision} adjustment", violation=violation, comment=comment)
            raise

    # -- checks -------------------------------------------------------------
    def _require_adjustable(self, recommendation: sqlite3.Row,
                            connection: sqlite3.Connection) -> None:
        code = recommendation["exception_code"]
        if recommendation["kind"] != "exception" or code not in ADJUSTABLE_EXCEPTIONS:
            raise ControlViolation(
                "FR-ADJ-01", "adjustments are proposed for bank fees, bank interest and missing "
                             f"ledger entries (EXC-03, EXC-04, EXC-06); this item is {code or 'a match'}")
        if recommendation["status"] == "superseded":
            raise ControlViolation("STATE", "this recommendation was superseded; use its replacement")
        live = [row for row in self.repositories.adjustments.for_recommendation(
            recommendation["recommendation_id"], connection) if row["status"] in LIVE_STATUSES]
        if live:
            raise ControlViolation(
                "STATE", f"adjustment {live[0]['adjustment_id']} for this item is already {live[0]['status']}")

    def _validate_entry(self, run: sqlite3.Row, amount_cents: int, debit: str, credit: str,
                        rationale: str, connection: sqlite3.Connection) -> None:
        """UC-08 step 2: amount, both accounts and a rationale, all reported together."""
        problems: list[str] = []
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
            problems.append("amount must be a positive whole number of cents")
        known = {row["gl_account_code"] for row in self.repositories.accounts.gl_accounts(connection)}
        for label, code in (("debit", debit), ("credit", credit)):
            if not code:
                problems.append(f"{label} account is required")
            elif code not in known:
                problems.append(f"{label} account {code} is not in the chart of accounts")
        if debit and credit and debit == credit:
            problems.append("debit and credit accounts must differ")
        cash = self.repositories.accounts.get_bank_account(run["bank_account_id"],
                                                           connection)["gl_account_code"]
        if debit and credit and cash not in (debit, credit):
            problems.append(f"one side of the entry must be the cash account {cash}")
        if not (rationale or "").strip():
            problems.append("a rationale is required")
        if problems:
            raise AdjustmentInputError(problems)

    # -- helpers ------------------------------------------------------------
    def _subject_ref(self, recommendation: sqlite3.Row, connection: sqlite3.Connection) -> str:
        column = {"bank": "external_txn_id", "ledger": "external_entry_id",
                  "carry_in": "external_item_id"}[recommendation["subject_item_type"]]
        return self.repositories.items.get(recommendation["subject_item_type"],
                                           recommendation["subject_item_id"], connection)[column]

    def _return_to_review(self, connection: sqlite3.Connection, run: sqlite3.Row) -> None:
        """A new proposal while ready to close reopens review: CR-10 condition 3 is unmet again."""
        if run["status"] == "ready_to_close":
            states.require_run_transition("ready_to_close", "in_review")
            self.repositories.runs.set_status(connection, run["run_id"], "in_review")
