"""Report package and the report check (UC-10, P8, FR-RPT-01..05, CR-07).

`generate_package` does four things, in this order:

1. Builds all 13 reports from stored records.
2. Runs the report check (FR-RPT-05): every approved item must appear in the Match or
   Exception Report with its amount and an approved outcome. An item that also meets
   the other five CR-07 conditions moves 'approved' -> 'report_verified' -> 'reconciled',
   with one REPORT_VERIFIED event per recommendation. Nothing else may set 'reconciled'.
3. Rebuilds the reports if any status changed, writes HTML and CSV for each, records each
   file with its content hash, and writes one REPORT_GENERATED event listing them.
4. Asks the period service to re-evaluate readiness, which may move the run to
   'ready_to_close' (CR-10 condition 5).

A package can be generated at any stage after matching, including after close, when the
Reconciliation Summary then shows the sign-off and chain head (FR-AUD-07). Item statuses
change only while the period is open.

Output goes to `<report_output>/run_<id>/`, one file per report and format. Regenerating
overwrites the files and adds new `report` rows; the earlier rows and their hashes stay
as the record of what was generated before.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.control import completion, period_lock, states
from app.control.completion import ItemEvidence
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.reports.render import Header, ReportData, render_csv, write_report
from app.infra.repositories import Repositories
from app.services import report_builders as builders
from app.services.period_service import PeriodService

Key = tuple[str, int]
REPORT_CODES = tuple(f"RPT-{n:02d}" for n in range(1, 14))


@dataclass
class PackageResult:
    run_id: int
    directory: Path
    hashes: dict[str, str]                       # "RPT-09.html" -> sha256
    promoted: int = 0                            # items moved to 'reconciled'
    failures: list[str] = field(default_factory=list)
    run_status: str = ""


class ReportService:
    def __init__(self, database: Database, repositories: Repositories, audit: AuditLog,
                 period: PeriodService, organization_name: str, output_root: Path):
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.period = period
        self.organization_name = organization_name
        self.output_root = Path(output_root)

    # -- building -----------------------------------------------------------
    def build(self, run_id: int) -> tuple[dict[str, ReportData], dict[Key, tuple[int, str]]]:
        """All 13 reports, plus what the Match and Exception reports list per item."""
        facts = builders.RunFacts(self.repositories, self.audit, run_id,
                                  self.period.statement(run_id), self.organization_name)
        reports = {code: build(facts) for code, build in builders.BUILDERS.items()}
        reports["RPT-06"], exceptions_listed = builders.rpt_06_exception(facts)
        reports["RPT-11"], matches_listed = builders.rpt_11_match(facts)
        listed = {**exceptions_listed, **matches_listed}
        return {code: reports[code] for code in REPORT_CODES}, listed

    def report_directory(self, run_id: int) -> Path:
        return self.output_root / f"run_{run_id}"

    # -- the package --------------------------------------------------------
    def generate_package(self, run_id: int, user_id: int | None = None,
                         at: str | None = None) -> PackageResult:
        at = at or utc_now()
        reports, listed = self.build(run_id)
        result = PackageResult(run_id, self.report_directory(run_id), {})

        run = self.repositories.runs.get(run_id)
        if run["status"] in period_lock.WORKING:
            result.promoted, result.failures = self._verify_and_reconcile(run_id, listed, user_id, at)
            if result.promoted:
                reports, _ = self.build(run_id)

        run = self.repositories.runs.get(run_id)
        header = Header(self.organization_name, self._account_label(run),
                        f"{run['period_start']} to {run['period_end']}", run_id, at)
        with self.database.transaction() as connection:
            for code, report in reports.items():
                paths = write_report(report, header, result.directory)
                digests = {"html": report.content_hash(),
                           "csv": _sha256(render_csv(report))}
                for output_format, path in paths.items():
                    self.repositories.reports.add(
                        connection, run_id=run_id, report_code=code, output_format=output_format,
                        file_path=str(path), content_sha256=digests[output_format], generated_at=at)
                    result.hashes[f"{code}.{output_format}"] = digests[output_format]
            self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                event_type="REPORT_GENERATED", process_code="P8", entity_type="reconciliation_run",
                entity_id=run_id, actor_type="human" if user_id else "system", actor_user_id=user_id,
                evidence_refs=sorted(f"{name}:{digest}" for name, digest in result.hashes.items()),
                revised_values={"reports": len(reports), "items_reconciled": result.promoted,
                                "report_check_failures": result.failures},
                created_at=at))

        result.run_status = self.period.refresh_status(run_id)
        return result

    def export(self, run_id: int, report_code: str, output_format: str, user_id: int | None) -> Path:
        """Hand over the current file of one report and record that it left the system."""
        current = [row for row in self.repositories.reports.current(run_id)
                   if row["report_code"] == report_code and row["output_format"] == output_format]
        if not current:
            raise FileNotFoundError(f"{report_code} has not been generated as {output_format} for run {run_id}")
        row = current[0]
        with self.database.transaction() as connection:
            self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                event_type="REPORT_EXPORTED", process_code="P8", entity_type="report",
                entity_id=row["report_id"], actor_type="human" if user_id else "system",
                actor_user_id=user_id, evidence_refs=[f"{report_code}.{output_format}:{row['content_sha256']}"]))
        return Path(row["file_path"])

    # -- the report check (FR-RPT-05) and CR-07 ------------------------------
    def _verify_and_reconcile(self, run_id: int, listed: dict[Key, tuple[int, str]],
                              user_id: int | None, at: str) -> tuple[int, list[str]]:
        facts = builders.RunFacts(self.repositories, self.audit, run_id,
                                  self.period.statement(run_id), self.organization_name)
        decided_events = {event["entity_id"] for event in facts.events
                          if event["event_type"] == "DECISION" and event["decision"] in ("approve", "modify")}

        # Which recommendation approved each item: chosen, leading, or the exception subject.
        approving: dict[Key, sqlite3.Row] = {}
        for rec in facts.recommendations:
            latest = facts.latest(rec)
            if rec["status"] != "superseded" and latest is not None and latest["decision"] in ("approve", "modify"):
                for key in facts.accepted(rec):
                    approving[key] = rec

        promote: dict[int, list[Key]] = {}
        failures: list[str] = []
        for key, item in facts.items.items():
            if item["status"] != "approved":
                continue
            rec = approving.get(key)
            history = facts.decisions.get(rec["recommendation_id"], []) if rec else []
            requires_senior = bool(rec) and (rec["category_code"] == "CAT-05"
                                             or any(row["decision"] == "escalate" for row in history))
            report_amount, report_outcome = listed.get(key, (None, ""))
            evidence = ItemEvidence(
                item_ref=facts.ref(key),
                validated=item["status"] != "excluded",
                disposition_proposed=rec is not None,
                approval_recorded=completion.approval_satisfied(history, requires_senior),
                evidence_linked=bool(rec) and bool(rec["explanation"])
                and (rec["kind"] == "exception" or bool(facts.candidates(rec["recommendation_id"]))),
                audit_written=bool(rec) and rec["recommendation_id"] in decided_events,
                report_verified=report_amount == item["amount_cents"] and report_outcome.startswith("approved"))
            if completion.is_reconciled(evidence):
                promote.setdefault(rec["recommendation_id"], []).append(key)
            else:
                unmet = [label for _, label, met in completion.evaluate(evidence) if not met]
                failures.append(f"{evidence.item_ref}: {'; '.join(unmet)}")

        if not promote:
            return 0, failures
        promoted = 0
        with self.database.transaction() as connection:
            context = RunContext.load(self.database, run_id)
            for rec_id, keys in sorted(promote.items()):
                for key in keys:
                    # Two steps, in the DOC-03 order: approved -> report_verified -> reconciled.
                    # Each step is checked against the status actually stored, read inside this
                    # transaction, so an item changed since the check above is refused, not
                    # promoted.
                    current = self.repositories.items.get(key[0], key[1], connection)["status"]
                    for new in ("report_verified", "reconciled"):
                        states.require_item_transition(current, new)
                        self.repositories.items.set_status(connection, key[0], key[1], new)
                        current = new
                    promoted += 1
                self.audit.write(connection, context, AuditEvent(
                    event_type="REPORT_VERIFIED", process_code="P8", entity_type="recommendation",
                    entity_id=rec_id, actor_type="human" if user_id else "system", actor_user_id=user_id,
                    item_refs=[facts.ref(key) for key in keys], rule_name="CR-07",
                    explanation="All six reconciliation conditions hold; listed in the Match or "
                                "Exception Report with the expected amount and an approved outcome.",
                    evidence_refs=[f"recommendation:{rec_id}"],
                    previous_status="approved", new_status="reconciled", created_at=at))
        return promoted, failures

    def _account_label(self, run) -> str:
        account = self.repositories.accounts.get_bank_account(run["bank_account_id"])
        return f"{account['bank_name']} {account['account_label']} ····{account['account_mask']}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
