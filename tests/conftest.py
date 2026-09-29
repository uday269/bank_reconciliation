"""Shared fixtures.

Every test runs against a fresh database created from db/schema.sql, so tests never
depend on each other's state and never touch the working database.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.config import load_config
from app.infra.audit import AuditLog, utc_now
from app.infra.db import open_database
from app.infra.repositories import Repositories
from app.services.import_service import ImportService
from app.services.matching_service import MatchingService
from app.services.validation_service import ValidationService

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPOSITORY_ROOT / "data" / "august_2026"

STAFF = [("Maya Castillo", "ROL-01"), ("Ethan Brooks", "ROL-01"),
         ("Priya Raman", "ROL-02"), ("Daniel Okafor", "ROL-03")]
GL_ACCOUNTS = [("1010", "Cash - Operating", "asset"), ("6810", "Bank Service Charges", "expense"),
               ("6820", "Returned Item Charges", "expense"), ("7010", "Interest Income", "income"),
               ("9990", "Suspense - Under Investigation", "asset")]


class Harness:
    """Everything a test needs for one run, wired together."""

    def __init__(self, tmp_path: Path):
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.toml",
                                  Path("does-not-exist.toml"))
        self.database = open_database(tmp_path / "test.db",
                                      REPOSITORY_ROOT / "db" / "schema.sql")
        self.repositories = Repositories.for_database(self.database)
        self.audit = AuditLog(self.database)
        self.imports = ImportService(self.database, self.repositories, self.audit, self.config)
        self.validation = ValidationService(self.database, self.repositories, self.audit, self.config)
        self.matching = MatchingService(self.database, self.repositories, self.audit, self.config)
        self.users: dict[str, int] = {}
        self.account_id = 0
        self._seed()

    def _seed(self) -> None:
        with self.database.transaction() as connection:
            for full_name, role in STAFF:
                user_id = self.repositories.users.create(connection, full_name, role, utc_now())
                self.users[full_name] = user_id
            self.repositories.accounts.create_gl_accounts(connection, GL_ACCOUNTS)
            self.account_id = self.repositories.accounts.create_bank_account(
                connection, "Lone Peak Commercial Bank", "Operating Checking", "7310", "1010")

    def new_run(self) -> int:
        return self.imports.create_run(
            bank_account_id=self.account_id, period_start="2026-08-01", period_end="2026-08-31",
            created_by=self.users["Maya Castillo"], dataset_seed=20260801)

    def imported_run(self, directory: Path | None = None) -> int:
        run_id = self.new_run()
        self.imports.import_directory(run_id, directory or DATASET, self.users["Maya Castillo"])
        return run_id

    def matched_run(self) -> int:
        run_id = self.imported_run()
        self.validation.validate_and_normalize(run_id, self.users["Maya Castillo"])
        self.matching.run_rules(run_id, self.users["Maya Castillo"])
        return run_id


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def dataset_copy(tmp_path):
    """A writable copy of the dataset, for tests that need to break a file."""
    target = tmp_path / "dataset"
    shutil.copytree(DATASET, target)
    return target
