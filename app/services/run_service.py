"""Starting a run from the reviewer interface: create, import, validate and match (UC-01..UC-03).

The import, validation and matching services do the work; this service puts the
controls in front of them so the screen gets the same protection as every other action:
the permission matrix, the run stage, and a BLOCKED_ATTEMPT event when refused.

Files are imported once per run. A file-level validation failure leaves the run in
'validation_failed' as evidence (CR-17); the correction is a new run with corrected
files, never a second import into the same run, so no run ever holds two copies of a
statement.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import Config
from app.control import ControlViolation, period_lock, permissions
from app.infra.audit import AuditLog
from app.infra.db import Database
from app.infra.repositories import Repositories
from app.services.blocked import record_blocked
from app.services.import_service import (BANK_FILE, CARRY_IN_FILE, CONTROL_FILE, LEDGER_FILE,
                                         ImportResult, ImportService)
from app.services.matching_service import MatchingService, MatchingSummary
from app.services.validation_service import ValidationService, ValidationSummary

# Form field -> file name the import service expects. The control file is optional.
UPLOADS = {"bank": BANK_FILE, "ledger": LEDGER_FILE, "carry_in": CARRY_IN_FILE, "control": CONTROL_FILE}
REQUIRED_UPLOADS = ("bank", "ledger", "carry_in")


class RunInputError(ValueError):
    """Missing files or invalid dates. Every problem is listed; nothing is written."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class ProcessResult:
    validation: ValidationSummary
    matching: MatchingSummary | None          # None when validation blocked matching


class RunService:
    def __init__(self, config: Config, database: Database, repositories: Repositories, audit: AuditLog,
                 imports: ImportService, validation: ValidationService, matching: MatchingService):
        self.config = config
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.imports = imports
        self.validation = validation
        self.matching = matching

    def create(self, user_id: int, period_start: str, period_end: str) -> int:
        """Open a run for one month of one account (UC-01). Parameters are frozen at creation.

        A refusal here leaves no BLOCKED_ATTEMPT event: the audit chain belongs to a run,
        and there is no run yet to record it in. The refusal is still shown with its rule.
        """
        user = self.repositories.users.get(user_id)
        permissions.require_role(user, permissions.IMPORT)
        problems = []
        try:
            start, end = date.fromisoformat(period_start), date.fromisoformat(period_end)
            if end < start:
                problems.append("the period end is before its start")
        except ValueError:
            problems.append("period dates must be in the form YYYY-MM-DD")
        account = self.repositories.accounts.find_bank_account(self.config.organization.account_mask)
        if account is None:
            problems.append("no bank account is configured; run 'python -m app.cli init' first")
        if problems:
            raise RunInputError(problems)
        return self.imports.create_run(bank_account_id=account["bank_account_id"], period_start=period_start,
                                       period_end=period_end, created_by=user_id)

    def import_files(self, run_id: int, user_id: int, files: dict[str, bytes]) -> ImportResult:
        """Import the uploaded files as one unit of work (UC-02, FR-IMP-01..06)."""
        try:
            user = self.repositories.users.get(user_id)
            run = self.repositories.runs.get(run_id)
            permissions.require_role(user, permissions.IMPORT)
            period_lock.require_importable(run)
            if run["status"] != "created":
                raise ControlViolation("STATE", "files are imported once per run; start a new run to correct them")
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id, process_code="P1",
                           entity_type="reconciliation_run", entity_id=run_id, attempted="import files",
                           violation=violation)
            raise

        missing = [name for name in REQUIRED_UPLOADS if not files.get(name)]
        if missing:
            raise RunInputError([f"the {name.replace('_', '-')} file is required" for name in missing])
        with tempfile.TemporaryDirectory() as directory:
            for name, content in files.items():
                if content and name in UPLOADS:
                    (Path(directory) / UPLOADS[name]).write_bytes(content)
            return self.imports.import_directory(run_id, Path(directory), user_id)

    def process(self, run_id: int, user_id: int) -> ProcessResult:
        """Validate and normalize, then match if every file-level test passed (UC-03, CR-17)."""
        try:
            user = self.repositories.users.get(user_id)
            run = self.repositories.runs.get(run_id)
            permissions.require_role(user, permissions.IMPORT)
            period_lock.require_importable(run)
            if run["status"] != "validating":
                raise ControlViolation("STATE", "validation runs once, after the files are imported")
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id, process_code="P2",
                           entity_type="reconciliation_run", entity_id=run_id, attempted="validate and match",
                           violation=violation)
            raise

        validation = self.validation.validate_and_normalize(run_id, user_id)
        if not validation.passed:
            return ProcessResult(validation, None)
        return ProcessResult(validation, self.matching.run_rules(run_id, user_id))
