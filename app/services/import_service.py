"""Import the bank statement, ledger cash detail and carry-in items (UC-01, P1).

Reads the four CSV files produced by the dataset generator, records each file with its
SHA-256 hash and control totals, stores the rows as imported, and writes one IMPORT
audit event per file inside the same transaction (CR-08).

This service reads and stores. It judges nothing: validation, normalization and
matching happen in later steps, so a file that imports cleanly can still fail
validation, which is the distinction FR-VAL-07 and CR-17 rest on.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.config import Config
from app.domain.normalize import NormalizationError, normalize_amount_to_cents, normalize_date, normalize_direction
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.repositories import Repositories

BANK_FILE = "bank_statement.csv"
LEDGER_FILE = "gl_cash_detail.csv"
CARRY_IN_FILE = "carry_in_items.csv"
CONTROL_FILE = "run_control.csv"

REQUIRED_COLUMNS = {
    "bank": ["txn_id", "txn_date", "amount", "direction", "description", "reference",
             "bank_type_code", "running_balance"],
    "gl": ["entry_id", "posting_date", "amount", "direction", "description", "reference",
           "gl_account", "source_journal"],
    "carry_in": ["item_id", "item_type", "original_date", "amount", "direction",
                 "description", "reference"],
    "control": ["file_type", "row_count", "total_debits", "total_credits",
                "opening_balance", "closing_balance"],
}

MAX_FILE_BYTES = 16 * 1024 * 1024        # a month of transactions is far smaller


class ImportError_(Exception):
    """The import could not proceed. Nothing has been written."""


@dataclass
class FileImport:
    """What one file contributed."""

    file_type: str
    file_name: str
    sha256: str
    row_count: int
    stored_rows: int
    rejected_rows: list[tuple[str, str]] = field(default_factory=list)
    source_file_id: int | None = None


@dataclass
class ImportResult:
    run_id: int
    files: list[FileImport]
    control_totals: dict[str, dict[str, Any]]

    @property
    def total_rows(self) -> int:
        return sum(f.stored_rows for f in self.files)

    def summary(self) -> str:
        parts = [f"{f.file_type}: {f.stored_rows} rows" for f in self.files]
        return f"Run {self.run_id} imported " + ", ".join(parts)


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def file_digest(path: Path) -> str:
    """SHA-256 of the file exactly as supplied (FR-IMP-04)."""
    if not path.exists():
        raise ImportError_(f"{path} not found")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path, file_type: str) -> list[dict[str, str]]:
    """Read a CSV and check its header. Raises before anything is written."""
    if not path.exists():
        raise ImportError_(f"{path} not found")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ImportError_(f"{path.name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")

    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    expected = REQUIRED_COLUMNS[file_type]
    missing = [column for column in expected if column not in header]
    if missing:
        raise ImportError_(f"{path.name} is missing columns: {', '.join(missing)}")
    return list(reader)


def parse_money(value: str) -> int | None:
    """Dollars to cents, or None when the value cannot be read."""
    try:
        return normalize_amount_to_cents(value)
    except NormalizationError:
        return None


def read_control_file(path: Path) -> dict[str, dict[str, Any]]:
    """Declared row counts, totals and balances per file type (FR-VAL-03)."""
    totals: dict[str, dict[str, Any]] = {}
    for row in read_csv(path, "control"):
        file_type = row["file_type"].strip()
        totals[file_type] = {
            "row_count": int(row["row_count"]),
            "total_debits_cents": parse_money(row["total_debits"]) or 0,
            "total_credits_cents": parse_money(row["total_credits"]) or 0,
            "opening_balance_cents": parse_money(row["opening_balance"]),
            "closing_balance_cents": parse_money(row["closing_balance"]),
        }
    return totals


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

def _shared(run_id: int, source_file_id: int) -> dict[str, Any]:
    return {"run_id": run_id, "source_file_id": source_file_id}


def build_bank_rows(rows: Iterable[dict[str, str]], run_id: int,
                    source_file_id: int) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Shape bank rows for storage. Unreadable rows are returned, not raised.

    A row with an unparseable date, amount or direction is held back and reported by
    validation as an exclusion, so one bad line cannot stop an otherwise good file
    (FR-VAL-02, FR-VAL-07).
    """
    stored: list[dict[str, Any]] = []
    rejected: list[tuple[str, str]] = []
    for row in rows:
        identifier = (row.get("txn_id") or "").strip()
        try:
            amount = normalize_amount_to_cents(row["amount"])
            transaction_date = normalize_date(row["txn_date"])
            direction = normalize_direction(row["direction"])
        except NormalizationError as error:
            rejected.append((identifier or "(no id)", str(error)))
            continue
        if not identifier:
            rejected.append(("(no id)", "txn_id is empty"))
            continue
        stored.append({
            **_shared(run_id, source_file_id),
            "external_txn_id": identifier,
            "transaction_date": transaction_date,
            "amount_cents": amount,
            "direction": direction,
            "description_original": (row.get("description") or "").strip(),
            "reference_original": (row.get("reference") or "").strip() or None,
            "bank_type_code": (row.get("bank_type_code") or "").strip() or None,
            "running_balance_cents": parse_money(row.get("running_balance", "")),
            "status": "imported",
        })
    return stored, rejected


def build_ledger_rows(rows: Iterable[dict[str, str]], run_id: int,
                      source_file_id: int) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    stored: list[dict[str, Any]] = []
    rejected: list[tuple[str, str]] = []
    for row in rows:
        identifier = (row.get("entry_id") or "").strip()
        try:
            amount = normalize_amount_to_cents(row["amount"])
            posting_date = normalize_date(row["posting_date"])
            direction = normalize_direction(row["direction"])
        except NormalizationError as error:
            rejected.append((identifier or "(no id)", str(error)))
            continue
        if not identifier:
            rejected.append(("(no id)", "entry_id is empty"))
            continue
        stored.append({
            **_shared(run_id, source_file_id),
            "external_entry_id": identifier,
            "posting_date": posting_date,
            "amount_cents": amount,
            "direction": direction,
            "description_original": (row.get("description") or "").strip(),
            "reference_original": (row.get("reference") or "").strip() or None,
            "gl_account_code": (row.get("gl_account") or "").strip(),
            "source_journal": (row.get("source_journal") or "").strip() or None,
            "status": "imported",
        })
    return stored, rejected


def build_carry_in_rows(rows: Iterable[dict[str, str]], run_id: int,
                        source_file_id: int) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    stored: list[dict[str, Any]] = []
    rejected: list[tuple[str, str]] = []
    for row in rows:
        identifier = (row.get("item_id") or "").strip()
        item_type = (row.get("item_type") or "").strip()
        try:
            amount = normalize_amount_to_cents(row["amount"])
            original_date = normalize_date(row["original_date"])
            direction = normalize_direction(row["direction"])
        except NormalizationError as error:
            rejected.append((identifier or "(no id)", str(error)))
            continue
        if item_type not in ("outstanding_check", "deposit_in_transit"):
            rejected.append((identifier, f"unknown item_type {item_type!r}"))
            continue
        stored.append({
            **_shared(run_id, source_file_id),
            "external_item_id": identifier,
            "item_type": item_type,
            "original_date": original_date,
            "amount_cents": amount,
            "direction": direction,
            "description_original": (row.get("description") or "").strip(),
            "reference_original": (row.get("reference") or "").strip() or None,
            "status": "imported",
        })
    return stored, rejected


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ImportService:
    def __init__(self, database: Database, repositories: Repositories,
                 audit: AuditLog, config: Config):
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.config = config

    def create_run(self, bank_account_id: int, period_start: str, period_end: str,
                   created_by: int, dataset_seed: int | None = None) -> int:
        """Open a run and freeze the parameters it will use (ADR-10)."""
        with self.database.transaction() as connection:
            run_id = self.repositories.runs.create(
                connection, bank_account_id=bank_account_id, period_start=period_start,
                period_end=period_end, parameter_snapshot=self.config.snapshot(),
                created_by=created_by, created_at=utc_now(), dataset_seed=dataset_seed)
        return run_id

    def import_directory(self, run_id: int, directory: Path, uploaded_by: int) -> ImportResult:
        """Import all four files of a dataset directory as one unit of work.

        Everything commits together: a failure on the third file leaves no trace of the
        first two, so a run is never half-imported.
        """
        directory = Path(directory)
        run = self.repositories.runs.get(run_id)
        if run["status"] not in ("created", "validating", "validation_failed"):
            raise ImportError_(
                f"run {run_id} is {run['status']}; only a new or failed run accepts an import")

        control_path = directory / CONTROL_FILE
        control_totals = read_control_file(control_path) if control_path.exists() else {}

        plan = [
            ("bank", directory / BANK_FILE, build_bank_rows,
             self.repositories.items.add_bank_transactions),
            ("gl", directory / LEDGER_FILE, build_ledger_rows,
             self.repositories.items.add_ledger_entries),
            ("carry_in", directory / CARRY_IN_FILE, build_carry_in_rows,
             self.repositories.items.add_carry_in_items),
        ]

        # Read and hash everything before writing anything, so a bad file is caught first.
        prepared: list[tuple[str, Path, str, list[dict[str, str]]]] = []
        for file_type, path, _, _ in plan:
            digest = file_digest(path)
            if self.repositories.source_files.hash_already_imported(run_id, digest):
                raise ImportError_(
                    f"{path.name} has already been imported into run {run_id} (FR-IMP-05)")
            prepared.append((file_type, path, digest, read_csv(path, file_type)))

        results: list[FileImport] = []
        context = RunContext.load(self.database, run_id)
        with self.database.transaction() as connection:
            for (file_type, path, digest, rows), (_, _, build, store) in zip(prepared, plan):
                declared = control_totals.get(file_type, {})
                source_file_id = self.repositories.source_files.create(
                    connection, run_id=run_id, file_type=file_type, file_name=path.name,
                    sha256=digest, row_count=len(rows), uploaded_by=uploaded_by,
                    uploaded_at=utc_now(),
                    control_total_debits_cents=declared.get("total_debits_cents", 0),
                    control_total_credits_cents=declared.get("total_credits_cents", 0),
                    opening_balance_cents=declared.get("opening_balance_cents"),
                    closing_balance_cents=declared.get("closing_balance_cents"))

                stored_rows, rejected = build(rows, run_id, source_file_id)
                store(connection, stored_rows)

                self.audit.write(connection, context, AuditEvent(
                    event_type="IMPORT", process_code="P1", entity_type="source_file",
                    actor_type="human", actor_user_id=uploaded_by, entity_id=source_file_id,
                    item_refs=[path.name],
                    revised_values={
                        "file_type": file_type, "sha256": digest, "rows_read": len(rows),
                        "rows_stored": len(stored_rows), "rows_unreadable": len(rejected),
                        "declared_row_count": declared.get("row_count"),
                        "declared_debits_cents": declared.get("total_debits_cents"),
                        "declared_credits_cents": declared.get("total_credits_cents"),
                        "opening_balance_cents": declared.get("opening_balance_cents"),
                        "closing_balance_cents": declared.get("closing_balance_cents"),
                    },
                    new_status="imported"))

                results.append(FileImport(file_type=file_type, file_name=path.name,
                                          sha256=digest, row_count=len(rows),
                                          stored_rows=len(stored_rows), rejected_rows=rejected,
                                          source_file_id=source_file_id))

            self.repositories.runs.set_status(connection, run_id, "validating")

        return ImportResult(run_id=run_id, files=results, control_totals=control_totals)
