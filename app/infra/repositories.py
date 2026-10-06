"""All SQL for the system, grouped by aggregate.

Services call these functions and never write SQL themselves, so every statement is
parameterized and every table is touched from one place. Money is always integer
cents. Dates are ISO 8601 strings. Statuses are the values from the state models in
DOC-03.

Connections:
  * Writes take the caller's open connection, so they join the caller's transaction
    and commit or roll back with its audit event (CR-08).
  * Reads that a write depends on take the same connection. `set_status` reads the
    previous status on the caller's connection, and every `get` used by a control
    check accepts `connection=`, so a check and the write it guards see the same
    uncommitted state. Nothing here relies on the database holding a single
    connection.
  * Reads for display take no connection and use the database directly.

Evidence tables (decisions, sign-offs, reopen requests and decisions, audit events)
are only ever inserted. The schema rejects updates to them (CR-09), and these
repositories offer no method that would try.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.infra.db import Database, insert, insert_many, update

ITEM_TABLES = {
    "bank": ("bank_transaction", "bank_transaction_id", "transaction_date"),
    "ledger": ("ledger_entry", "ledger_entry_id", "posting_date"),
    "carry_in": ("carry_in_item", "carry_in_item_id", "original_date"),
}


class RepositoryError(Exception):
    """A lookup failed or an argument was not one the schema allows."""


def _item_table(item_type: str) -> tuple[str, str, str]:
    if item_type not in ITEM_TABLES:
        raise RepositoryError(f"unknown item_type {item_type!r}")
    return ITEM_TABLES[item_type]


def _fetch_all(database: Database, connection: sqlite3.Connection | None, sql: str,
               parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Read on the caller's connection when one is given, otherwise on the database."""
    if connection is None:
        return database.query(sql, parameters)
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise RepositoryError(str(error)) from error


def _fetch_one(database: Database, connection: sqlite3.Connection | None, sql: str,
               parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
    if connection is None:
        return database.query_one(sql, parameters)
    try:
        return connection.execute(sql, parameters).fetchone()
    except sqlite3.Error as error:
        raise RepositoryError(str(error)) from error


def _require(row: sqlite3.Row | None, what: str) -> sqlite3.Row:
    if row is None:
        raise RepositoryError(f"{what} not found")
    return row


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

class UserRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, connection: sqlite3.Connection, full_name: str, role_code: str,
               created_at: str, user_id: int | None = None) -> int:
        values: dict[str, Any] = {"full_name": full_name, "role_code": role_code,
                                  "is_active": 1, "created_at": created_at}
        if user_id is not None:
            values["user_id"] = user_id
        return insert(connection, "app_user", values)

    def get(self, user_id: int, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM app_user WHERE user_id = ?", (user_id,)),
                        f"user {user_id}")

    def active(self) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM app_user WHERE is_active = 1 ORDER BY role_code, full_name")

    def by_role(self, role_code: str) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM app_user WHERE is_active = 1 AND role_code = ? ORDER BY full_name",
            (role_code,))


class AccountRepository:
    def __init__(self, database: Database):
        self.database = database

    def create_bank_account(self, connection: sqlite3.Connection, bank_name: str,
                            account_label: str, account_mask: str, gl_account_code: str,
                            currency_code: str = "USD") -> int:
        return insert(connection, "bank_account", {
            "bank_name": bank_name, "account_label": account_label,
            "account_mask": account_mask, "gl_account_code": gl_account_code,
            "currency_code": currency_code})

    def create_gl_accounts(self, connection: sqlite3.Connection,
                           accounts: Iterable[tuple[str, str, str]]) -> int:
        rows = [{"gl_account_code": code, "account_name": name, "account_type": kind}
                for code, name, kind in accounts]
        return insert_many(connection, "gl_account", rows)

    def find_bank_account(self, account_mask: str) -> sqlite3.Row | None:
        return self.database.query_one(
            "SELECT * FROM bank_account WHERE account_mask = ?", (account_mask,))

    def get_bank_account(self, bank_account_id: int,
                         connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM bank_account WHERE bank_account_id = ?",
                                   (bank_account_id,)),
                        f"bank account {bank_account_id}")

    def gl_accounts(self, connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        """The chart of accounts an adjustment may use, for validation and the W5 picker."""
        return _fetch_all(self.database, connection,
                          "SELECT * FROM gl_account ORDER BY gl_account_code")


# ---------------------------------------------------------------------------
# Run and source files
# ---------------------------------------------------------------------------

class RunRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, connection: sqlite3.Connection, bank_account_id: int, period_start: str,
               period_end: str, parameter_snapshot: str, created_by: int, created_at: str,
               dataset_seed: int | None = None, model_version_id: int | None = None) -> int:
        return insert(connection, "reconciliation_run", {
            "bank_account_id": bank_account_id, "period_start": period_start,
            "period_end": period_end, "status": "created",
            "parameter_snapshot": parameter_snapshot, "dataset_seed": dataset_seed,
            "model_version_id": model_version_id, "created_by": created_by,
            "created_at": created_at})

    def get(self, run_id: int, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM reconciliation_run WHERE run_id = ?", (run_id,)),
                        f"run {run_id}")

    def latest(self) -> sqlite3.Row | None:
        return self.database.query_one(
            "SELECT * FROM reconciliation_run ORDER BY run_id DESC LIMIT 1")

    def set_status(self, connection: sqlite3.Connection, run_id: int, status: str) -> str:
        """Change the run status and return the previous one, read on the same connection."""
        previous = self.get(run_id, connection)["status"]
        update(connection, "reconciliation_run", {"status": status}, "run_id = ?", (run_id,))
        return previous

    def is_closed(self, run_id: int, connection: sqlite3.Connection | None = None) -> bool:
        return self.get(run_id, connection)["status"] == "closed"


class SourceFileRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, connection: sqlite3.Connection, run_id: int, file_type: str,
               file_name: str, sha256: str, row_count: int, uploaded_by: int,
               uploaded_at: str, control_total_debits_cents: int = 0,
               control_total_credits_cents: int = 0, opening_balance_cents: int | None = None,
               closing_balance_cents: int | None = None) -> int:
        return insert(connection, "source_file", {
            "run_id": run_id, "file_type": file_type, "file_name": file_name,
            "sha256": sha256, "row_count": row_count,
            "control_total_debits_cents": control_total_debits_cents,
            "control_total_credits_cents": control_total_credits_cents,
            "opening_balance_cents": opening_balance_cents,
            "closing_balance_cents": closing_balance_cents,
            "uploaded_by": uploaded_by, "uploaded_at": uploaded_at})

    def for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM source_file WHERE run_id = ? ORDER BY source_file_id", (run_id,))

    def by_type(self, run_id: int, file_type: str) -> sqlite3.Row | None:
        return self.database.query_one(
            "SELECT * FROM source_file WHERE run_id = ? AND file_type = ? "
            "ORDER BY source_file_id DESC LIMIT 1", (run_id, file_type))

    def hash_already_imported(self, run_id: int, sha256: str) -> bool:
        return self.database.scalar(
            "SELECT COUNT(*) FROM source_file WHERE run_id = ? AND sha256 = ?",
            (run_id, sha256)) > 0


# ---------------------------------------------------------------------------
# Transactions on both sides
# ---------------------------------------------------------------------------

class ItemRepository:
    """Bank transactions, ledger entries and carry-in items share one interface.

    They are separate tables because their fields differ, but every caller works with
    the same item_type strings used in the state models and the ground truth file.
    """

    def __init__(self, database: Database):
        self.database = database

    def add_bank_transactions(self, connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
        return insert_many(connection, "bank_transaction", rows)

    def add_ledger_entries(self, connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
        return insert_many(connection, "ledger_entry", rows)

    def add_carry_in_items(self, connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
        return insert_many(connection, "carry_in_item", rows)

    def get(self, item_type: str, item_id: int,
            connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        table, key, _ = _item_table(item_type)
        return _require(_fetch_one(self.database, connection,
                                   f"SELECT * FROM {table} WHERE {key} = ?", (item_id,)),
                        f"{item_type} item {item_id}")

    def list(self, run_id: int, item_type: str, statuses: Sequence[str] | None = None) -> list[sqlite3.Row]:
        table, key, date_column = _item_table(item_type)
        sql = f"SELECT * FROM {table} WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            parameters.extend(statuses)
        sql += f" ORDER BY {date_column}, {key}"
        return self.database.query(sql, parameters)

    def set_status(self, connection: sqlite3.Connection, item_type: str, item_id: int,
                   status: str) -> str:
        """Change an item status and return the previous one, read on the same connection."""
        table, key, _ = _item_table(item_type)
        previous = self.get(item_type, item_id, connection)["status"]
        update(connection, table, {"status": status}, f"{key} = ?", (item_id,))
        return previous

    def set_normalized(self, connection: sqlite3.Connection, item_type: str, item_id: int,
                       values: dict[str, Any]) -> None:
        """Write normalized columns. Original columns are never touched (FR-NRM-05)."""
        table, key, _ = _item_table(item_type)
        forbidden = {column for column in values if column.endswith("_original")}
        if forbidden:
            raise RepositoryError(f"original values are immutable: {', '.join(sorted(forbidden))}")
        update(connection, table, values, f"{key} = ?", (item_id,))

    def flag_possible_duplicate(self, connection: sqlite3.Connection, item_type: str,
                                item_id: int) -> None:
        table, key, _ = _item_table(item_type)
        update(connection, table, {"is_possible_duplicate": 1}, f"{key} = ?", (item_id,))

    def exclude(self, connection: sqlite3.Connection, item_type: str, item_id: int,
                reason: str) -> None:
        table, key, _ = _item_table(item_type)
        update(connection, table, {"status": "excluded", "exclusion_reason": reason},
               f"{key} = ?", (item_id,))

    def status_counts(self, run_id: int, item_type: str) -> dict[str, int]:
        table, _, _ = _item_table(item_type)
        rows = self.database.query(
            f"SELECT status, COUNT(*) AS n FROM {table} WHERE run_id = ? GROUP BY status",
            (run_id,))
        return {row["status"]: row["n"] for row in rows}

    def totals_cents(self, run_id: int, item_type: str) -> dict[str, int]:
        """Debit and credit totals for control-total comparison (FR-VAL-03)."""
        table, _, _ = _item_table(item_type)
        rows = self.database.query(
            f"SELECT direction, COALESCE(SUM(amount_cents), 0) AS total FROM {table} "
            f"WHERE run_id = ? AND status <> 'excluded' GROUP BY direction", (run_id,))
        totals = {"debit": 0, "credit": 0}
        for row in rows:
            totals[row["direction"]] = int(row["total"])
        return totals


# ---------------------------------------------------------------------------
# Validation and normalization evidence
# ---------------------------------------------------------------------------

class ValidationRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(self, connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
        return insert_many(connection, "validation_result", rows)

    def for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM validation_result WHERE run_id = ? ORDER BY validation_result_id",
            (run_id,))

    def failures(self, run_id: int, scope: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM validation_result WHERE run_id = ? AND outcome = 'fail'"
        parameters: list[Any] = [run_id]
        if scope:
            sql += " AND scope = ?"
            parameters.append(scope)
        return self.database.query(sql + " ORDER BY validation_result_id", parameters)

    def has_file_level_failure(self, run_id: int) -> bool:
        return self.database.scalar(
            "SELECT COUNT(*) FROM validation_result "
            "WHERE run_id = ? AND scope = 'file' AND outcome = 'fail'", (run_id,)) > 0


class NormalizationRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(self, connection: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
        return insert_many(connection, "normalization_change", rows)

    def for_item(self, run_id: int, item_type: str, item_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM normalization_change WHERE run_id = ? AND item_type = ? AND item_id = ? "
            "ORDER BY normalization_change_id", (run_id, item_type, item_id))

    def count(self, run_id: int) -> int:
        return int(self.database.scalar(
            "SELECT COUNT(*) FROM normalization_change WHERE run_id = ?", (run_id,)) or 0)

    def for_run(self, run_id: int) -> list[sqlite3.Row]:
        """Every transformation in the run, for the Transformation Report (RPT-03)."""
        return self.database.query(
            "SELECT * FROM normalization_change WHERE run_id = ? ORDER BY normalization_change_id",
            (run_id,))


# ---------------------------------------------------------------------------
# Recommendations, candidates and their members
# ---------------------------------------------------------------------------

@dataclass
class CandidateInput:
    """One ranked option and the items it links. Members are (item_type, item_id)."""

    rank_order: int
    score: float
    feature_values: str
    members: list[tuple[str, int]]
    confidence: float | None = None


class RecommendationRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, connection: sqlite3.Connection, run_id: int, subject_item_type: str,
               subject_item_id: int, kind: str, source: str, category_code: str,
               risk_level: str, explanation: str, created_at: str,
               relationship: str = "none", rule_name: str | None = None,
               model_version_id: int | None = None, exception_code: str | None = None,
               risk_rules: str | None = None, confidence: float | None = None,
               genai_prose: str | None = None,
               candidates: list[CandidateInput] | None = None) -> int:
        recommendation_id = insert(connection, "recommendation", {
            "run_id": run_id, "subject_item_type": subject_item_type,
            "subject_item_id": subject_item_id, "kind": kind, "relationship": relationship,
            "source": source, "rule_name": rule_name, "model_version_id": model_version_id,
            "category_code": category_code, "exception_code": exception_code,
            "risk_level": risk_level, "risk_rules": risk_rules, "confidence": confidence,
            "explanation": explanation, "genai_prose": genai_prose,
            "status": "open", "created_at": created_at})

        for candidate in candidates or []:
            candidate_id = insert(connection, "candidate", {
                "recommendation_id": recommendation_id, "rank_order": candidate.rank_order,
                "score": candidate.score, "confidence": candidate.confidence,
                "feature_values": candidate.feature_values})
            for member_type, member_id in candidate.members:
                column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                          "carry_in": "carry_in_item_id"}[member_type]
                insert(connection, "candidate_member",
                       {"candidate_id": candidate_id, column: member_id})
        return recommendation_id

    def add_candidate(self, connection: sqlite3.Connection, recommendation_id: int,
                      candidate: CandidateInput) -> int:
        """Append a candidate to an existing recommendation; existing candidates are untouched.

        Used for a pairing a reviewer selects by hand, which is recorded beside the
        system's own candidates rather than in place of them.
        """
        candidate_id = insert(connection, "candidate", {
            "recommendation_id": recommendation_id, "rank_order": candidate.rank_order,
            "score": candidate.score, "confidence": candidate.confidence,
            "feature_values": candidate.feature_values})
        for member_type, member_id in candidate.members:
            column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                      "carry_in": "carry_in_item_id"}[member_type]
            insert(connection, "candidate_member", {"candidate_id": candidate_id, column: member_id})
        return candidate_id

    def get(self, recommendation_id: int,
            connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM recommendation WHERE recommendation_id = ?",
                                   (recommendation_id,)),
                        f"recommendation {recommendation_id}")

    def candidates(self, recommendation_id: int,
                   connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        return _fetch_all(self.database, connection,
                          "SELECT * FROM candidate WHERE recommendation_id = ? ORDER BY rank_order",
                          (recommendation_id,))

    def candidate_members(self, candidate_id: int,
                          connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        return _fetch_all(self.database, connection,
                          "SELECT * FROM candidate_member WHERE candidate_id = ? "
                          "ORDER BY candidate_member_id", (candidate_id,))

    def for_subject(self, run_id: int, item_type: str, item_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM recommendation WHERE run_id = ? AND subject_item_type = ? "
            "AND subject_item_id = ? ORDER BY recommendation_id", (run_id, item_type, item_id))

    def open_holding(self, run_id: int, item_type: str, item_id: int,
                     connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        """Open recommendations that currently propose this item.

        An item is held by a recommendation when it is the subject, or a member of the
        leading candidate. Alternatives ranked lower are evidence, not a proposal, so
        they do not hold an item. Used when a reviewer selects a different candidate.
        """
        _item_table(item_type)                       # rejects an unknown item type
        column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                  "carry_in": "carry_in_item_id"}[item_type]
        return _fetch_all(self.database, connection,
                          "SELECT r.* FROM recommendation r WHERE r.run_id = ? AND r.status = 'open' "
                          "AND ((r.subject_item_type = ? AND r.subject_item_id = ?) OR EXISTS ("
                          " SELECT 1 FROM candidate c JOIN candidate_member m "
                          " ON m.candidate_id = c.candidate_id "
                          f" WHERE c.recommendation_id = r.recommendation_id AND c.rank_order = 1 "
                          f" AND m.{column} = ?)) ORDER BY r.recommendation_id",
                          (run_id, item_type, item_id, item_id))

    def queue(self, run_id: int, category_code: str, statuses: Sequence[str] = ("open",)) -> list[sqlite3.Row]:
        """Queue order: highest risk first, then lowest confidence (FR-REV-02)."""
        placeholders = ", ".join("?" for _ in statuses)
        return self.database.query(
            "SELECT * FROM recommendation WHERE run_id = ? AND category_code = ? "
            f"AND status IN ({placeholders}) "
            "ORDER BY CASE risk_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
            "COALESCE(confidence, 1.0) ASC, recommendation_id",
            (run_id, category_code, *statuses))

    def queue_counts(self, run_id: int) -> dict[str, int]:
        rows = self.database.query(
            "SELECT category_code, COUNT(*) AS n FROM recommendation "
            "WHERE run_id = ? AND status = 'open' GROUP BY category_code", (run_id,))
        return {row["category_code"]: row["n"] for row in rows}

    def exception_counts(self, run_id: int) -> dict[str, int]:
        rows = self.database.query(
            "SELECT exception_code, COUNT(*) AS n FROM recommendation "
            "WHERE run_id = ? AND exception_code IS NOT NULL GROUP BY exception_code", (run_id,))
        return {row["exception_code"]: row["n"] for row in rows}

    def set_prose(self, connection: sqlite3.Connection, recommendation_id: int, prose: str) -> None:
        """Store labelled explanation prose. Only this column changes; nothing that scores,
        ranks, routes or decides is touched (CR-13)."""
        update(connection, "recommendation", {"genai_prose": prose},
               "recommendation_id = ?", (recommendation_id,))

    def set_status(self, connection: sqlite3.Connection, recommendation_id: int, status: str) -> str:
        """Change the recommendation status and return the previous one, read on the same connection."""
        previous = self.get(recommendation_id, connection)["status"]
        update(connection, "recommendation", {"status": status},
               "recommendation_id = ?", (recommendation_id,))
        return previous

    def count(self, run_id: int) -> int:
        return int(self.database.scalar(
            "SELECT COUNT(*) FROM recommendation WHERE run_id = ?", (run_id,)) or 0)

    def for_run(self, run_id: int) -> list[sqlite3.Row]:
        """Every recommendation in the run, whatever its status, oldest first."""
        return self.database.query(
            "SELECT * FROM recommendation WHERE run_id = ? ORDER BY recommendation_id", (run_id,))

    def model_version(self, model_version_id: int) -> sqlite3.Row | None:
        return self.database.query_one(
            "SELECT * FROM model_version WHERE model_version_id = ?", (model_version_id,))


# ---------------------------------------------------------------------------
# Review decisions and batches (FR-REV, CR-01..03, CR-11, DD-02)
# ---------------------------------------------------------------------------

class DecisionRepository:
    """Reviewer decisions. Append-only: a changed mind is a new decision (CR-09).

    A batch approval writes one `review_batch` row and one decision per item in it,
    each carrying the batch identifier (DD-02, pending confirmation). The batch row
    records the reviewer's single action; the decisions remain the per-item evidence.
    """

    def __init__(self, database: Database):
        self.database = database

    def create_batch(self, connection: sqlite3.Connection, run_id: int, created_by: int,
                     created_at: str, item_count: int) -> int:
        return insert(connection, "review_batch", {
            "run_id": run_id, "created_by": created_by, "created_at": created_at,
            "item_count": item_count})

    def get_batch(self, review_batch_id: int,
                  connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM review_batch WHERE review_batch_id = ?",
                                   (review_batch_id,)),
                        f"review batch {review_batch_id}")

    def add(self, connection: sqlite3.Connection, *, run_id: int, recommendation_id: int,
            decision: str, decision_level: str, decided_by: int, decided_at: str,
            previous_status: str, new_status: str, comment: str | None = None,
            chosen_candidate_id: int | None = None, review_batch_id: int | None = None,
            opened_at: str | None = None) -> int:
        return insert(connection, "review_decision", {
            "run_id": run_id, "recommendation_id": recommendation_id,
            "chosen_candidate_id": chosen_candidate_id, "review_batch_id": review_batch_id,
            "decision": decision, "decision_level": decision_level, "comment": comment,
            "decided_by": decided_by, "opened_at": opened_at, "decided_at": decided_at,
            "previous_status": previous_status, "new_status": new_status})

    def for_recommendation(self, recommendation_id: int,
                           connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        """Every decision on one recommendation, oldest first: the item's review history."""
        return _fetch_all(self.database, connection,
                          "SELECT * FROM review_decision WHERE recommendation_id = ? "
                          "ORDER BY review_decision_id", (recommendation_id,))

    def latest(self, recommendation_id: int,
               connection: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        return _fetch_one(self.database, connection,
                          "SELECT * FROM review_decision WHERE recommendation_id = ? "
                          "ORDER BY review_decision_id DESC LIMIT 1", (recommendation_id,))

    def escalated_by(self, recommendation_id: int,
                     connection: sqlite3.Connection | None = None) -> int | None:
        """The user who most recently escalated this recommendation (input to CR-03)."""
        row = _fetch_one(self.database, connection,
                         "SELECT decided_by FROM review_decision WHERE recommendation_id = ? "
                         "AND decision = 'escalate' ORDER BY review_decision_id DESC LIMIT 1",
                         (recommendation_id,))
        return None if row is None else int(row["decided_by"])

    def for_batch(self, review_batch_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM review_decision WHERE review_batch_id = ? ORDER BY review_decision_id",
            (review_batch_id,))

    def for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM review_decision WHERE run_id = ? ORDER BY review_decision_id",
            (run_id,))

    def deciders(self, run_id: int, decision_level: str | None = None,
                 connection: sqlite3.Connection | None = None) -> set[int]:
        """Distinct users who recorded decisions in a run (input to CR-05)."""
        sql = "SELECT DISTINCT decided_by FROM review_decision WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if decision_level:
            sql += " AND decision_level = ?"
            parameters.append(decision_level)
        return {int(row["decided_by"]) for row in _fetch_all(self.database, connection, sql, parameters)}

    def counts(self, run_id: int) -> dict[str, int]:
        rows = self.database.query(
            "SELECT decision, COUNT(*) AS n FROM review_decision WHERE run_id = ? GROUP BY decision",
            (run_id,))
        return {row["decision"]: row["n"] for row in rows}


# ---------------------------------------------------------------------------
# Proposed adjustments (FR-ADJ, CR-04). Never posted anywhere (CR-12, NOP-01).
# ---------------------------------------------------------------------------

class AdjustmentRepository:
    """Adjustments are proposals with an approval trail, not journal entries.

    Rows are never deleted: a rejected adjustment keeps status 'rejected' and its
    decision record. Status is the only mutable column; the decision that changed it
    is kept in the append-only `adjustment_decision` table.
    """

    def __init__(self, database: Database):
        self.database = database

    def create(self, connection: sqlite3.Connection, *, run_id: int, recommendation_id: int,
               amount_cents: int, debit_account_code: str, credit_account_code: str,
               rationale: str, prepared_by: int, prepared_at: str,
               evidence_refs: str | None = None) -> int:
        return insert(connection, "adjustment", {
            "run_id": run_id, "recommendation_id": recommendation_id,
            "amount_cents": amount_cents, "debit_account_code": debit_account_code,
            "credit_account_code": credit_account_code, "rationale": rationale,
            "evidence_refs": evidence_refs, "prepared_by": prepared_by,
            "prepared_at": prepared_at, "status": "proposed"})

    def get(self, adjustment_id: int,
            connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM adjustment WHERE adjustment_id = ?",
                                   (adjustment_id,)),
                        f"adjustment {adjustment_id}")

    def set_status(self, connection: sqlite3.Connection, adjustment_id: int, status: str) -> str:
        previous = self.get(adjustment_id, connection)["status"]
        update(connection, "adjustment", {"status": status},
               "adjustment_id = ?", (adjustment_id,))
        return previous

    def add_decision(self, connection: sqlite3.Connection, *, adjustment_id: int,
                     decision: str, decided_by: int, decided_at: str,
                     comment: str | None = None) -> int:
        return insert(connection, "adjustment_decision", {
            "adjustment_id": adjustment_id, "decision": decision, "comment": comment,
            "decided_by": decided_by, "decided_at": decided_at})

    def decisions(self, adjustment_id: int,
                  connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        return _fetch_all(self.database, connection,
                          "SELECT * FROM adjustment_decision WHERE adjustment_id = ? "
                          "ORDER BY adjustment_decision_id", (adjustment_id,))

    def for_recommendation(self, recommendation_id: int,
                           connection: sqlite3.Connection | None = None) -> list[sqlite3.Row]:
        return _fetch_all(self.database, connection,
                          "SELECT * FROM adjustment WHERE recommendation_id = ? "
                          "ORDER BY adjustment_id", (recommendation_id,))

    def for_run(self, run_id: int, statuses: Sequence[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM adjustment WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            parameters.extend(statuses)
        return self.database.query(sql + " ORDER BY adjustment_id", parameters)

    def totals_cents(self, run_id: int) -> dict[str, int]:
        """Proposed, approved and rejected totals, for the Adjustment Report (RPT-07)."""
        rows = self.database.query(
            "SELECT status, COALESCE(SUM(amount_cents), 0) AS total FROM adjustment "
            "WHERE run_id = ? GROUP BY status", (run_id,))
        totals = {"proposed": 0, "approved": 0, "rejected": 0}
        for row in rows:
            totals[row["status"]] = int(row["total"])
        return totals


# ---------------------------------------------------------------------------
# Period sign-off and reopening (FR-PER, CR-05, CR-06, CR-10)
# ---------------------------------------------------------------------------

class PeriodRepository:
    """Sign-offs, reopen requests and reopen decisions. All three are append-only.

    A re-close after reopening adds a second sign-off; the first is kept. A reopen
    request is pending until a reopen decision exists for it.
    """

    def __init__(self, database: Database):
        self.database = database

    def add_signoff(self, connection: sqlite3.Connection, *, run_id: int, signed_by: int,
                    signed_at: str, bank_ending_balance_cents: int,
                    book_ending_balance_cents: int, chain_head_hash: str,
                    deposits_in_transit_cents: int = 0, outstanding_checks_cents: int = 0,
                    bank_originated_cents: int = 0, unresolved_difference_cents: int = 0,
                    unresolved_item_count: int = 0, comment: str | None = None) -> int:
        return insert(connection, "period_signoff", {
            "run_id": run_id, "signed_by": signed_by, "signed_at": signed_at,
            "bank_ending_balance_cents": bank_ending_balance_cents,
            "book_ending_balance_cents": book_ending_balance_cents,
            "deposits_in_transit_cents": deposits_in_transit_cents,
            "outstanding_checks_cents": outstanding_checks_cents,
            "bank_originated_cents": bank_originated_cents,
            "unresolved_difference_cents": unresolved_difference_cents,
            "unresolved_item_count": unresolved_item_count, "comment": comment,
            "chain_head_hash": chain_head_hash})

    def signoffs(self, run_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM period_signoff WHERE run_id = ? ORDER BY period_signoff_id", (run_id,))

    def latest_signoff(self, run_id: int,
                       connection: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        return _fetch_one(self.database, connection,
                          "SELECT * FROM period_signoff WHERE run_id = ? "
                          "ORDER BY period_signoff_id DESC LIMIT 1", (run_id,))

    def add_reopen_request(self, connection: sqlite3.Connection, *, run_id: int,
                           requested_by: int, reason: str, requested_at: str,
                           original_status: str) -> int:
        return insert(connection, "reopen_request", {
            "run_id": run_id, "requested_by": requested_by, "reason": reason,
            "requested_at": requested_at, "original_status": original_status})

    def get_reopen_request(self, reopen_request_id: int,
                           connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        return _require(_fetch_one(self.database, connection,
                                   "SELECT * FROM reopen_request WHERE reopen_request_id = ?",
                                   (reopen_request_id,)),
                        f"reopen request {reopen_request_id}")

    def pending_reopen_request(self, run_id: int,
                               connection: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        """The open request for a run, if any: one with no decision recorded."""
        return _fetch_one(self.database, connection,
                          "SELECT r.* FROM reopen_request r WHERE r.run_id = ? AND NOT EXISTS "
                          "(SELECT 1 FROM reopen_decision d "
                          " WHERE d.reopen_request_id = r.reopen_request_id) "
                          "ORDER BY r.reopen_request_id DESC LIMIT 1", (run_id,))

    def add_reopen_decision(self, connection: sqlite3.Connection, *, reopen_request_id: int,
                            decision: str, decided_by: int, decided_at: str,
                            revised_status: str, comment: str | None = None) -> int:
        return insert(connection, "reopen_decision", {
            "reopen_request_id": reopen_request_id, "decision": decision,
            "comment": comment, "decided_by": decided_by, "decided_at": decided_at,
            "revised_status": revised_status})

    def reopen_decision(self, reopen_request_id: int,
                        connection: sqlite3.Connection | None = None) -> sqlite3.Row | None:
        return _fetch_one(self.database, connection,
                          "SELECT * FROM reopen_decision WHERE reopen_request_id = ? "
                          "ORDER BY reopen_decision_id LIMIT 1", (reopen_request_id,))

    def reopen_history(self, run_id: int) -> list[sqlite3.Row]:
        """Every request with its decision, if any, for the Change and Override Report (RPT-10)."""
        return self.database.query(
            "SELECT r.reopen_request_id, r.requested_by, r.reason, r.requested_at, "
            "r.original_status, d.reopen_decision_id, d.decision, d.comment, d.decided_by, "
            "d.decided_at, d.revised_status "
            "FROM reopen_request r LEFT JOIN reopen_decision d "
            "ON d.reopen_request_id = r.reopen_request_id "
            "WHERE r.run_id = ? ORDER BY r.reopen_request_id", (run_id,))


# ---------------------------------------------------------------------------
# Generated reports (FR-RPT, ADR-14)
# ---------------------------------------------------------------------------

class ReportRepository:
    """One row per generated report file, with its content hash.

    Regenerating a report adds a row rather than replacing one. The newest row per
    report code and format is the current version; older rows are kept as history.
    """

    def __init__(self, database: Database):
        self.database = database

    def add(self, connection: sqlite3.Connection, *, run_id: int, report_code: str,
            output_format: str, file_path: str, content_sha256: str, generated_at: str) -> int:
        return insert(connection, "report", {
            "run_id": run_id, "report_code": report_code, "output_format": output_format,
            "file_path": file_path, "content_sha256": content_sha256,
            "generated_at": generated_at})

    def get(self, report_id: int) -> sqlite3.Row:
        return _require(self.database.query_one(
            "SELECT * FROM report WHERE report_id = ?", (report_id,)), f"report {report_id}")

    def current(self, run_id: int) -> list[sqlite3.Row]:
        """The newest version of each report code and format, ordered by code."""
        return self.database.query(
            "SELECT r.* FROM report r WHERE r.run_id = ? AND r.report_id = "
            "(SELECT MAX(x.report_id) FROM report x WHERE x.run_id = r.run_id "
            " AND x.report_code = r.report_code AND x.output_format = r.output_format) "
            "ORDER BY r.report_code, r.output_format", (run_id,))

    def history(self, run_id: int, report_code: str) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM report WHERE run_id = ? AND report_code = ? ORDER BY report_id",
            (run_id, report_code))


# ---------------------------------------------------------------------------
# Convenience bundle
# ---------------------------------------------------------------------------

@dataclass
class Repositories:
    """Every repository for one database, created once and passed to services."""

    users: UserRepository
    accounts: AccountRepository
    runs: RunRepository
    source_files: SourceFileRepository
    items: ItemRepository
    validation: ValidationRepository
    normalization: NormalizationRepository
    recommendations: RecommendationRepository
    decisions: DecisionRepository
    adjustments: AdjustmentRepository
    periods: PeriodRepository
    reports: ReportRepository

    @staticmethod
    def for_database(database: Database) -> "Repositories":
        return Repositories(
            users=UserRepository(database),
            accounts=AccountRepository(database),
            runs=RunRepository(database),
            source_files=SourceFileRepository(database),
            items=ItemRepository(database),
            validation=ValidationRepository(database),
            normalization=NormalizationRepository(database),
            recommendations=RecommendationRepository(database),
            decisions=DecisionRepository(database),
            adjustments=AdjustmentRepository(database),
            periods=PeriodRepository(database),
            reports=ReportRepository(database),
        )
