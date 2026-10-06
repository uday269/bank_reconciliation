"""Command line entry points.

    python -m app.cli init                       create the database and seed reference data
    python -m app.cli run --data data/august_2026    import, validate and match one dataset
    python -m app.cli status [--run 1]           run status, queue counts and chain result
    python -m app.cli verify [--run 1]           verify the audit chain
    python -m app.cli report [--run 1]           generate the report package (RPT-01..13)
    python -m app.cli serve                      start the reviewer interface on 127.0.0.1:8000
    python -m app.cli reset --force              delete the database file and start again

Reviewer decisions, adjustments, sign-off and reopening are made in the reviewer
interface, never from the command line: every one of them needs a selected identity and
goes through the same controls. The command line covers setup, the automated pipeline
and evidence, which is also what the benchmarks use.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from app.config import Config, ConfigError, load_config
from app.infra.audit import AuditLog, utc_now
from app.infra.db import Database, open_database
from app.infra.repositories import Repositories
from app.services.container import Services, load_calibrator
from app.services.import_service import ImportError_, ImportService
from app.services.matching_service import MatchingService
from app.services.validation_service import ValidationService

# Reviewer identities and the chart of accounts the prototype needs. Fictional (ASM-03).
STAFF = [
    ("Maya Castillo", "ROL-01"),
    ("Ethan Brooks", "ROL-01"),
    ("Priya Raman", "ROL-02"),
    ("Daniel Okafor", "ROL-03"),
]

GL_ACCOUNTS = [
    ("1010", "Cash - Operating", "asset"),
    ("1210", "Accounts Receivable", "asset"),
    ("2010", "Accounts Payable", "liability"),
    ("6810", "Bank Service Charges", "expense"),
    ("6820", "Returned Item Charges", "expense"),
    ("7010", "Interest Income", "income"),
    ("9990", "Suspense - Under Investigation", "asset"),
]


def money(cents: int | None) -> str:
    return "-" if cents is None else f"${cents / 100:,.2f}"


def build(config: Config) -> tuple[Database, Repositories, AuditLog]:
    database = open_database(config.paths.database, config.paths.schema)
    return database, Repositories.for_database(database), AuditLog(database)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def command_init(config: Config, args) -> int:
    database, repositories, _ = build(config)
    if repositories.users.active():
        print(f"{config.paths.database} is already initialized.")
        return 0

    with database.transaction() as connection:
        now = utc_now()
        for full_name, role in STAFF:
            repositories.users.create(connection, full_name, role, now)
        repositories.accounts.create_gl_accounts(connection, GL_ACCOUNTS)
        repositories.accounts.create_bank_account(
            connection,
            bank_name=config.organization.bank_name,
            account_label=config.organization.account_label,
            account_mask=config.organization.account_mask,
            gl_account_code=config.organization.gl_account_code,
            currency_code=config.organization.currency)

    print(f"Initialized {config.paths.database}")
    print(f"  {len(STAFF)} identities, {len(GL_ACCOUNTS)} ledger accounts, 1 bank account")
    for user in repositories.users.active():
        print(f"    {user['user_id']}  {user['full_name']:18} {user['role_code']}")
    return 0


def command_run(config: Config, args) -> int:
    database, repositories, audit = build(config)
    account = repositories.accounts.find_bank_account(config.organization.account_mask)
    if account is None:
        print("Run 'python -m app.cli init' first.", file=sys.stderr)
        return 2

    reviewers = repositories.users.by_role("ROL-01")
    if not reviewers:
        print("No ROL-01 identity exists; run init first.", file=sys.stderr)
        return 2
    performed_by = args.user or reviewers[0]["user_id"]

    data_directory = Path(args.data)
    started = time.perf_counter()

    imports = ImportService(database, repositories, audit, config)
    run_id = imports.create_run(
        bank_account_id=account["bank_account_id"], period_start=args.period_start,
        period_end=args.period_end, created_by=performed_by, dataset_seed=args.seed)
    print(f"Run {run_id} created for {args.period_start} to {args.period_end}")

    try:
        result = imports.import_directory(run_id, data_directory, performed_by)
    except ImportError_ as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1

    print("\nImport")
    for imported in result.files:
        print(f"  {imported.file_type:9} {imported.stored_rows:>5} rows stored, "
              f"{len(imported.rejected_rows)} unreadable, hash {imported.sha256[:12]}")
        for identifier, reason in imported.rejected_rows:
            print(f"      {identifier}: {reason}")

    validation = ValidationService(database, repositories, audit, config)
    summary = validation.validate_and_normalize(run_id, performed_by)
    print("\nValidation")
    print(f"  {summary.summary()}")
    for failure in summary.failures:
        print(f"  FAILED  {failure}")
    for warning in summary.warnings:
        print(f"  warning {warning}")
    if not summary.passed:
        print("\nMatching blocked: file-level validation failed (CR-17).", file=sys.stderr)
        return 1

    calibrator = load_calibrator(Path(args.calibration))
    matching = MatchingService(database, repositories, audit, config, calibrator)
    matched = matching.run_rules(run_id, performed_by, use_scoring=not args.rules_only)
    print("\nMatching" + (" (rules only)" if args.rules_only else ""))
    print(f"  {matched.summary()}")
    print("  by rule:      " + ", ".join(f"{rule} {count}" for rule, count in sorted(matched.by_rule.items())))
    print("  by exception: " + ", ".join(f"{code} {count}" for code, count in sorted(matched.by_exception.items())))
    if not args.rules_only:
        print(f"  calibration:  {matched.calibration_version} "
              f"({matched.candidates_generated} candidates, {matched.capped_searches} capped)")
        if matched.by_confidence_band:
            print("  confidence:   " + ", ".join(
                f"{band} {count}" for band, count in sorted(matched.by_confidence_band.items())))

    print("\nReview queues")
    for code in ("CAT-01", "CAT-02", "CAT-03", "CAT-04", "CAT-05"):
        count = matched.by_category.get(code, 0)
        if count:
            print(f"  {code}  {count:>4} items")

    verification = audit.verify(run_id)
    print(f"\nAudit: {audit.count(run_id)} events. {verification.summary()}")
    print(f"Elapsed: {time.perf_counter() - started:.1f}s")
    print(f"\nRun {run_id} is ready for review.")
    return 0


def command_status(config: Config, args) -> int:
    database, repositories, audit = build(config)
    run = repositories.runs.get(args.run) if args.run else repositories.runs.latest()
    if run is None:
        print("No runs yet.")
        return 0

    run_id = run["run_id"]
    print(f"Run {run_id}  {run['period_start']} to {run['period_end']}  status {run['status']}")

    print("\nItems")
    for item_type in ("bank", "ledger", "carry_in"):
        counts = repositories.items.status_counts(run_id, item_type)
        total = sum(counts.values())
        detail = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
        print(f"  {item_type:9} {total:>5}  ({detail})")

    queues = repositories.recommendations.queue_counts(run_id)
    if queues:
        print("\nOpen queues")
        for code, count in sorted(queues.items()):
            print(f"  {code}  {count:>4}")

    exceptions = repositories.recommendations.exception_counts(run_id)
    if exceptions:
        print("\nExceptions")
        for code, count in sorted(exceptions.items()):
            print(f"  {code}  {count:>4}")

    source_files = repositories.source_files.for_run(run_id)
    if source_files:
        print("\nSource files")
        for source in source_files:
            print(f"  {source['file_type']:9} {source['file_name']:24} rows {source['row_count']:>5}  "
                  f"opening {money(source['opening_balance_cents']):>14}  "
                  f"closing {money(source['closing_balance_cents']):>14}")

    print(f"\nAudit: {audit.count(run_id)} events. {audit.verify(run_id).summary()}")
    return 0


def command_verify(config: Config, args) -> int:
    database, repositories, audit = build(config)
    run = repositories.runs.get(args.run) if args.run else repositories.runs.latest()
    if run is None:
        print("No runs yet.")
        return 0
    result = audit.verify(run["run_id"])
    print(result.summary())
    return 0 if result.intact else 1


def command_report(config: Config, args) -> int:
    """Generate the package. Approved items that pass the report check become reconciled."""
    services = Services.build(config)
    run = services.repositories.runs.get(args.run) if args.run else services.repositories.runs.latest()
    if run is None:
        print("No runs yet.")
        return 0
    result = services.reports.generate_package(run["run_id"], args.user)
    print(f"Report package for run {run['run_id']} written to {result.directory}")
    print(f"  {len(result.hashes)} files (13 reports, HTML and CSV)")
    print(f"  items reconciled by the report check: {result.promoted}")
    for failure in result.failures:
        print(f"  report check failed: {failure}")
    print(f"  run status: {result.run_status}")
    return 1 if result.failures else 0


def command_serve(config: Config, args) -> int:
    """Start the reviewer interface. Imported here so the other commands need no web packages."""
    try:
        import uvicorn

        from app.web.main import create_app
    except ImportError as error:
        print(f"The web packages are not installed ({error}). Run: pip install -r requirements.txt",
              file=sys.stderr)
        return 2
    print(f"Reviewer interface: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(config), host=args.host, port=args.port)
    return 0


def command_reset(config: Config, args) -> int:
    if not args.force:
        print("Refusing to delete the database without --force.", file=sys.stderr)
        return 2
    path = config.paths.database
    if path.exists():
        path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = path.with_name(path.name + suffix)
            if extra.exists():
                extra.unlink()
        print(f"Deleted {path}")
    else:
        print(f"{path} does not exist")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli",
                                     description="AI-Assisted Bank Reconciliation")
    parser.add_argument("--config", default="config/default.toml", help="configuration file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the database and seed reference data")

    run_parser = subparsers.add_parser("run", help="import, validate and match one dataset")
    run_parser.add_argument("--data", default="data/august_2026", help="dataset directory")
    run_parser.add_argument("--period-start", default="2026-08-01")
    run_parser.add_argument("--period-end", default="2026-08-31")
    run_parser.add_argument("--seed", type=int, default=20260801)
    run_parser.add_argument("--user", type=int, help="identity performing the run")
    run_parser.add_argument("--calibration", default="models/calibration.json",
                            help="calibration artefact to load")
    run_parser.add_argument("--rules-only", action="store_true",
                            help="skip scoring, reproducing the Stage 6 baseline")

    status_parser = subparsers.add_parser("status", help="run status and queue counts")
    status_parser.add_argument("--run", type=int)

    verify_parser = subparsers.add_parser("verify", help="verify the audit chain")
    verify_parser.add_argument("--run", type=int)

    report_parser = subparsers.add_parser("report", help="generate the report package")
    report_parser.add_argument("--run", type=int)
    report_parser.add_argument("--user", type=int, help="identity generating the package (default: system)")

    serve_parser = subparsers.add_parser("serve", help="start the reviewer interface")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    reset_parser = subparsers.add_parser("reset", help="delete the database file")
    reset_parser.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    commands = {"init": command_init, "run": command_run, "status": command_status,
                "verify": command_verify, "report": command_report, "serve": command_serve,
                "reset": command_reset}
    return commands[args.command](config, args)


if __name__ == "__main__":
    raise SystemExit(main())
