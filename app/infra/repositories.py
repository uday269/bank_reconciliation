"""All SQL for the system, grouped by aggregate.

Services call these functions and never write SQL themselves, so every statement is
parameterized and every table is touched from one place. Writes take the caller's
open connection, so they join the caller's transaction (CR-08). Reads use the
database directly because they need no transaction.

Money is always integer cents. Dates are ISO 8601 strings. Statuses are the values
from the state models in DOC-03.
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

    def get(self, user_id: int) -> sqlite3.Row:
        row = self.database.query_one("SELECT * FROM app_user WHERE user_id = ?", (user_id,))
        if row is None:
            raise RepositoryError(f"user {user_id} not found")
        return row

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

    def get(self, run_id: int) -> sqlite3.Row:
        row = self.database.query_one(
            "SELECT * FROM reconciliation_run WHERE run_id = ?", (run_id,))
        if row is None:
            raise RepositoryError(f"run {run_id} not found")
        return row

    def latest(self) -> sqlite3.Row | None:
        return self.database.query_one(
            "SELECT * FROM reconciliation_run ORDER BY run_id DESC LIMIT 1")

    def set_status(self, connection: sqlite3.Connection, run_id: int, status: str) -> str:
        previous = self.get(run_id)["status"]
        update(connection, "reconciliation_run", {"status": status}, "run_id = ?", (run_id,))
        return previous

    def is_closed(self, run_id: int) -> bool:
        return self.get(run_id)["status"] == "closed"


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

    def get(self, item_type: str, item_id: int) -> sqlite3.Row:
        table, key, _ = _item_table(item_type)
        row = self.database.query_one(f"SELECT * FROM {table} WHERE {key} = ?", (item_id,))
        if row is None:
            raise RepositoryError(f"{item_type} item {item_id} not found")
        return row

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
        table, key, _ = _item_table(item_type)
        previous = self.get(item_type, item_id)["status"]
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

    def get(self, recommendation_id: int) -> sqlite3.Row:
        row = self.database.query_one(
            "SELECT * FROM recommendation WHERE recommendation_id = ?", (recommendation_id,))
        if row is None:
            raise RepositoryError(f"recommendation {recommendation_id} not found")
        return row

    def candidates(self, recommendation_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM candidate WHERE recommendation_id = ? ORDER BY rank_order",
            (recommendation_id,))

    def candidate_members(self, candidate_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM candidate_member WHERE candidate_id = ? ORDER BY candidate_member_id",
            (candidate_id,))

    def for_subject(self, run_id: int, item_type: str, item_id: int) -> list[sqlite3.Row]:
        return self.database.query(
            "SELECT * FROM recommendation WHERE run_id = ? AND subject_item_type = ? "
            "AND subject_item_id = ? ORDER BY recommendation_id", (run_id, item_type, item_id))

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

    def set_status(self, connection: sqlite3.Connection, recommendation_id: int, status: str) -> str:
        previous = self.get(recommendation_id)["status"]
        update(connection, "recommendation", {"status": status},
               "recommendation_id = ?", (recommendation_id,))
        return previous

    def count(self, run_id: int) -> int:
        return int(self.database.scalar(
            "SELECT COUNT(*) FROM recommendation WHERE run_id = ?", (run_id,)) or 0)


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
        )
