"""A fully wired application state for the web tests: services on a fresh database, seeded
identities, and the August run imported, validated and matched."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.config import load_config
from app.infra.audit import utc_now
from app.services.container import Services
from tests.conftest import DATASET, GL_ACCOUNTS, REPOSITORY_ROOT, STAFF


def build_services(tmp_path: Path, match: bool = True) -> tuple[Services, dict[str, int], int | None]:
    config = load_config(REPOSITORY_ROOT / "config" / "default.toml", Path("does-not-exist.toml"))
    config = replace(config, paths=replace(config.paths, database=tmp_path / "web.db",
                                           schema=REPOSITORY_ROOT / "db" / "schema.sql",
                                           report_output=tmp_path / "reports"))
    services = Services.build(config, REPOSITORY_ROOT / "models" / "calibration.json")
    users: dict[str, int] = {}
    with services.database.transaction() as connection:
        for full_name, role in STAFF:
            users[full_name] = services.repositories.users.create(connection, full_name, role, utc_now())
        services.repositories.accounts.create_gl_accounts(connection, GL_ACCOUNTS)
        account_id = services.repositories.accounts.create_bank_account(
            connection, "Lone Peak Commercial Bank", "Operating Checking", "7310", "1010")
    if not match:
        return services, users, None
    maya = users["Maya Castillo"]
    run_id = services.imports.create_run(bank_account_id=account_id, period_start="2026-08-01",
                                         period_end="2026-08-31", created_by=maya, dataset_seed=20260801)
    services.imports.import_directory(run_id, DATASET, maya)
    services.validation.validate_and_normalize(run_id, maya)
    services.matching.run_rules(run_id, maya)
    return services, users, run_id


UPLOAD_NAMES = {"bank": "bank_statement.csv", "ledger": "gl_cash_detail.csv",
                "carry_in": "carry_in_items.csv", "control": "run_control.csv"}


def dataset_uploads(break_control_total: bool = False) -> dict[str, bytes]:
    """The August files as uploaded bytes; optionally with a control total that will not agree."""
    files = {name: (DATASET / file_name).read_bytes() for name, file_name in UPLOAD_NAMES.items()}
    if break_control_total:
        files["control"] = files["control"].replace(b"1227189.35", b"1227189.36")
    return files


def as_harness(services: Services, users: dict[str, int], run_id: int):
    """The attributes the review helpers in the service tests read, from a web fixture."""
    from types import SimpleNamespace
    return SimpleNamespace(repositories=services.repositories, users=users, run_id=run_id,
                           database=services.database, audit=services.audit)
