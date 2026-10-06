"""Evidence for refused actions (ADR-12), shared by every service that applies controls.

A control refusal rolls back the action's transaction, so its evidence has to be
written afterwards, in a transaction of its own. The event names the rule that fired,
its error code and the details the reviewer saw. Nothing else is written.
"""

from __future__ import annotations

from typing import Any

from app.control import ControlViolation
from app.infra.audit import AuditEvent, AuditLog, RunContext
from app.infra.db import Database


def record_blocked(database: Database, audit: AuditLog, *, run_id: int, user_id: int,
                   process_code: str, entity_type: str, entity_id: int, attempted: str,
                   violation: ControlViolation, comment: str | None = None,
                   extra: dict[str, Any] | None = None) -> None:
    revised: dict[str, Any] = {"error_code": violation.code, "details": violation.details}
    revised.update(extra or {})
    with database.transaction() as connection:
        audit.write(connection, RunContext.load(database, run_id), AuditEvent(
            event_type="BLOCKED_ATTEMPT", process_code=process_code, entity_type=entity_type,
            entity_id=entity_id, actor_type="human", actor_user_id=user_id,
            rule_name=violation.rule, decision=attempted, comment=comment,
            explanation=violation.message, approval_status="blocked", revised_values=revised))
