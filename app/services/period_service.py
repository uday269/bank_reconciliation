"""Period control: readiness, sign-off and lock, reopening (UC-11..UC-13, P7, FR-PER-01..05).

Readiness
    `readiness()` evaluates each CR-10 condition separately and builds the bank-to-book
    statement (BR-17), so the close screen shows which condition is unmet and what the
    difference is made of. `refresh_status()` moves the run between 'in_review' and
    'ready_to_close' as the conditions change; it is a system action with its own event.

Sign-off
    Only a Controller, only from 'ready_to_close', only when every condition holds, and
    never as the only first-level reviewer (CR-05). A non-zero difference needs the
    signer's comment. The sign-off row records the statement and the audit chain head as
    it stood at signing; two events follow, SIGNOFF and PERIOD_LOCKED. The lock is
    enforced here and again by database triggers (FR-PER-03).

Reopening
    A Staff or Senior Accountant requests with a reason; a Controller who is not the
    requester decides (CR-06). Approval returns the run to review, rejection leaves it
    closed, and both states are recorded (FR-PER-04). A reopened period needs a new
    sign-off to close again; the first stays on record (FR-PER-05).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.control import ControlViolation
from app.control import duties, period_lock, permissions, states
from app.control.period_lock import CloseCondition, CloseFacts
from app.domain import exceptions as exception_catalog
from app.domain.rules import Item
from app.domain.statement import Statement, StatementLine, build_statement, money
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.repositories import Repositories
from app.services.blocked import record_blocked

ITEM_TYPES = ("bank", "ledger", "carry_in")
EXTERNAL_COLUMN = {"bank": "external_txn_id", "ledger": "external_entry_id",
                   "carry_in": "external_item_id"}
DATE_COLUMN = {"bank": "transaction_date", "ledger": "posting_date", "carry_in": "original_date"}
CATEGORIES = ("CAT-01", "CAT-02", "CAT-03", "CAT-04", "CAT-05")

# Events that change what the report package must show. A package generated before the
# latest of these is out of date, which leaves CR-10 condition 5 unmet.
STATE_CHANGING_EVENTS = frozenset({"DECISION", "ADJUSTMENT_PROPOSED", "ADJUSTMENT_DECIDED",
                                   "REOPEN_DECIDED", "RULE"})


class PeriodInputError(ValueError):
    """A required field is missing. Nothing has been written."""


@dataclass(frozen=True)
class Readiness:
    run_id: int
    status: str
    conditions: list[CloseCondition]
    statement: Statement
    chain_head: str

    @property
    def ready_for_signoff(self) -> bool:
        return period_lock.ready_for_signoff(self.conditions)


class PeriodService:
    def __init__(self, database: Database, repositories: Repositories, audit: AuditLog):
        self.database = database
        self.repositories = repositories
        self.audit = audit

    # -- statement and readiness --------------------------------------------
    def statement(self, run_id: int, connection: sqlite3.Connection | None = None) -> Statement:
        """BR-17 from the current dispositions. Shared with the Reconciliation Summary (RPT-09)."""
        balances = {row["file_type"]: row for row in self.repositories.source_files.for_run(run_id)}
        if "bank" not in balances or "gl" not in balances:
            raise PeriodInputError("the statement needs both the bank statement and the GL cash detail")

        # An exception the reviewer paired by hand (a 'modify' decision) is a match now,
        # not a reconciling item; every other live exception keeps its section.
        live_exceptions: dict[tuple[str, int], sqlite3.Row] = {}
        for category in CATEGORIES:
            for row in self.repositories.recommendations.queue(run_id, category, ("open", "decided")):
                if row["kind"] != "exception":
                    continue
                latest = self.repositories.decisions.latest(row["recommendation_id"], connection)
                if latest is not None and latest["decision"] == "modify":
                    continue
                live_exceptions[(row["subject_item_type"], row["subject_item_id"])] = row

        lines: list[StatementLine] = []
        for item_type in ITEM_TYPES:
            for row in self.repositories.items.list(run_id, item_type):
                if row["status"] == "excluded":
                    continue
                key = (item_type, row[ITEM_ID[item_type]])
                recommendation = live_exceptions.get(key)
                if row["status"] != "unresolved" and recommendation is None:
                    continue                                   # matched: not a reconciling item
                code = recommendation["exception_code"] if recommendation else None
                section = exception_catalog.statement_section(code) if code else None
                if row["status"] == "unresolved" or section is None:
                    section = "unresolved"
                lines.append(StatementLine(
                    item_ref=row[EXTERNAL_COLUMN[item_type]], section=section,
                    amount_cents=row["amount_cents"],
                    cash_effect_cents=Item.from_row(item_type, row).cash_effect,
                    exception_code=code, description=row["description_original"],
                    business_date=row[DATE_COLUMN[item_type]], status=row["status"]))

        return build_statement(balances["bank"]["closing_balance_cents"],
                               balances["gl"]["closing_balance_cents"], lines)

    def readiness(self, run_id: int, signoff_comment: str | None = None) -> Readiness:
        run = self.repositories.runs.get(run_id)
        statement = self.statement(run_id)
        counts = {item_type: self.repositories.items.status_counts(run_id, item_type)
                  for item_type in ITEM_TYPES}
        total = lambda status: sum(c.get(status, 0) for c in counts.values())  # noqa: E731
        verification = self.audit.verify(run_id)
        facts = CloseFacts(
            items_awaiting_decision=total("proposed"),
            escalations_open=total("escalated"),
            adjustments_pending=len(self.repositories.adjustments.for_run(run_id, ["proposed"])),
            chain_intact=verification.intact,
            package_generated=self._package_is_current(run_id),
            approved_not_verified=total("approved") + total("report_verified"),
            unresolved_difference_cents=statement.unresolved_difference_cents,
            signoff_comment=signoff_comment)
        return Readiness(run_id, run["status"], period_lock.evaluate_close_conditions(facts),
                         statement, verification.head_hash)

    def refresh_status(self, run_id: int) -> str:
        """System transition between 'in_review' and 'ready_to_close' (DOC-03 run lifecycle)."""
        readiness = self.readiness(run_id)
        current = readiness.status
        target = current
        if current == "in_review" and readiness.ready_for_signoff:
            target = "ready_to_close"
        elif current == "ready_to_close" and not readiness.ready_for_signoff:
            target = "in_review"
        if target == current:
            return current
        with self.database.transaction() as connection:
            states.require_run_transition(current, target)
            self.repositories.runs.set_status(connection, run_id, target)
            self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                event_type="STATUS_CHANGED", process_code="P7", entity_type="reconciliation_run",
                entity_id=run_id, rule_name="CR-10",
                revised_values={"conditions": [
                    {"number": c.number, "met": c.met, "detail": c.detail}
                    for c in readiness.conditions]},
                previous_status=current, new_status=target))
        return target

    # -- sign-off and lock (UC-11) ------------------------------------------
    def sign_off(self, run_id: int, user_id: int, comment: str | None = None,
                 at: str | None = None) -> int:
        """Sign, record the statement and chain head, and lock. Returns the sign-off id."""
        at = at or utc_now()
        try:
            readiness = self.readiness(run_id, signoff_comment=comment)
            statement = readiness.statement
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(run_id, connection)
                permissions.require_role(user, permissions.SIGN_OFF)
                if run["status"] in period_lock.LOCKED:
                    raise ControlViolation("CR-18", "the period is already closed")
                period_lock.require_close_ready(readiness.conditions)
                if run["status"] != "ready_to_close":
                    raise ControlViolation("STATE", "the run must be ready to close before sign-off")
                duties.require_independent_signer(
                    user_id, self.repositories.decisions.deciders(run_id, "first", connection))

                chain_head = self.audit.head_hash(run_id, connection)
                figures = statement.as_dict()
                signoff_id = self.repositories.periods.add_signoff(
                    connection, run_id=run_id, signed_by=user_id, signed_at=at,
                    bank_ending_balance_cents=statement.bank_ending_balance_cents,
                    book_ending_balance_cents=statement.book_ending_balance_cents,
                    deposits_in_transit_cents=statement.deposits_in_transit_cents,
                    outstanding_checks_cents=statement.outstanding_checks_cents,
                    bank_originated_cents=statement.bank_originated_net_cents,
                    unresolved_difference_cents=statement.unresolved_difference_cents,
                    unresolved_item_count=statement.unresolved_item_count,
                    comment=(comment or "").strip() or None, chain_head_hash=chain_head)

                states.require_run_transition("ready_to_close", "closed")
                self.repositories.runs.set_status(connection, run_id, "closed")
                context = RunContext.load(self.database, run_id)
                self.audit.write(connection, context, AuditEvent(
                    event_type="SIGNOFF", process_code="P7", entity_type="period_signoff",
                    entity_id=signoff_id, actor_type="human", actor_user_id=user_id,
                    rule_name="CR-10", comment=comment, approval_status="signed",
                    evidence_refs=[f"chain_head:{chain_head}"], revised_values=figures,
                    explanation=f"Unresolved difference {money(statement.unresolved_difference_cents)}",
                    previous_status="ready_to_close", new_status="closed", created_at=at))
                self.audit.write(connection, context, AuditEvent(
                    event_type="PERIOD_LOCKED", process_code="P7", entity_type="reconciliation_run",
                    entity_id=run_id, rule_name="FR-PER-03",
                    evidence_refs=[f"period_signoff:{signoff_id}"],
                    previous_status="ready_to_close", new_status="closed", created_at=at))
                return signoff_id
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id,
                           process_code="P7", entity_type="reconciliation_run", entity_id=run_id,
                           attempted="sign off", violation=violation, comment=comment)
            raise

    # -- reopening (UC-12, UC-13) -------------------------------------------
    def request_reopen(self, run_id: int, user_id: int, reason: str, at: str | None = None) -> int:
        if not (reason or "").strip():
            raise PeriodInputError("a reason is required to request reopening (FR-PER-04)")
        at = at or utc_now()
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(run_id, connection)
                permissions.require_role(user, permissions.REQUEST_REOPEN)
                period_lock.require_reopen_requestable(
                    run, self.repositories.periods.pending_reopen_request(run_id, connection))

                request_id = self.repositories.periods.add_reopen_request(
                    connection, run_id=run_id, requested_by=user_id, reason=reason.strip(),
                    requested_at=at, original_status=run["status"])
                states.require_run_transition("closed", "reopen_requested")
                self.repositories.runs.set_status(connection, run_id, "reopen_requested")
                self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                    event_type="REOPEN_REQUESTED", process_code="P7", entity_type="reopen_request",
                    entity_id=request_id, actor_type="human", actor_user_id=user_id,
                    comment=reason.strip(), approval_status="requested",
                    previous_status="closed", new_status="reopen_requested", created_at=at))
                return request_id
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id,
                           process_code="P7", entity_type="reconciliation_run", entity_id=run_id,
                           attempted="request reopening", violation=violation, comment=reason)
            raise

    def decide_reopen(self, request_id: int, user_id: int, decision: str,
                      comment: str | None = None, at: str | None = None) -> str:
        """Approve (back to review) or reject (stays closed). Returns the run's new status."""
        if decision not in ("approve", "reject"):
            raise ValueError(f"a reopening is approved or rejected, not {decision!r}")
        at = at or utc_now()
        request = self.repositories.periods.get_reopen_request(request_id)
        run_id = request["run_id"]
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(run_id, connection)
                permissions.require_role(user, permissions.DECIDE_REOPEN)
                period_lock.require_reopen_decidable(
                    run, request, self.repositories.periods.reopen_decision(request_id, connection))
                duties.require_independent_reopen_approver(user_id, request["requested_by"])
                permissions.require_comment(decision, "low", comment)

                revised = "in_review" if decision == "approve" else "closed"
                states.require_run_transition("reopen_requested", revised)
                decision_id = self.repositories.periods.add_reopen_decision(
                    connection, reopen_request_id=request_id, decision=decision,
                    decided_by=user_id, decided_at=at, revised_status=revised, comment=comment)
                self.repositories.runs.set_status(connection, run_id, revised)
                self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                    event_type="REOPEN_DECIDED", process_code="P7", entity_type="reopen_request",
                    entity_id=request_id, actor_type="human", actor_user_id=user_id,
                    rule_name="CR-06", decision=decision, comment=comment,
                    approval_status="approved" if decision == "approve" else "rejected",
                    evidence_refs=[f"reopen_decision:{decision_id}"],
                    original_values={"status": request["original_status"],
                                     "requested_by": request["requested_by"],
                                     "reason": request["reason"]},
                    revised_values={"status": revised},
                    previous_status="reopen_requested", new_status=revised, created_at=at))
                return revised
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id,
                           process_code="P7", entity_type="reopen_request", entity_id=request_id,
                           attempted=f"{decision} reopening", violation=violation, comment=comment)
            raise

    # -- helpers ------------------------------------------------------------
    def _package_is_current(self, run_id: int) -> bool:
        """CR-10 condition 5: all 13 reports exist and none predates the latest change."""
        current = self.repositories.reports.current(run_id)
        if {row["report_code"] for row in current} != {f"RPT-{n:02d}" for n in range(1, 14)}:
            return False
        last_change = last_package = 0
        for event in self.audit.events(run_id):
            if event["event_type"] in STATE_CHANGING_EVENTS:
                last_change = event["sequence_no"]
            elif event["event_type"] == "REPORT_GENERATED":
                last_package = event["sequence_no"]
        return last_package > last_change


ITEM_ID = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id", "carry_in": "carry_in_item_id"}
