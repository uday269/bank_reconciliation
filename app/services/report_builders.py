"""Assemble the data for each report in the package (RPT-01..13, DOC-02 section 6).

Builders read the run once into `RunFacts` and return `ReportData`; rendering is done
elsewhere. Every figure comes from stored records, never from recomputation that could
disagree with them: the statement is the period service's, decisions are the decision
rows, and the audit log is the chain itself.

Ordering is deterministic (record identifiers and business dates), so identical input
gives identical report content and identical content hashes (FR-RPT-04).

Ground truth is never read here. Accuracy and the false automatic match rate need it, so
they are reported by the offline evaluation (ADR-11); RPT-12 says so and reports what the
reviewers' decisions show.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from app.domain import exceptions as exception_catalog
from app.domain.risk import confidence_band
from app.domain.statement import Statement, money
from app.infra.audit import AuditLog
from app.infra.reports.render import ReportData, Table
from app.infra.repositories import Repositories

Key = tuple[str, int]
ITEM_TYPES = ("bank", "ledger", "carry_in")
ID_COLUMN = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id", "carry_in": "carry_in_item_id"}
EXTERNAL = {"bank": "external_txn_id", "ledger": "external_entry_id", "carry_in": "external_item_id"}
DATE = {"bank": "transaction_date", "ledger": "posting_date", "carry_in": "original_date"}
FILE_TYPE_TO_ITEMS = {"bank": "bank", "gl": "ledger", "carry_in": "carry_in"}
OUTCOME = {"approve": "approved", "modify": "approved (modified)", "reject": "rejected",
           "escalate": "escalated", "unresolved": "unresolved"}


def amount(cents: int | None) -> str:
    """Plain decimal for both HTML and CSV: sortable, no currency symbol."""
    return "" if cents is None else f"{cents / 100:.2f}"


class RunFacts:
    """Everything the reports need, read once per package."""

    def __init__(self, repositories: Repositories, audit: AuditLog, run_id: int,
                 statement: Statement, organization_name: str):
        self.repositories = repositories
        self.audit = audit
        self.run_id = run_id
        self.run = repositories.runs.get(run_id)
        self.statement = statement
        self.organization = organization_name
        snapshot = json.loads(self.run["parameter_snapshot"])["parameters"]
        self.high_band = snapshot["confidence_high_band"]
        self.low_band = snapshot["confidence_low_band"]

        self.items: dict[Key, sqlite3.Row] = {}
        for item_type in ITEM_TYPES:
            for row in repositories.items.list(run_id, item_type):
                self.items[(item_type, row[ID_COLUMN[item_type]])] = row
        self.recommendations = repositories.recommendations.for_run(run_id)
        self.decisions: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in repositories.decisions.for_run(run_id):
            self.decisions[row["recommendation_id"]].append(row)
        self.adjustments = repositories.adjustments.for_run(run_id)
        self.events = audit.events(run_id)
        self._users: dict[int, str] = {}
        self._candidates: dict[int, list[sqlite3.Row]] = {}
        self._members: dict[int, list[Key]] = {}

    # -- lookups --------------------------------------------------------------
    def user(self, user_id: int | None) -> str:
        if user_id is None:
            return "System"
        if user_id not in self._users:
            self._users[user_id] = self.repositories.users.get(user_id)["full_name"]
        return self._users[user_id]

    def ref(self, key: Key) -> str:
        return self.items[key][EXTERNAL[key[0]]]

    def candidates(self, recommendation_id: int) -> list[sqlite3.Row]:
        if recommendation_id not in self._candidates:
            self._candidates[recommendation_id] = self.repositories.recommendations.candidates(recommendation_id)
        return self._candidates[recommendation_id]

    def members(self, candidate_id: int) -> list[Key]:
        if candidate_id not in self._members:
            keys = []
            for row in self.repositories.recommendations.candidate_members(candidate_id):
                for item_type in ITEM_TYPES:
                    if row[ID_COLUMN[item_type]] is not None:
                        keys.append((item_type, int(row[ID_COLUMN[item_type]])))
            self._members[candidate_id] = keys
        return self._members[candidate_id]

    def subject(self, rec: sqlite3.Row) -> Key:
        return (rec["subject_item_type"], rec["subject_item_id"])

    def latest(self, rec: sqlite3.Row) -> sqlite3.Row | None:
        history = self.decisions.get(rec["recommendation_id"])
        return history[-1] if history else None

    def outcome(self, rec: sqlite3.Row) -> str:
        latest = self.latest(rec)
        if latest is not None:
            return OUTCOME[latest["decision"]]
        if rec["status"] == "superseded":
            return "superseded"
        return "escalated" if self.items[self.subject(rec)]["status"] == "escalated" else "awaiting decision"

    def accepted(self, rec: sqlite3.Row) -> list[Key]:
        """The records this recommendation pairs: chosen candidate, leading one, or the subject."""
        latest = self.latest(rec)
        if latest is not None and latest["decision"] == "modify" and latest["chosen_candidate_id"]:
            return self.members(latest["chosen_candidate_id"])
        candidates = self.candidates(rec["recommendation_id"])
        if rec["kind"] == "match" and candidates:
            return self.members(candidates[0]["candidate_id"])
        return [self.subject(rec)]

    def is_hand_paired(self, rec: sqlite3.Row) -> bool:
        latest = self.latest(rec)
        if latest is None or latest["decision"] != "modify" or not latest["chosen_candidate_id"]:
            return False
        chosen = next(c for c in self.candidates(rec["recommendation_id"])
                      if c["candidate_id"] == latest["chosen_candidate_id"])
        return json.loads(chosen["feature_values"]).get("origin") == "reviewer"

    def band(self, confidence: float | None) -> str:
        return confidence_band(confidence, self.high_band, self.low_band)

    def records(self, keys: list[Key]) -> str:
        return "; ".join(self.ref(key) for key in keys)


# ---------------------------------------------------------------------------
# RPT-01..RPT-03: data intake
# ---------------------------------------------------------------------------

def rpt_01_data_import(facts: RunFacts) -> ReportData:
    files = facts.repositories.source_files.for_run(facts.run_id)
    table = Table("Source files", ["Type", "File", "Rows", "Control debits", "Control credits",
                                   "Opening balance", "Closing balance", "Uploaded by", "Uploaded at", "SHA-256"],
                  numeric=["Rows", "Control debits", "Control credits", "Opening balance", "Closing balance"])
    for row in files:
        table.rows.append([row["file_type"], row["file_name"], str(row["row_count"]),
                           amount(row["control_total_debits_cents"]), amount(row["control_total_credits_cents"]),
                           amount(row["opening_balance_cents"]), amount(row["closing_balance_cents"]),
                           facts.user(row["uploaded_by"]), row["uploaded_at"], row["sha256"]])
    return ReportData("RPT-01", "Data Import Report",
                      "What was imported, by whom and when, with control totals and file hashes.",
                      [("Files imported", str(len(files)))], [table],
                      ["A file with the same hash cannot be imported twice into a run (FR-IMP-05)."])


def rpt_02_data_quality(facts: RunFacts) -> ReportData:
    validation = facts.repositories.validation.for_run(facts.run_id)
    tests = Table("Validation tests", ["Test", "Name", "Scope", "Row", "Outcome", "Message"])
    for row in validation:
        tests.rows.append([row["test_code"], row["test_name"], row["scope"], row["row_reference"] or "",
                           row["outcome"], row["message"] or ""])

    counts = Table("Records by source", ["Source", "Imported", "Excluded", "Possible duplicates", "Retained"],
                   numeric=["Imported", "Excluded", "Possible duplicates", "Retained"])
    excluded = Table("Excluded records", ["Record", "Reason"])
    for item_type in ITEM_TYPES:
        rows = [row for key, row in facts.items.items() if key[0] == item_type]
        out = [row for row in rows if row["status"] == "excluded"]
        duplicates = sum(1 for row in rows if "is_possible_duplicate" in row.keys() and row["is_possible_duplicate"])
        counts.rows.append([item_type, str(len(rows)), str(len(out)), str(duplicates), str(len(rows) - len(out))])
        excluded.rows += [[row[EXTERNAL[item_type]],
                           (row["exclusion_reason"] if "exclusion_reason" in row.keys() else None) or ""]
                          for row in out]

    controls = Table("Control totals", ["Source", "Control debits", "Retained debits", "Control credits",
                                        "Retained credits"],
                     numeric=["Control debits", "Retained debits", "Control credits", "Retained credits"])
    for row in facts.repositories.source_files.for_run(facts.run_id):
        totals = facts.repositories.items.totals_cents(facts.run_id, FILE_TYPE_TO_ITEMS[row["file_type"]])
        controls.rows.append([row["file_type"], amount(row["control_total_debits_cents"]), amount(totals["debit"]),
                              amount(row["control_total_credits_cents"]), amount(totals["credit"])])

    outcomes = Counter(row["outcome"] for row in validation)
    return ReportData("RPT-02", "Data Quality Report",
                      "Validation tests and their results, excluded and possible duplicate records, and "
                      "control-total comparison.",
                      [("Tests passed", str(outcomes.get("pass", 0))), ("Warnings", str(outcomes.get("warning", 0))),
                       ("Failures", str(outcomes.get("fail", 0)))],
                      [counts, controls, excluded, tests],
                      ["Retained totals exclude rows that failed a row-level test, so a difference from the "
                       "control total equals the excluded rows.",
                       "Possible duplicates are flagged for review, never removed automatically."])


def rpt_03_transformation(facts: RunFacts) -> ReportData:
    changes = facts.repositories.normalization.for_run(facts.run_id)
    table = Table("Transformations", ["Record", "Field", "Original value", "Revised value", "Rule"])
    for row in changes:
        key = (row["item_type"], row["item_id"])
        table.rows.append([facts.ref(key) if key in facts.items else f"{key[0]} {key[1]}",
                           row["field_name"], row["original_value"] or "", row["revised_value"] or "",
                           row["rule_name"]])
    by_rule = Counter(row["rule_name"] for row in changes)
    return ReportData("RPT-03", "Transformation Report",
                      "Every normalization applied, with the original value kept beside the revised one.",
                      [("Changes", str(len(changes)))] + [(rule, str(n)) for rule, n in sorted(by_rule.items())],
                      [table], ["Original values are never overwritten (FR-NRM-05)."])


# ---------------------------------------------------------------------------
# RPT-04..RPT-06: what the system proposed
# ---------------------------------------------------------------------------

def rpt_04_exact_match(facts: RunFacts) -> ReportData:
    table = Table("Rule-based matches", ["Recommendation", "Rule", "Records", "Amount", "Business date",
                                         "Risk", "Queue", "Outcome", "Decided by", "Proposed at"],
                  numeric=["Amount"])
    rule_matches = [rec for rec in facts.recommendations if rec["source"] == "rule" and rec["kind"] == "match"]
    for rec in rule_matches:
        subject = facts.items[facts.subject(rec)]
        latest = facts.latest(rec)
        table.rows.append([str(rec["recommendation_id"]), rec["rule_name"],
                           facts.records(facts.accepted(rec)), amount(subject["amount_cents"]),
                           subject[DATE[rec["subject_item_type"]]], rec["risk_level"], rec["category_code"],
                           facts.outcome(rec), facts.user(latest["decided_by"]) if latest else "",
                           rec["created_at"]])
    return ReportData("RPT-04", "Exact Match Report",
                      "Matches settled by a deterministic rule, the records linked and the review outcome.",
                      [("Rule-based matches", str(len(rule_matches)))]
                      + [(rule, str(n)) for rule, n in sorted(Counter(r["rule_name"] for r in rule_matches).items())],
                      [table], ["Exact matches carry no confidence value: a rule either holds or it does not."])


def rpt_05_ai_recommendation(facts: RunFacts) -> ReportData:
    scored = [rec for rec in facts.recommendations if rec["source"] == "ai"]
    table = Table("Scored recommendations",
                  ["Recommendation", "Record", "Candidates", "Leading score", "Confidence", "Band",
                   "Runner-up confidence", "Relationship", "Risk", "Risk rules", "Queue", "Explanation", "Outcome"],
                  numeric=["Candidates", "Leading score", "Confidence", "Runner-up confidence"])
    versions: set[int] = set()
    for rec in scored:
        system = [c for c in facts.candidates(rec["recommendation_id"])
                  if json.loads(c["feature_values"]).get("origin") != "reviewer"]
        runner_up = system[1]["confidence"] if len(system) > 1 else None
        versions.add(rec["model_version_id"])
        table.rows.append([str(rec["recommendation_id"]), facts.ref(facts.subject(rec)), str(len(system)),
                           f"{system[0]['score']:.4f}" if system else "",
                           f"{rec['confidence']:.4f}" if rec["confidence"] is not None else "",
                           facts.band(rec["confidence"]),
                           f"{runner_up:.4f}" if runner_up is not None else "",
                           rec["relationship"], rec["risk_level"],
                           ", ".join(json.loads(rec["risk_rules"] or "[]")), rec["category_code"],
                           rec["explanation"], facts.outcome(rec)])
    model_rows = Table("Model versions", ["Version", "Algorithm", "Calibration dataset", "Seed", "Fitted at"])
    for version_id in sorted(v for v in versions if v is not None):
        row = facts.repositories.recommendations.model_version(version_id)
        if row is None:
            # The schema's foreign key makes this unreachable; if it ever happens, the report
            # says so rather than failing, so the rest of the package is still produced.
            model_rows.rows.append([f"model version {version_id} not found", "", "", "", ""])
            continue
        model_rows.rows.append([row["version_label"], row["algorithm"], row["calibration_dataset"],
                                str(row["calibration_seed"]), row["trained_at"]])
    prose = Table("Generative AI prose (labelled, never a score or decision)", ["Recommendation", "Prose"])
    prose.rows = [[str(r["recommendation_id"]), r["genai_prose"]] for r in scored if r["genai_prose"]]
    bands = Counter(facts.band(rec["confidence"]) for rec in scored)
    return ReportData("RPT-05", "AI Recommendation Report",
                      "Scored recommendations with model version, candidate scores, calibrated confidence, "
                      "risk and the explanation shown to the reviewer.",
                      [("Scored recommendations", str(len(scored)))]
                      + [(f"{band} confidence", str(bands.get(band, 0))) for band in ("High", "Medium", "Low")],
                      [model_rows, table, prose],
                      ["Every candidate is kept and shown to the reviewer, not only the leading one (FR-REV-09).",
                       "Confidence orders the queue; it never approves anything (CR-01)."])


def rpt_06_exception(facts: RunFacts) -> tuple[ReportData, dict[Key, tuple[int, str]]]:
    rows = [rec for rec in facts.recommendations if rec["kind"] == "exception"]
    table = Table("Exceptions", ["Recommendation", "Code", "Category", "Record", "Amount", "Business date",
                                 "Reason", "Next action", "Risk", "Queue", "Disposition", "Decided by"],
                  numeric=["Amount"])
    listed: dict[Key, tuple[int, str]] = {}
    for rec in rows:
        key = facts.subject(rec)
        item = facts.items[key]
        latest = facts.latest(rec)
        outcome = "paired by reviewer" if facts.is_hand_paired(rec) else facts.outcome(rec)
        table.rows.append([str(rec["recommendation_id"]), rec["exception_code"],
                           exception_catalog.get(rec["exception_code"]).name, facts.ref(key),
                           amount(item["amount_cents"]), item[DATE[key[0]]], rec["explanation"],
                           exception_catalog.next_action(rec["exception_code"]), rec["risk_level"],
                           rec["category_code"], outcome, facts.user(latest["decided_by"]) if latest else ""])
        if rec["status"] != "superseded" and not facts.is_hand_paired(rec):
            listed[key] = (item["amount_cents"], outcome)
    counts = Counter(rec["exception_code"] for rec in rows if rec["status"] != "superseded")
    report = ReportData("RPT-06", "Exception Report",
                        "Every exception, why it was raised, the records involved and the disposition.",
                        [(f"{code} {exception_catalog.get(code).name}", str(n)) for code, n in sorted(counts.items())],
                        [table], ["Superseded exceptions stay listed: they were replaced by a reviewer's pairing "
                                  "or a re-proposal and remain part of the record."])
    return report, listed


# ---------------------------------------------------------------------------
# RPT-07, RPT-08, RPT-10: human decisions
# ---------------------------------------------------------------------------

def rpt_07_adjustment(facts: RunFacts) -> ReportData:
    table = Table("Adjustments", ["Adjustment", "Record", "Exception", "Amount", "Debit", "Credit", "Rationale",
                                  "Evidence", "Prepared by", "Prepared at", "Status", "Decided by", "Decided at",
                                  "Comment", "Posted"], numeric=["Amount"])
    for row in facts.adjustments:
        rec = facts.repositories.recommendations.get(row["recommendation_id"])
        decisions = facts.repositories.adjustments.decisions(row["adjustment_id"])
        decision = decisions[-1] if decisions else None
        table.rows.append([str(row["adjustment_id"]), facts.ref(facts.subject(rec)), rec["exception_code"] or "",
                           amount(row["amount_cents"]), row["debit_account_code"], row["credit_account_code"],
                           row["rationale"], row["evidence_refs"] or "", facts.user(row["prepared_by"]),
                           row["prepared_at"], row["status"], facts.user(decision["decided_by"]) if decision else "",
                           decision["decided_at"] if decision else "", (decision["comment"] or "") if decision else "",
                           "No"])
    totals = facts.repositories.adjustments.totals_cents(facts.run_id)
    return ReportData("RPT-07", "Adjustment Report",
                      "Proposed and approved correcting entries, with accounts, rationale, evidence and approvers.",
                      [("Proposed (awaiting approval)", money(totals["proposed"])),
                       ("Approved", money(totals["approved"])), ("Rejected", money(totals["rejected"]))],
                      [table], ["Approved adjustments are for entry in the general ledger by the accounting team. "
                                "The system never posts them (CR-12)."])


def rpt_08_approval(facts: RunFacts) -> ReportData:
    decisions = [row for history in facts.decisions.values() for row in history]
    decisions.sort(key=lambda row: row["review_decision_id"])
    table = Table("Review decisions", ["Decision", "Recommendation", "Reviewer", "Level", "Action", "Comment",
                                       "Detail opened", "Decided", "Seconds", "Status change", "Batch"],
                  numeric=["Seconds"])
    timed: list[int] = []
    for row in decisions:
        seconds = _seconds(row["opened_at"], row["decided_at"])
        if seconds is not None:
            timed.append(seconds)
        table.rows.append([str(row["review_decision_id"]), str(row["recommendation_id"]),
                           facts.user(row["decided_by"]), row["decision_level"], row["decision"],
                           row["comment"] or "", row["opened_at"] or "", row["decided_at"],
                           "" if seconds is None else str(seconds),
                           f"{row['previous_status']} → {row['new_status']}",
                           str(row["review_batch_id"] or "")])
    adjustment_table = Table("Adjustment decisions", ["Adjustment", "Approver", "Decision", "Comment", "Decided at"])
    for adjustment in facts.adjustments:
        for row in facts.repositories.adjustments.decisions(adjustment["adjustment_id"]):
            adjustment_table.rows.append([str(adjustment["adjustment_id"]), facts.user(row["decided_by"]),
                                          row["decision"], row["comment"] or "", row["decided_at"]])
    signoffs = Table("Manager sign-off", ["Signed by", "Signed at", "Unresolved difference", "Comment"],
                     numeric=["Unresolved difference"])
    for row in facts.repositories.periods.signoffs(facts.run_id):
        signoffs.rows.append([facts.user(row["signed_by"]), row["signed_at"],
                              amount(row["unresolved_difference_cents"]), row["comment"] or ""])

    actions = Counter(row["decision"] for row in decisions)
    batches = {row["review_batch_id"] for row in decisions if row["review_batch_id"]}
    summary = [(action.capitalize(), str(actions.get(action, 0)))
               for action in ("approve", "modify", "reject", "escalate", "unresolved")]
    summary += [("Batches", str(len(batches))),
                ("Decisions with measured time", str(len(timed))),
                ("Median seconds per timed decision", str(int(median(timed))) if timed else "not measured")]
    return ReportData("RPT-08", "Approval Report",
                      "Every reviewer decision with identity, comment, timing and status change, plus "
                      "adjustment approvals and manager sign-off.",
                      summary, [table, adjustment_table, signoffs],
                      ["Time is measured from the recorded opening of an item's detail view to its decision; "
                       "decisions made without opening the detail view (exact-match batches) are not timed (DD-04).",
                       "A batch writes one decision per item; the batch number links them (DD-02)."])


def rpt_10_change_and_override(facts: RunFacts) -> ReportData:
    modifications = Table("Modifications and reviewer pairings",
                          ["Recommendation", "Original proposal", "Revised pairing", "Origin", "Reviewer",
                           "Reason", "Decided at"])
    rejections = Table("Rejected proposals", ["Recommendation", "Original proposal", "Reviewer", "Reason",
                                              "Decided at", "Re-proposed as"])
    senior = Table("Senior decisions on escalated items", ["Recommendation", "Escalated by", "Escalation reason",
                                                           "Decided by", "Decision", "Comment"])
    by_id = {rec["recommendation_id"]: rec for rec in facts.recommendations}
    requeue_events = {e["entity_id"]: json.loads(e["revised_values"] or "{}").get("requeued_recommendations", [])
                      for e in facts.events if e["event_type"] == "DECISION"}
    for rec_id, history in sorted(facts.decisions.items()):
        rec = by_id[rec_id]
        candidates = facts.candidates(rec_id)
        original = (facts.records(facts.members(candidates[0]["candidate_id"]))
                    if rec["kind"] == "match" and candidates else f"{facts.ref(facts.subject(rec))} "
                                                                  f"({rec['exception_code']})")
        escalation = next((row for row in history if row["decision"] == "escalate"), None)
        for row in history:
            if row["decision"] == "modify":
                modifications.rows.append([str(rec_id), original, facts.records(facts.members(row["chosen_candidate_id"])),
                                           "reviewer-selected" if facts.is_hand_paired(rec) else "system candidate",
                                           facts.user(row["decided_by"]), row["comment"] or "", row["decided_at"]])
            elif row["decision"] == "reject":
                rejections.rows.append([str(rec_id), original, facts.user(row["decided_by"]), row["comment"] or "",
                                        row["decided_at"], ", ".join(str(i) for i in requeue_events.get(rec_id, []))])
            elif row["decision_level"] == "senior" and escalation is not None:
                senior.rows.append([str(rec_id), facts.user(escalation["decided_by"]), escalation["comment"] or "",
                                    facts.user(row["decided_by"]), row["decision"], row["comment"] or ""])
    reopenings = Table("Period reopenings", ["Request", "Requested by", "Requested at", "Reason", "Original state",
                                             "Decided by", "Decision", "Revised state", "Comment"])
    for row in facts.repositories.periods.reopen_history(facts.run_id):
        reopenings.rows.append([str(row["reopen_request_id"]), facts.user(row["requested_by"]), row["requested_at"],
                                row["reason"], row["original_status"],
                                facts.user(row["decided_by"]) if row["decided_by"] else "awaiting decision",
                                row["decision"] or "", row["revised_status"] or "", row["comment"] or ""])
    return ReportData("RPT-10", "Change and Override Report",
                      "Where a person changed what the system proposed, or a closed period was reopened: "
                      "original state, revised state, reason, requester and approver.",
                      [("Modifications", str(len(modifications.rows))), ("Rejections", str(len(rejections.rows))),
                       ("Senior decisions on escalations", str(len(senior.rows))),
                       ("Reopen requests", str(len(reopenings.rows)))],
                      [modifications, rejections, senior, reopenings],
                      ["Original recommendations and candidates are never altered; changes are new records."])


# ---------------------------------------------------------------------------
# RPT-09, RPT-11..RPT-13: outcome, performance, evidence
# ---------------------------------------------------------------------------

def rpt_09_reconciliation_summary(facts: RunFacts) -> ReportData:
    s = facts.statement
    statement = Table("Bank-to-book statement", ["Line", "Amount"], numeric=["Amount"])
    statement.rows = [["Bank ending balance", amount(s.bank_ending_balance_cents)],
                      ["Add: deposits in transit", amount(s.deposits_in_transit_cents)],
                      ["Less: outstanding checks", amount(-s.outstanding_checks_cents)],
                      ["Adjusted bank balance", amount(s.adjusted_bank_balance_cents)],
                      ["Book ending balance", amount(s.book_ending_balance_cents)],
                      ["Add: bank-originated credits not yet recorded", amount(s.bank_originated_credits_cents)],
                      ["Less: bank-originated debits not yet recorded", amount(-s.bank_originated_debits_cents)],
                      ["Adjusted book balance", amount(s.adjusted_book_balance_cents)],
                      ["Unresolved difference", amount(s.unresolved_difference_cents)]]
    sections = []
    for name, title in (("deposits_in_transit", "Deposits in transit"), ("outstanding_checks", "Outstanding checks"),
                        ("bank_originated", "Bank-originated items"), ("unresolved", "Unresolved items")):
        table = Table(title, ["Record", "Business date", "Amount", "Category", "Description", "Status"],
                      numeric=["Amount"])
        table.rows = [[line.item_ref, line.business_date, amount(line.amount_cents), line.exception_code or "",
                       line.description, line.status] for line in s.section(name)]
        sections.append(table)

    status_counts = Counter(row["status"] for row in facts.items.values())
    statuses = Table("Items by status", ["Status", "Items"], numeric=["Items"])
    statuses.rows = [[status, str(n)] for status, n in sorted(status_counts.items())]

    signoff = Table("Sign-off and lock", ["Signed by", "Signed at", "Comment", "Chain head at signing"])
    for row in facts.repositories.periods.signoffs(facts.run_id):
        signoff.rows.append([facts.user(row["signed_by"]), row["signed_at"], row["comment"] or "",
                             row["chain_head_hash"]])
    locks = [e for e in facts.events if e["event_type"] == "PERIOD_LOCKED"]
    verification = facts.audit.verify(facts.run_id)
    reconciled = status_counts.get("reconciled", 0)
    return ReportData("RPT-09", "Reconciliation Summary",
                      "Bank-to-book reconciliation statement, final totals, unresolved items, approval "
                      "status, sign-off, lock and audit chain head.",
                      [("Period", f"{facts.run['period_start']} to {facts.run['period_end']}"),
                       ("Run status", facts.run["status"]),
                       ("Adjusted bank balance", money(s.adjusted_bank_balance_cents)),
                       ("Adjusted book balance", money(s.adjusted_book_balance_cents)),
                       ("Unresolved difference", money(s.unresolved_difference_cents)),
                       ("Unresolved items", str(s.unresolved_item_count)),
                       ("Items reconciled", f"{reconciled} of {len(facts.items) - status_counts.get('excluded', 0)}"),
                       ("Lock events", str(len(locks))),
                       ("Audit chain", "intact" if verification.intact
                        else f"broken at event {verification.first_broken_sequence}"),
                       ("Chain head at sign-off", signoff.rows[-1][3] if signoff.rows else "not yet signed")],
                      [statement] + sections + [statuses, signoff],
                      ["An item counts as reconciled only when all six conditions in CR-07 hold.",
                       "The chain head shown is the one recorded at sign-off. The live head and full "
                       "verification are in the Complete Audit Log (RPT-13), which changes with every event.",
                       "Database triggers stop changes made through this application; the chain head printed "
                       "here, kept outside the database, is what makes any other change detectable (DD-06)."])


def rpt_11_match(facts: RunFacts) -> tuple[ReportData, dict[Key, tuple[int, str]]]:
    rows = [rec for rec in facts.recommendations if rec["kind"] == "match" or facts.is_hand_paired(rec)]
    table = Table("Matches", ["Recommendation", "Source", "Relationship", "Records", "Amount", "Confidence",
                              "Band", "Risk", "Queue", "Outcome", "Decided by"],
                  numeric=["Amount", "Confidence"])
    listed: dict[Key, tuple[int, str]] = {}
    for rec in rows:
        accepted = facts.accepted(rec)
        hand = facts.is_hand_paired(rec)
        source = "reviewer" if hand else ("exact rule" if rec["source"] == "rule" else "AI-suggested")
        outcome = facts.outcome(rec)
        latest = facts.latest(rec)
        subject = facts.items[facts.subject(rec)]
        relationship = rec["relationship"] if not hand else ("one_to_one" if len(accepted) == 2 else "group")
        table.rows.append([str(rec["recommendation_id"]), source, relationship, facts.records(accepted),
                           amount(subject["amount_cents"]),
                           "" if hand or rec["confidence"] is None else f"{rec['confidence']:.4f}",
                           "not scored" if hand else facts.band(rec["confidence"]), rec["risk_level"],
                           rec["category_code"], outcome, facts.user(latest["decided_by"]) if latest else ""])
        if rec["status"] != "superseded":
            for key in accepted:
                listed[key] = (facts.items[key]["amount_cents"], outcome)
    counts = Counter((r[1], r[9]) for r in table.rows)
    summary = [(f"{source}: {outcome}", str(n)) for (source, outcome), n in sorted(counts.items())]
    return (ReportData("RPT-11", "Match Report",
                       "Exact, AI-suggested and reviewer-paired matches with confidence, risk and outcome.",
                       summary, [table],
                       ["Reviewer pairings carry no score: they were selected by a person and verified "
                        "arithmetically, not scored by the model."]),
            listed)


def rpt_12_ai_performance(facts: RunFacts) -> ReportData:
    scored = [rec for rec in facts.recommendations if rec["source"] == "ai"]
    table = Table("Reviewer outcome by confidence band",
                  ["Band", "Proposed", "Approved as proposed", "Modified", "Rejected", "Escalated",
                   "Unresolved", "Awaiting decision", "Approval rate", "Rejection rate"],
                  numeric=["Proposed", "Approved as proposed", "Modified", "Rejected", "Escalated", "Unresolved",
                           "Awaiting decision", "Approval rate", "Rejection rate"])
    totals = Counter()
    for band in ("High", "Medium", "Low"):
        recs = [rec for rec in scored if facts.band(rec["confidence"]) == band]
        outcome = Counter(facts.outcome(rec) for rec in recs)
        decided = len(recs) - outcome["awaiting decision"] - outcome["escalated"]
        approved, rejected = outcome["approved"], outcome["rejected"]
        totals.update({"proposed": len(recs), "approved": approved, "rejected": rejected, "decided": decided})
        table.rows.append([band, str(len(recs)), str(approved), str(outcome["approved (modified)"]), str(rejected),
                           str(outcome["escalated"]), str(outcome["unresolved"]), str(outcome["awaiting decision"]),
                           _rate(approved, decided), _rate(rejected, decided)])
    return ReportData("RPT-12", "AI Performance Report",
                      "How reviewers responded to the model's proposals, overall and by confidence band.",
                      [("Scored proposals", str(totals["proposed"])),
                       ("Approved as proposed", f"{totals['approved']} ({_rate(totals['approved'], totals['decided'])} of decided)"),
                       ("Rejected by reviewers", f"{totals['rejected']} ({_rate(totals['rejected'], totals['decided'])} of decided)"),
                       ("Review rate", "100% by design: every item requires a recorded human decision (DD-03)")],
                      [table],
                      ["Accuracy, precision, recall and the hypothetical false automatic match rate need the "
                       "labelled ground truth, which the application never reads. They are produced by the "
                       "offline evaluation and reported in the Evaluation Report (ADR-11, DD-01).",
                       "Rejection by a reviewer is not proof that a proposal was wrong, and approval is not "
                       "proof that it was right; these figures describe reviewer agreement only."])


def rpt_13_audit_log(facts: RunFacts) -> ReportData:
    table = Table("Events", ["Sequence", "Time (UTC)", "Event", "Process", "Actor", "Entity", "Decision",
                             "Rule", "Status change", "Comment", "Event hash"], numeric=["Sequence"])
    for event in facts.events:
        actor = facts.user(event["actor_user_id"]) if event["actor_type"] == "human" else "System"
        status = (f"{event['previous_status'] or ''} → {event['new_status'] or ''}"
                  if event["previous_status"] or event["new_status"] else "")
        table.rows.append([str(event["sequence_no"]), event["created_at"], event["event_type"], event["process_code"],
                           actor, f"{event['entity_type']} {event['entity_id'] or ''}".strip(),
                           event["decision"] or "", event["rule_name"] or "", status, event["comment"] or "",
                           event["event_hash"][:16]])
    verification = facts.audit.verify(facts.run_id)
    counts = Counter(event["event_type"] for event in facts.events)
    return ReportData("RPT-13", "Complete Audit Log",
                      "Every event in the run, in order, with the hash chain verification result.",
                      [("Events", str(len(facts.events))), ("Verification", verification.summary()),
                       ("Chain head", verification.head_hash)]
                      + [(event_type, str(n)) for event_type, n in sorted(counts.items())],
                      [table],
                      ["Hashes are shortened to 16 characters here; the CSV export and the database hold the "
                       "full values used by verification.",
                       "Events are never updated or deleted; a correction is a new event (CR-09)."])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seconds(opened_at: str | None, decided_at: str) -> int | None:
    if not opened_at:
        return None
    opened = datetime.strptime(opened_at, "%Y-%m-%dT%H:%M:%SZ")
    decided = datetime.strptime(decided_at, "%Y-%m-%dT%H:%M:%SZ")
    return max(0, int((decided - opened).total_seconds()))


def _rate(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{part / whole:.1%}"


BUILDERS = {
    "RPT-01": rpt_01_data_import, "RPT-02": rpt_02_data_quality, "RPT-03": rpt_03_transformation,
    "RPT-04": rpt_04_exact_match, "RPT-05": rpt_05_ai_recommendation, "RPT-07": rpt_07_adjustment,
    "RPT-08": rpt_08_approval, "RPT-09": rpt_09_reconciliation_summary, "RPT-10": rpt_10_change_and_override,
    "RPT-12": rpt_12_ai_performance, "RPT-13": rpt_13_audit_log,
}
