"""Command line tests (TC-I-069..070): the setup, pipeline and evidence commands end to end."""

from __future__ import annotations

from pathlib import Path

from app.cli import main
from app.config import load_config
from app.infra.db import open_database
from app.infra.repositories import Repositories
from tests.conftest import REPOSITORY_ROOT


def temporary_config(tmp_path: Path) -> Path:
    """The default configuration, pointed at a database and report folder inside tmp_path."""
    text = (REPOSITORY_ROOT / "config" / "default.toml").read_text(encoding="utf-8")
    text = text.replace('database = "reconciliation.db"', f'database = "{(tmp_path / "cli.db").as_posix()}"')
    text = text.replace('report_output = "reports"', f'report_output = "{(tmp_path / "reports").as_posix()}"')
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_tci_069_init_run_report_and_verify(tmp_path):
    config = temporary_config(tmp_path)
    assert main(["--config", str(config), "init"]) == 0
    assert main(["--config", str(config), "run"]) == 0
    assert main(["--config", str(config), "status"]) == 0
    assert main(["--config", str(config), "report"]) == 0
    assert main(["--config", str(config), "verify"]) == 0

    database = open_database(tmp_path / "cli.db", REPOSITORY_ROOT / "db" / "schema.sql")
    repositories = Repositories.for_database(database)
    run = repositories.runs.latest()
    assert run["status"] == "in_review"
    assert len(repositories.reports.current(run["run_id"])) == 26
    assert (tmp_path / "reports" / f"run_{run['run_id']}" / "rpt_09.html").exists()


def test_tci_070_reset_needs_force(tmp_path):
    config = temporary_config(tmp_path)
    main(["--config", str(config), "init"])
    assert main(["--config", str(config), "reset"]) == 2
    assert (tmp_path / "cli.db").exists()
    assert main(["--config", str(config), "reset", "--force"]) == 0
    assert not (tmp_path / "cli.db").exists()
    assert load_config(config, Path("does-not-exist.toml")).paths.database == tmp_path / "cli.db"
