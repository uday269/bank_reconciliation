"""Audit event writing and hash chain verification.

Every system and human action writes one event (FR-AUD-01..07). Events are written
inside the caller's transaction, so an action and its evidence commit together or not
at all (CR-08). Rows are append-only: corrections are new events (CR-09), enforced by
database triggers as well as by this module never issuing an UPDATE.

Chain
    event_hash = SHA-256(previous_hash + "|" + canonical_json(event))
    previous_hash of the first event in a run is the literal "GENESIS"

Canonical JSON means sorted keys, no insignificant whitespace, UTF-8, integers for
money and ISO 8601 strings for dates, so the same event always hashes to the same
value on any machine (ADR-08).

Stated limit (DD-06): triggers stop changes made through this application. Anyone
holding the database file can rebuild it, chain included. What makes that detectable
is the chain-head hash recorded in the signed Reconciliation Summary and in the
exported report package, which live outside the database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from app.infra.db import Database, insert

GENESIS = "GENESIS"

# Event types written by the system. Listed here so a typo becomes an error
# rather than a new category nobody notices in the reports.
EVENT_TYPES = frozenset({
    "IMPORT", "VALIDATION", "NORMALIZATION", "RULE", "AI_RECOMMENDATION", "ROUTING",
    "DETAIL_OPENED", "DECISION", "BLOCKED_ATTEMPT", "ADJUSTMENT_PROPOSED",
    "ADJUSTMENT_DECIDED", "REPORT_GENERATED", "REPORT_EXPORTED", "REPORT_VERIFIED",
    "CHAIN_VERIFIED", "SIGNOFF", "PERIOD_LOCKED", "REOPEN_REQUESTED", "REOPEN_DECIDED",
    "GENAI_PROSE",
    "IDENTITY_SELECTED", "STAGE_TIMING",
})

PROCESS_CODES = frozenset({"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"})

# Columns that take part in the hash. Deliberately everything except the row
# identifier, which SQLite assigns, and the hash columns themselves.
HASHED_COLUMNS = (
    "run_id", "sequence_no", "event_type", "process_code", "bank_account_id",
    "period_start", "period_end", "actor_type", "actor_user_id", "entity_type",
    "entity_id", "item_refs", "original_values", "revised_values", "rule_name",
    "model_version_id", "candidate_scores", "confidence", "risk_level", "explanation",
    "decision", "comment", "approval_status", "evidence_refs", "previous_status",
    "new_status", "created_at",
)


class AuditError(Exception):
    """An audit event could not be written or is malformed."""


def utc_now() -> str:
    """Current time as an ISO 8601 UTC string, seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(payload: dict[str, Any]) -> str:
    """Byte-stable JSON: sorted keys, no spaces, no NaN, UTF-8 preserved."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def compute_hash(previous_hash: str, row: dict[str, Any]) -> str:
    """Hash one event row against the previous hash."""
    payload = {column: row.get(column) for column in HASHED_COLUMNS}
    material = f"{previous_hash}|{canonical_json(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class AuditEvent:
    """One action, ready to be written.

    Only `event_type`, `process_code`, `entity_type` and the actor are always required.
    Everything else is filled in by whichever service has something to record.
    """

    event_type: str
    process_code: str
    entity_type: str
    actor_type: str = "system"                 # "system" or "human"
    actor_user_id: int | None = None
    entity_id: int | None = None
    item_refs: list[str] | None = None
    original_values: dict[str, Any] | None = None
    revised_values: dict[str, Any] | None = None
    rule_name: str | None = None
    model_version_id: int | None = None
    candidate_scores: list[dict[str, Any]] | None = None
    confidence: float | None = None
    risk_level: str | None = None
    explanation: str | None = None
    decision: str | None = None
    comment: str | None = None
    approval_status: str | None = None
    evidence_refs: list[str] | None = None
    previous_status: str | None = None
    new_status: str | None = None
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise AuditError(f"unknown event_type {self.event_type!r}")
        if self.process_code not in PROCESS_CODES:
            raise AuditError(f"unknown process_code {self.process_code!r}")
        if self.actor_type not in ("system", "human"):
            raise AuditError("actor_type must be 'system' or 'human'")
        if self.actor_type == "human" and self.actor_user_id is None:
            raise AuditError("a human event must name the acting user (FR-AUD-02)")
        if self.actor_type == "system" and self.actor_user_id is not None:
            raise AuditError("a system event must not name a user")
        if self.risk_level is not None and self.risk_level not in ("low", "medium", "high"):
            raise AuditError(f"unknown risk_level {self.risk_level!r}")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise AuditError("confidence must be between 0 and 1")


class AuditLog:
    """Writes and verifies the chain for one database."""

    def __init__(self, database: Database):
        self.database = database

    # -- writing ------------------------------------------------------------
    def write(self, connection: sqlite3.Connection, run: "RunContext", event: AuditEvent) -> dict[str, Any]:
        """Append one event inside the caller's open transaction.

        Returns the stored row. Raises AuditError if the event is malformed, which
        rolls back the caller's action along with it (CR-08).
        """
        event.validate()

        sequence_no = self._next_sequence(connection, run.run_id)
        previous_hash = self._head_hash(connection, run.run_id)

        row: dict[str, Any] = {
            "run_id": run.run_id,
            "sequence_no": sequence_no,
            "event_type": event.event_type,
            "process_code": event.process_code,
            "bank_account_id": run.bank_account_id,
            "period_start": run.period_start,
            "period_end": run.period_end,
            "actor_type": event.actor_type,
            "actor_user_id": event.actor_user_id,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "item_refs": _json_or_none(event.item_refs),
            "original_values": _json_or_none(event.original_values),
            "revised_values": _json_or_none(event.revised_values),
            "rule_name": event.rule_name,
            "model_version_id": event.model_version_id,
            "candidate_scores": _json_or_none(event.candidate_scores),
            "confidence": event.confidence,
            "risk_level": event.risk_level,
            "explanation": event.explanation,
            "decision": event.decision,
            "comment": event.comment,
            "approval_status": event.approval_status,
            "evidence_refs": _json_or_none(event.evidence_refs),
            "previous_status": event.previous_status,
            "new_status": event.new_status,
            "created_at": event.created_at,
        }
        row["previous_hash"] = previous_hash
        row["event_hash"] = compute_hash(previous_hash, row)

        insert(connection, "audit_event", row)
        return row

    def write_many(self, connection: sqlite3.Connection, run: "RunContext",
                   events: Iterable[AuditEvent]) -> int:
        """Append several events in order. Used by batch actions, one event per item."""
        count = 0
        for event in events:
            self.write(connection, run, event)
            count += 1
        return count

    def _next_sequence(self, connection: sqlite3.Connection, run_id: int) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next FROM audit_event WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["next"])

    def _head_hash(self, connection: sqlite3.Connection, run_id: int) -> str:
        row = connection.execute(
            "SELECT event_hash FROM audit_event WHERE run_id = ? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return GENESIS if row is None else row["event_hash"]

    # -- reading ------------------------------------------------------------
    def head_hash(self, run_id: int) -> str:
        """Chain head, printed on the signed Reconciliation Summary (FR-AUD-07)."""
        value = self.database.scalar(
            "SELECT event_hash FROM audit_event WHERE run_id = ? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (run_id,),
        )
        return value or GENESIS

    def count(self, run_id: int) -> int:
        return int(self.database.scalar(
            "SELECT COUNT(*) FROM audit_event WHERE run_id = ?", (run_id,)) or 0)

    def events(self, run_id: int, limit: int | None = None, offset: int = 0) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM audit_event WHERE run_id = ? ORDER BY sequence_no"
               + (" LIMIT ? OFFSET ?" if limit is not None else ""))
        parameters = (run_id, limit, offset) if limit is not None else (run_id,)
        return self.database.query(sql, parameters)

    # -- verification -------------------------------------------------------
    def verify(self, run_id: int) -> "ChainVerification":
        """Recompute the chain and report the first break, if any (FR-AUD-05)."""
        rows = self.database.query(
            "SELECT * FROM audit_event WHERE run_id = ? ORDER BY sequence_no", (run_id,))

        expected_previous = GENESIS
        expected_sequence = 1
        for row in rows:
            data = dict(row)
            if data["sequence_no"] != expected_sequence:
                return ChainVerification(
                    run_id, False, expected_sequence - 1, self.head_hash(run_id),
                    data["sequence_no"],
                    f"sequence gap: expected {expected_sequence}, found {data['sequence_no']}")
            if data["previous_hash"] != expected_previous:
                return ChainVerification(
                    run_id, False, expected_sequence - 1, self.head_hash(run_id),
                    data["sequence_no"], "previous_hash does not match the preceding event")
            recomputed = compute_hash(data["previous_hash"], data)
            if recomputed != data["event_hash"]:
                return ChainVerification(
                    run_id, False, expected_sequence - 1, self.head_hash(run_id),
                    data["sequence_no"], "event content does not match its stored hash")
            expected_previous = data["event_hash"]
            expected_sequence += 1

        return ChainVerification(run_id, True, len(rows), self.head_hash(run_id), None, None)


@dataclass(frozen=True)
class RunContext:
    """The run identity every event carries (FR-AUD-02)."""

    run_id: int
    bank_account_id: int
    period_start: str
    period_end: str

    @staticmethod
    def load(database: Database, run_id: int) -> "RunContext":
        row = database.query_one(
            "SELECT run_id, bank_account_id, period_start, period_end "
            "FROM reconciliation_run WHERE run_id = ?", (run_id,))
        if row is None:
            raise AuditError(f"run {run_id} not found")
        return RunContext(row["run_id"], row["bank_account_id"],
                          row["period_start"], row["period_end"])


@dataclass(frozen=True)
class ChainVerification:
    run_id: int
    intact: bool
    events_checked: int
    head_hash: str
    first_broken_sequence: int | None
    reason: str | None

    def summary(self) -> str:
        if self.intact:
            return f"Chain intact: {self.events_checked} events, head {self.head_hash[:12]}…"
        return (f"Chain broken at event {self.first_broken_sequence}: {self.reason}. "
                f"{self.events_checked} events verified before the break.")


def _json_or_none(value: Any) -> str | None:
    return None if value is None else canonical_json(value) if isinstance(value, dict) \
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
