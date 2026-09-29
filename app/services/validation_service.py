"""Validate and normalize imported data (UC-02, P2).

Validation answers one question per test: is this data fit to be matched? Tests are
either file-level or row-level, and the difference decides what happens next:

  * a file-level failure blocks matching for the whole run (CR-17, FR-VAL-07)
  * a row-level failure excludes that row and reports it; the run continues
  * a warning changes nothing and is shown to the reviewer

Rows that pass are normalized in the same transaction, keeping every original value
beside its normalized form (FR-NRM-05). Validation results, normalization changes and
the audit events all commit together (CR-08).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import Config
from app.domain.normalize import (
    FieldChange, business_days_between, normalize_description, normalize_item,
)
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.repositories import Repositories

# Test catalogue. The code is stable across runs and appears in RPT-02.
TESTS = {
    "VAL-01": ("Required columns present", "file"),
    "VAL-02": ("Field types and required values", "row"),
    "VAL-03": ("Control totals agree", "file"),
    "VAL-04": ("Duplicate identifiers", "row"),
    "VAL-05": ("Possible duplicate transactions", "row"),
    "VAL-06": ("Transactions within the period", "row"),
    "VAL-07": ("Running balance continuity", "file"),
    "VAL-08": ("Ledger account in scope", "row"),
}


@dataclass
class ValidationSummary:
    run_id: int
    passed: bool
    tests_run: int
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    excluded_rows: int = 0
    possible_duplicates: int = 0
    normalized_items: int = 0
    normalization_changes: int = 0

    def summary(self) -> str:
        verdict = "passed" if self.passed else "FAILED"
        return (f"Validation {verdict}: {self.tests_run} tests, {len(self.failures)} file-level "
                f"failures, {self.excluded_rows} rows excluded, "
                f"{self.possible_duplicates} possible duplicates flagged, "
                f"{self.normalized_items} items normalized")


class ValidationService:
    def __init__(self, database: Database, repositories: Repositories,
                 audit: AuditLog, config: Config):
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.config = config

    # -- entry point --------------------------------------------------------
    def validate_and_normalize(self, run_id: int, performed_by: int | None = None) -> ValidationSummary:
        run = self.repositories.runs.get(run_id)
        if run["status"] not in ("validating", "validation_failed"):
            raise ValueError(f"run {run_id} is {run['status']}; validation runs after import")

        period_start = run["period_start"]
        period_end = run["period_end"]
        context = RunContext.load(self.database, run_id)

        results: list[dict[str, Any]] = []
        summary = ValidationSummary(run_id=run_id, passed=True, tests_run=0)

        # Collected first, applied inside the transaction below.
        exclusions: list[tuple[str, int, str]] = []
        duplicate_flags: list[tuple[str, int]] = []

        for item_type, file_type in (("bank", "bank"), ("ledger", "gl"), ("carry_in", "carry_in")):
            source_file = self.repositories.source_files.by_type(run_id, file_type)
            if source_file is None:
                results.append(self._result(run_id, None, "VAL-01", "file", None, "fail",
                                            f"no {file_type} file imported"))
                summary.failures.append(f"VAL-01 {file_type}: file missing")
                summary.passed = False
                continue

            results.append(self._result(run_id, source_file["source_file_id"], "VAL-01",
                                        "file", None, "pass", "header accepted at import"))

            items = self.repositories.items.list(run_id, item_type)

            # VAL-02 is enforced at import: unreadable rows never reach the database.
            declared_rows = source_file["row_count"]
            unreadable = declared_rows - len(items)
            if unreadable > 0:
                results.append(self._result(
                    run_id, source_file["source_file_id"], "VAL-02", "row", None, "fail",
                    f"{unreadable} row(s) could not be read and were not stored"))
                summary.excluded_rows += unreadable

            # VAL-03 control totals
            results.extend(self._check_control_totals(run_id, item_type, source_file, summary))

            # VAL-04 duplicate identifiers within the file
            results.extend(self._check_duplicate_identifiers(run_id, item_type, items,
                                                             source_file, summary, exclusions))

            # VAL-05 possible duplicate transactions (BR-07): flagged, never excluded
            if item_type in ("bank", "ledger"):
                results.extend(self._check_possible_duplicates(
                    run_id, item_type, items, source_file, summary, duplicate_flags))

            # VAL-06 period membership
            if item_type in ("bank", "ledger"):
                results.extend(self._check_period(run_id, item_type, items, source_file,
                                                  period_start, period_end, summary))

            # VAL-08 ledger account in scope
            if item_type == "ledger":
                results.extend(self._check_gl_account(run_id, items, source_file, summary))

        # VAL-07 running balance continuity on the bank file
        results.extend(self._check_running_balance(run_id, summary))

        summary.tests_run = len(results)

        with self.database.transaction() as connection:
            self.repositories.validation.add(connection, results)

            for item_type, item_id, reason in exclusions:
                self.repositories.items.exclude(connection, item_type, item_id, reason)
            for item_type, item_id in duplicate_flags:
                self.repositories.items.flag_possible_duplicate(connection, item_type, item_id)

            self.audit.write(connection, context, AuditEvent(
                event_type="VALIDATION", process_code="P2", entity_type="reconciliation_run",
                entity_id=run_id,
                actor_type="human" if performed_by else "system", actor_user_id=performed_by,
                revised_values={
                    "tests_run": summary.tests_run,
                    "file_level_failures": len(summary.failures),
                    "warnings": len(summary.warnings),
                    "rows_excluded": summary.excluded_rows,
                    "possible_duplicates": summary.possible_duplicates,
                    "passed": summary.passed,
                }))

            if not summary.passed:
                self.repositories.runs.set_status(connection, run_id, "validation_failed")
                return summary

            # Normalization runs only on data that passed (FR-VAL-07).
            normalized, changes = self._normalize(connection, run_id, context, performed_by)
            summary.normalized_items = normalized
            summary.normalization_changes = changes
            self.repositories.runs.set_status(connection, run_id, "processing")

        return summary

    # -- individual tests ---------------------------------------------------
    def _check_control_totals(self, run_id: int, item_type: str, source_file,
                              summary: ValidationSummary) -> list[dict[str, Any]]:
        """VAL-03: declared totals must equal stored totals, and the balances must roll forward."""
        results = []
        actual = self.repositories.items.totals_cents(run_id, item_type)
        declared_debits = source_file["control_total_debits_cents"]
        declared_credits = source_file["control_total_credits_cents"]

        matched = (actual["debit"] == declared_debits and actual["credit"] == declared_credits)
        results.append(self._result(
            run_id, source_file["source_file_id"], "VAL-03", "file", None,
            "pass" if matched else "fail",
            f"declared {declared_debits}/{declared_credits} cents (debit/credit), "
            f"stored {actual['debit']}/{actual['credit']}"))
        if not matched:
            summary.failures.append(f"VAL-03 {item_type}: control totals do not agree")
            summary.passed = False

        opening = source_file["opening_balance_cents"]
        closing = source_file["closing_balance_cents"]
        if opening is not None and closing is not None:
            sign = 1 if item_type == "bank" else -1      # a ledger debit increases cash
            computed = opening + sign * (actual["credit"] - actual["debit"])
            expected = closing if item_type == "bank" else closing
            rolls_forward = computed == expected
            results.append(self._result(
                run_id, source_file["source_file_id"], "VAL-03", "file", None,
                "pass" if rolls_forward else "fail",
                f"opening {opening} + movement = {computed}, declared closing {expected}"))
            if not rolls_forward:
                summary.failures.append(f"VAL-03 {item_type}: balances do not roll forward")
                summary.passed = False
        return results

    def _check_duplicate_identifiers(self, run_id: int, item_type: str, items, source_file,
                                     summary: ValidationSummary,
                                     exclusions: list) -> list[dict[str, Any]]:
        """VAL-04: a repeated source identifier is a broken file, so the later row is excluded."""
        key = {"bank": "external_txn_id", "ledger": "external_entry_id",
               "carry_in": "external_item_id"}[item_type]
        id_column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                     "carry_in": "carry_in_item_id"}[item_type]
        seen: dict[str, int] = {}
        results = []
        for row in items:
            identifier = row[key]
            if identifier in seen:
                results.append(self._result(
                    run_id, source_file["source_file_id"], "VAL-04", "row", identifier, "fail",
                    f"identifier repeats row {seen[identifier]}"))
                exclusions.append((item_type, row[id_column], f"duplicate identifier {identifier}"))
                summary.excluded_rows += 1
            else:
                seen[identifier] = row[id_column]
        if not results:
            results.append(self._result(run_id, source_file["source_file_id"], "VAL-04",
                                        "row", None, "pass", "no repeated identifiers"))
        return results

    def _check_possible_duplicates(self, run_id: int, item_type: str, items, source_file,
                                   summary: ValidationSummary,
                                   duplicate_flags: list) -> list[dict[str, Any]]:
        """VAL-05 / BR-07: same amount, direction and description within a day, different ids.

        These are flagged for investigation, never excluded: the system cannot tell which
        copy is genuine, so both go to a reviewer (SCN-07).
        """
        window = self.config.matching.duplicate_window_business_days
        id_column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id"}[item_type]
        date_column = {"bank": "transaction_date", "ledger": "posting_date"}[item_type]

        buckets: dict[tuple, list] = defaultdict(list)
        for row in items:
            description = normalize_description(row["description_original"])[0]
            buckets[(row["amount_cents"], row["direction"], description)].append(row)

        results = []
        for (amount, direction, _), rows in buckets.items():
            if len(rows) < 2:
                continue
            for index, first in enumerate(rows):
                for second in rows[index + 1:]:
                    gap = abs(business_days_between(
                        date.fromisoformat(first[date_column]),
                        date.fromisoformat(second[date_column])))
                    if gap <= window:
                        for row in (first, second):
                            if (item_type, row[id_column]) not in duplicate_flags:
                                duplicate_flags.append((item_type, row[id_column]))
                        results.append(self._result(
                            run_id, source_file["source_file_id"], "VAL-05", "row",
                            f"{first[id_column]},{second[id_column]}", "warning",
                            f"same amount {amount} cents, direction and description "
                            f"within {gap} business day(s)"))
        summary.possible_duplicates = len(duplicate_flags)
        if results:
            summary.warnings.append(f"VAL-05 {item_type}: {len(results)} possible duplicate pair(s)")
        else:
            results.append(self._result(run_id, source_file["source_file_id"], "VAL-05",
                                        "row", None, "pass", "no possible duplicates found"))
        return results

    def _check_period(self, run_id: int, item_type: str, items, source_file,
                      period_start: str, period_end: str,
                      summary: ValidationSummary) -> list[dict[str, Any]]:
        """VAL-06: rows outside the period are a warning, not an exclusion.

        They may be legitimate late-posted items, so a reviewer decides rather than the
        system silently dropping a transaction that affects cash.
        """
        date_column = {"bank": "transaction_date", "ledger": "posting_date"}[item_type]
        id_column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id"}[item_type]
        outside = [row for row in items if not period_start <= row[date_column] <= period_end]
        results = []
        for row in outside:
            results.append(self._result(
                run_id, source_file["source_file_id"], "VAL-06", "row", str(row[id_column]),
                "warning", f"dated {row[date_column]}, outside {period_start} to {period_end}"))
        if outside:
            summary.warnings.append(f"VAL-06 {item_type}: {len(outside)} row(s) outside the period")
        else:
            results.append(self._result(run_id, source_file["source_file_id"], "VAL-06",
                                        "row", None, "pass", "all rows within the period"))
        return results

    def _check_gl_account(self, run_id: int, items, source_file,
                          summary: ValidationSummary) -> list[dict[str, Any]]:
        """VAL-08: every ledger row must belong to the cash account being reconciled."""
        expected = self.config.organization.gl_account_code
        wrong = [row for row in items if row["gl_account_code"] != expected]
        if wrong:
            summary.failures.append(f"VAL-08: {len(wrong)} ledger row(s) not on account {expected}")
            summary.passed = False
            return [self._result(run_id, source_file["source_file_id"], "VAL-08", "row",
                                 str(wrong[0]["ledger_entry_id"]), "fail",
                                 f"{len(wrong)} row(s) are not on account {expected}")]
        return [self._result(run_id, source_file["source_file_id"], "VAL-08", "row", None,
                             "pass", f"all rows on account {expected}")]

    def _check_running_balance(self, run_id: int, summary: ValidationSummary) -> list[dict[str, Any]]:
        """VAL-07: the statement's running balance must move by each item's amount."""
        source_file = self.repositories.source_files.by_type(run_id, "bank")
        if source_file is None:
            return []
        rows = [row for row in self.repositories.items.list(run_id, "bank")
                if row["running_balance_cents"] is not None]
        if len(rows) < 2:
            return [self._result(run_id, source_file["source_file_id"], "VAL-07", "file", None,
                                 "warning", "not enough rows with a running balance to check")]

        breaks = 0
        previous = rows[0]["running_balance_cents"]
        for row in rows[1:]:
            movement = row["amount_cents"] if row["direction"] == "credit" else -row["amount_cents"]
            if previous + movement != row["running_balance_cents"]:
                breaks += 1
            previous = row["running_balance_cents"]

        outcome = "pass" if breaks == 0 else "warning"
        if breaks:
            summary.warnings.append(f"VAL-07: running balance breaks at {breaks} row(s)")
        return [self._result(run_id, source_file["source_file_id"], "VAL-07", "file", None,
                             outcome, f"{breaks} discontinuity(ies) across {len(rows)} rows")]

    # -- normalization ------------------------------------------------------
    def _normalize(self, connection, run_id: int, context: RunContext,
                   performed_by: int | None) -> tuple[int, int]:
        """Normalize every row that passed, recording each change (FR-NRM-05)."""
        normalized_count = 0
        change_rows: list[dict[str, Any]] = []
        now = utc_now()

        for item_type in ("bank", "ledger", "carry_in"):
            for row in self.repositories.items.list(run_id, item_type, statuses=("imported",)):
                id_column = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                             "carry_in": "carry_in_item_id"}[item_type]
                item_id = row[id_column]

                values, changes = normalize_item(row["description_original"],
                                                 row["reference_original"])
                payee_column = "payee_normalized" if item_type == "bank" else (
                    "counterparty_normalized" if item_type == "ledger" else None)
                payee = normalize_description(row["description_original"])[2]
                if payee_column:
                    values[payee_column] = payee

                self.repositories.items.set_normalized(connection, item_type, item_id, values)
                self.repositories.items.set_status(connection, item_type, item_id, "validated")
                normalized_count += 1

                for change in changes:
                    change_rows.append({
                        "run_id": run_id, "item_type": item_type, "item_id": item_id,
                        "field_name": change.field_name,
                        "original_value": change.original_value,
                        "revised_value": change.revised_value,
                        "rule_name": change.rule_name, "created_at": now})

        self.repositories.normalization.add(connection, change_rows)
        self.audit.write(connection, context, AuditEvent(
            event_type="NORMALIZATION", process_code="P2", entity_type="reconciliation_run",
            entity_id=run_id,
            actor_type="human" if performed_by else "system", actor_user_id=performed_by,
            revised_values={"items_normalized": normalized_count,
                            "fields_changed": len(change_rows)},
            previous_status="imported", new_status="validated"))
        return normalized_count, len(change_rows)

    # -- helper -------------------------------------------------------------
    @staticmethod
    def _result(run_id: int, source_file_id: int | None, test_code: str, scope: str,
                row_reference: str | None, outcome: str, message: str) -> dict[str, Any]:
        return {"run_id": run_id, "source_file_id": source_file_id, "test_code": test_code,
                "test_name": TESTS[test_code][0], "scope": scope,
                "row_reference": row_reference, "outcome": outcome, "message": message,
                "created_at": utc_now()}
