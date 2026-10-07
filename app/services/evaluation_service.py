"""Evaluation against labelled ground truth (FR-EVL-01..11, brief section 11).

This module is the only application code that reads `ground_truth.csv` (FR-EVL-11). The
reviewer interface, the services that decide, and the reports never do.

Two kinds of measurement, kept apart because they answer different questions:

  Pipeline     What the system proposes before anyone reviews it. Measured on a fresh
               temporary database for each configuration (rules only, rules plus
               scoring), so the working database is never touched and the two
               configurations differ in one switch only (FR-EVL-09).

  Review       What people did with those proposals: approval and rejection rates, time
               per item, audit completeness, and the statement they signed. Measured on a
               run that people actually reviewed. A run nobody reviewed reports n = 0
               rather than a figure (FR-EVL-10).

Policies fixed before measuring:
  * A match is correct when every record in it carries the same true match group.
  * BR-19, the hypothetical auto-accept policy (DD-01): an exact match with Low risk, or
    a scored match at or above PRM-09 with Low risk. It is computed, never executed.
  * Time per item (DD-04) counts only decisions made after opening the detail view; batch
    approvals are not timed. A person with no timed decisions is reported as not measured.
  * Every rate is printed with the count it was computed from (FR-EVL-10).
"""

from __future__ import annotations

import csv
import json
import random
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from app.config import Config
from app.domain.calibration import CalibrationPoint, Calibrator, CalibrationQuality, assess
from app.domain.exceptions import statement_section
from app.domain.rules import Item
from app.domain.statement import StatementLine, build_statement, money
from app.services.container import Services, load_calibrator
from app.services.reference_data import seed

Key = tuple[str, int]
ITEM_TYPES = ("bank", "ledger", "carry_in")
ID_COLUMN = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id", "carry_in": "carry_in_item_id"}
EXTERNAL = {"bank": "external_txn_id", "ledger": "external_entry_id", "carry_in": "external_item_id"}
BANDS = ("High", "Medium", "Low")
TIMING_PROTOCOL_ITEMS = 50          # per person (DD-04)

# FR-AUD-02 fields, by the event types they apply to. The columns every event carries
# (run, account, period, process, timestamp, actor, entity, hashes) are NOT NULL in the
# schema, so the database enforces them; these are the fields that depend on the event.
APPLICABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "IMPORT": ("item_refs", "revised_values"),
    "VALIDATION": ("revised_values",),
    "NORMALIZATION": ("revised_values",),
    "RULE": ("item_refs", "rule_name", "risk_level", "explanation", "new_status"),
    "AI_RECOMMENDATION": ("item_refs", "model_version_id", "candidate_scores", "confidence",
                          "risk_level", "explanation", "new_status"),
    "ROUTING": ("rule_name", "revised_values"),
    "DECISION": ("item_refs", "decision", "approval_status", "risk_level", "explanation",
                 "evidence_refs", "previous_status", "new_status"),
    "BLOCKED_ATTEMPT": ("rule_name", "decision", "explanation", "approval_status"),
    "ADJUSTMENT_PROPOSED": ("item_refs", "approval_status", "revised_values", "evidence_refs"),
    "ADJUSTMENT_DECIDED": ("item_refs", "decision", "approval_status", "original_values", "revised_values"),
    "REPORT_GENERATED": ("evidence_refs",),
    "REPORT_VERIFIED": ("item_refs", "rule_name", "evidence_refs", "new_status"),
    "REPORT_EXPORTED": ("evidence_refs",),
    "SIGNOFF": ("approval_status", "evidence_refs", "revised_values"),
    "PERIOD_LOCKED": ("evidence_refs", "new_status"),
    "REOPEN_REQUESTED": ("comment", "approval_status"),
    "REOPEN_DECIDED": ("decision", "approval_status", "original_values", "revised_values"),
    "STATUS_CHANGED": ("rule_name", "previous_status", "new_status"),
    "CHAIN_VERIFIED": ("approval_status", "evidence_refs"),
    "GENAI_PROSE": ("rule_name", "evidence_refs", "revised_values"),
}
COMMENTED_DECISIONS = {"reject", "modify", "escalate", "unresolved"}


def rate(part: int, whole: int) -> float | None:
    return None if whole == 0 else part / whole


def shown(part: int, whole: int, digits: int = 4) -> str:
    """A rate with its count beside it (FR-EVL-10)."""
    value = rate(part, whole)
    return f"n/a (n=0)" if value is None else f"{value:.{digits}f} ({part}/{whole})"


def load_truth(data_directory: Path) -> dict[tuple[str, str], dict[str, str]]:
    with (data_directory / "ground_truth.csv").open(newline="", encoding="utf-8") as handle:
        return {(row["item_type"], row["item_id"]): row for row in csv.DictReader(handle)}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class PipelineMetrics:
    label: str
    elapsed_seconds: float
    proposed: int = 0
    correct: int = 0
    true_groups: int = 0
    found_groups: int = 0
    by_scenario: dict[str, tuple[int, int]] = field(default_factory=dict)
    by_source: dict[str, tuple[int, int]] = field(default_factory=dict)          # correct, proposed
    bands: dict[str, tuple[int, int]] = field(default_factory=dict)              # correct, proposed
    calibration: CalibrationQuality | None = None
    exception_agreed: int = 0
    exception_total: int = 0
    exception_confusion: dict[tuple[str, str], int] = field(default_factory=dict)  # (true, proposed)
    risk_agreed: int = 0
    risk_total: int = 0
    hypothetical_accepted: int = 0
    hypothetical_wrong: int = 0
    items_total: int = 0
    items_hypothetically_settled: int = 0
    queues: dict[str, int] = field(default_factory=dict)
    missed: dict[str, list[str]] = field(default_factory=dict)                   # scenario -> group ids
    capped_searches: int = 0
    provisional_difference_cents: int = 0
    wrong_examples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return rate(self.correct, self.proposed)

    @property
    def recall(self) -> float | None:
        return rate(self.found_groups, self.true_groups)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return None if not p or not r else 2 * p * r / (p + r)


@dataclass
class ReviewMetrics:
    run_id: int
    status: str
    recommendations: int = 0
    decided: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    approvals_correct: int = 0
    approvals_checked: int = 0
    batches: int = 0
    batch_decisions: int = 0
    manual_pairings: int = 0
    by_band: dict[str, Counter] = field(default_factory=dict)
    timing: dict[str, list[int]] = field(default_factory=dict)          # person -> seconds
    blocked_attempts: Counter = field(default_factory=Counter)          # rule -> count
    audit_total: int = 0
    audit_complete: int = 0
    audit_incomplete: dict[str, Counter] = field(default_factory=dict)  # event type -> missing field
    chain_intact: bool = False
    signed_difference_cents: int | None = None
    current_difference_cents: int = 0
    adjustments: Counter = field(default_factory=Counter)


@dataclass
class Evaluation:
    data_directory: Path
    calibration_label: str
    rules_only: PipelineMetrics
    hybrid: PipelineMetrics
    true_difference_cents: int
    review: ReviewMetrics | None = None


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------

class EvaluationService:
    def __init__(self, config: Config, calibration_path: Path):
        self.config = config
        self.calibration_path = calibration_path
        self.calibrator: Calibrator = load_calibrator(calibration_path)

    # -- pipeline -----------------------------------------------------------
    def evaluate(self, data_directory: Path, review_services: Services | None = None,
                 review_run_id: int | None = None) -> Evaluation:
        truth = load_truth(data_directory)
        rules_only = self.measure_pipeline(data_directory, truth, use_scoring=False)
        hybrid = self.measure_pipeline(data_directory, truth, use_scoring=True)
        evaluation = Evaluation(data_directory, self.calibrator.version_label, rules_only, hybrid,
                                true_difference_cents=self._true_difference(data_directory, truth))
        if review_services is not None and review_run_id is not None:
            evaluation.review = self.measure_review(review_services, review_run_id, truth)
        return evaluation

    def measure_pipeline(self, data_directory: Path, truth: dict, use_scoring: bool) -> PipelineMetrics:
        with tempfile.TemporaryDirectory() as directory:
            services, run_id, elapsed = self._fresh_run(Path(directory), data_directory, use_scoring)
            try:
                return self._pipeline_metrics(services, run_id, truth, elapsed,
                                              "rules plus scoring" if use_scoring else "rules only")
            finally:
                services.database.close()

    def _fresh_run(self, directory: Path, data_directory: Path, use_scoring: bool) -> tuple[Services, int, float]:
        config = replace(self.config, paths=replace(self.config.paths, database=directory / "evaluation.db",
                                                    report_output=directory / "reports"))
        services = Services.build(config, self.calibration_path)
        users = seed(services.database, services.repositories, config)
        account = services.repositories.accounts.find_bank_account(config.organization.account_mask)
        maya = users["Maya Castillo"]
        started = time.perf_counter()
        run_id = services.imports.create_run(bank_account_id=account["bank_account_id"],
                                             period_start="2026-08-01", period_end="2026-08-31",
                                             created_by=maya, dataset_seed=20260801)
        services.imports.import_directory(run_id, data_directory, maya)
        services.validation.validate_and_normalize(run_id, maya)
        services.matching.run_rules(run_id, maya, use_scoring=use_scoring)
        return services, run_id, time.perf_counter() - started

    def _pipeline_metrics(self, services: Services, run_id: int, truth: dict, elapsed: float,
                          label: str) -> PipelineMetrics:
        repos = services.repositories
        refs = self._external_ids(services, run_id)
        label_of = lambda key: truth.get((key[0], refs[key]), {})  # noqa: E731
        metrics = PipelineMetrics(label, elapsed)
        snapshot = json.loads(repos.runs.get(run_id)["parameter_snapshot"])["parameters"]
        high, low = snapshot["confidence_high_band"], snapshot["confidence_low_band"]
        threshold = snapshot["auto_accept_threshold"]

        found_groups: set[str] = set()
        by_source: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        bands: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        points: list[CalibrationPoint] = []
        settled_items: set[Key] = set()

        recommendations = repos.recommendations.for_run(run_id)
        for rec in recommendations:
            metrics.queues[rec["category_code"]] = metrics.queues.get(rec["category_code"], 0) + 1
            if rec["exception_code"] == "EXC-08":
                metrics.capped_searches += 1

            truth_risk = label_of((rec["subject_item_type"], rec["subject_item_id"])).get("expected_risk")
            if truth_risk:
                metrics.risk_total += 1
                metrics.risk_agreed += truth_risk == rec["risk_level"]

            if rec["kind"] == "exception":
                expected = label_of((rec["subject_item_type"], rec["subject_item_id"])).get("exception_code", "")
                if expected:
                    metrics.exception_total += 1
                    metrics.exception_agreed += expected == rec["exception_code"]
                    pair = (expected, rec["exception_code"])
                    metrics.exception_confusion[pair] = metrics.exception_confusion.get(pair, 0) + 1
                continue

            candidates = repos.recommendations.candidates(rec["recommendation_id"])
            leading = candidates[0]
            members = self._members(services, leading["candidate_id"])
            groups = {label_of(key).get("true_match_group", "") for key in members}
            correct = len(groups) == 1 and "" not in groups
            metrics.proposed += 1
            metrics.correct += correct
            by_source[rec["source"]][0] += correct
            by_source[rec["source"]][1] += 1
            if correct:
                found_groups |= groups
            else:
                metrics.wrong_examples.append({"recommendation_id": rec["recommendation_id"],
                                               "records": [refs[key] for key in members],
                                               "confidence": rec["confidence"], "source": rec["source"]})

            if rec["confidence"] is not None:
                band = "High" if rec["confidence"] >= high else "Medium" if rec["confidence"] >= low else "Low"
                bands[band][0] += correct
                bands[band][1] += 1
                # The raw leading score, not the stored confidence: assessing a calibrated
                # value through the calibrator again would measure the wrong thing.
                points.append(CalibrationPoint(leading["score"], correct))

            eligible = rec["risk_level"] == "low" and (
                rec["category_code"] == "CAT-01"
                or (rec["confidence"] is not None and rec["confidence"] >= threshold))
            if eligible:
                metrics.hypothetical_accepted += 1
                metrics.hypothetical_wrong += not correct
                settled_items.update(members)

        metrics.found_groups = len(found_groups)
        scenario_groups: dict[str, set[str]] = defaultdict(set)
        for row in truth.values():
            if row["true_match_group"]:
                scenario_groups[row["scenario_code"]].add(row["true_match_group"])
        metrics.true_groups = sum(len(groups) for groups in scenario_groups.values())
        for scenario, groups in sorted(scenario_groups.items()):
            metrics.by_scenario[scenario] = (len(groups & found_groups), len(groups))
            missed = sorted(groups - found_groups)
            if missed:
                metrics.missed[scenario] = missed
        metrics.by_source = {source: tuple(counts) for source, counts in by_source.items()}
        metrics.bands = {band: tuple(bands[band]) for band in BANDS if band in bands}
        if points and self.calibrator.is_fitted:
            metrics.calibration = assess(self.calibrator, points)

        metrics.items_total = sum(1 for key in refs if repos.items.get(*key)["status"] != "excluded")
        metrics.items_hypothetically_settled = len(settled_items)
        metrics.provisional_difference_cents = services.period.statement(run_id).unresolved_difference_cents
        return metrics

    # -- review -------------------------------------------------------------
    def measure_review(self, services: Services, run_id: int, truth: dict) -> ReviewMetrics:
        repos = services.repositories
        run = repos.runs.get(run_id)
        refs = self._external_ids(services, run_id)
        metrics = ReviewMetrics(run_id, run["status"])
        snapshot = json.loads(run["parameter_snapshot"])["parameters"]
        high, low = snapshot["confidence_high_band"], snapshot["confidence_low_band"]
        names = {u["user_id"]: u["full_name"] for u in repos.users.active()}

        recommendations = {rec["recommendation_id"]: rec for rec in repos.recommendations.for_run(run_id)}
        metrics.recommendations = len(recommendations)
        history: dict[int, list] = defaultdict(list)
        for decision in repos.decisions.for_run(run_id):
            history[decision["recommendation_id"]].append(decision)

        batches = set()
        for rec_id, decisions in history.items():
            rec = recommendations[rec_id]
            final = decisions[-1]
            metrics.decided += final["decision"] != "escalate"
            for decision in decisions:
                metrics.decisions[decision["decision"]] = metrics.decisions.get(decision["decision"], 0) + 1
                if decision["review_batch_id"]:
                    batches.add(decision["review_batch_id"])
                    metrics.batch_decisions += 1
                elif decision["opened_at"]:
                    seconds = self._seconds(decision["opened_at"], decision["decided_at"])
                    metrics.timing.setdefault(names.get(decision["decided_by"], str(decision["decided_by"])),
                                              []).append(seconds)
            if rec["confidence"] is not None:
                band = "High" if rec["confidence"] >= high else "Medium" if rec["confidence"] >= low else "Low"
                metrics.by_band.setdefault(band, Counter())[final["decision"]] += 1

            # Did the people approve the right thing? Checked against ground truth here only.
            # Two cases: a match approved or modified, and an exception paired by hand
            # (a modify with a chosen candidate). Approving an exception is not a pairing.
            approved_match = final["decision"] in ("approve", "modify") and rec["kind"] == "match"
            hand_paired = final["decision"] == "modify" and bool(final["chosen_candidate_id"])
            if approved_match or hand_paired:
                candidate_id = final["chosen_candidate_id"] or repos.recommendations.candidates(rec_id)[0]["candidate_id"]
                members = self._members(services, candidate_id)
                groups = {truth.get((key[0], refs[key]), {}).get("true_match_group", "") for key in members}
                metrics.approvals_checked += 1
                metrics.approvals_correct += len(groups) == 1 and "" not in groups
                if final["decision"] == "modify" and rec["kind"] == "exception":
                    metrics.manual_pairings += 1
        metrics.batches = len(batches)

        for adjustment in repos.adjustments.for_run(run_id):
            metrics.adjustments[adjustment["status"]] += 1

        for event in services.audit.events(run_id):
            metrics.audit_total += 1
            missing = self._missing_fields(event)
            if missing:
                for name in missing:
                    metrics.audit_incomplete.setdefault(event["event_type"], Counter())[name] += 1
            else:
                metrics.audit_complete += 1
            if event["event_type"] == "BLOCKED_ATTEMPT":
                metrics.blocked_attempts[event["rule_name"]] += 1
        metrics.chain_intact = services.audit.verify(run_id).intact

        signoff = repos.periods.latest_signoff(run_id)
        metrics.signed_difference_cents = signoff["unresolved_difference_cents"] if signoff else None
        metrics.current_difference_cents = services.period.statement(run_id).unresolved_difference_cents
        return metrics

    @staticmethod
    def _missing_fields(event) -> list[str]:
        """FR-AUD-02 fields that apply to this event and are empty (FR-EVL-07)."""
        missing = []
        if event["actor_type"] == "human" and event["actor_user_id"] is None:
            missing.append("actor_user_id")
        for name in APPLICABLE_FIELDS.get(event["event_type"], ()):
            if event[name] in (None, "", "[]", "{}"):
                missing.append(name)
        if event["event_type"] == "DECISION":
            if event["rule_name"] is None and event["model_version_id"] is None:
                missing.append("rule_name or model_version_id")
            needs_comment = event["decision"] in COMMENTED_DECISIONS or (
                event["decision"] == "approve" and event["risk_level"] == "high")
            if needs_comment and not (event["comment"] or "").strip():
                missing.append("comment")
        return missing

    # -- the timing protocol (DD-04) ------------------------------------------
    def timing_sample(self, services: Services, run_id: int, people: int = 2,
                      per_person: int = TIMING_PROTOCOL_ITEMS, seed_value: int = 20261006) -> list[list[int]]:
        """Open items for each person, stratified by queue in proportion to its size.

        Seeded, so the same run always yields the same lists. Items are dealt in turn so
        each person's list has the same mix.
        """
        by_queue: dict[str, list[int]] = defaultdict(list)
        for category in ("CAT-01", "CAT-02", "CAT-03", "CAT-04", "CAT-05"):
            by_queue[category] = [row["recommendation_id"] for row in
                                  services.repositories.recommendations.queue(run_id, category)]
        total = sum(len(ids) for ids in by_queue.values())
        wanted = min(people * per_person, total)
        generator = random.Random(seed_value)
        chosen: list[int] = []
        for category, ids in sorted(by_queue.items()):
            share = round(wanted * len(ids) / total) if total else 0
            chosen += generator.sample(ids, min(share, len(ids)))
        generator.shuffle(chosen)
        return [sorted(chosen[person::people]) for person in range(people)]

    # -- helpers ------------------------------------------------------------
    def _true_difference(self, data_directory: Path, truth: dict) -> int:
        """The difference a perfect review would sign: ground-truth dispositions, real balances."""
        with tempfile.TemporaryDirectory() as directory:
            services, run_id, _ = self._fresh_run(Path(directory), data_directory, use_scoring=False)
            try:
                lines = []
                for item_type in ITEM_TYPES:
                    for row in services.repositories.items.list(run_id, item_type):
                        label = truth.get((item_type, row[EXTERNAL[item_type]]))
                        if row["status"] == "excluded" or not label or not label["exception_code"]:
                            continue
                        lines.append(StatementLine(row[EXTERNAL[item_type]],
                                                   statement_section(label["exception_code"]) or "unresolved",
                                                   row["amount_cents"], Item.from_row(item_type, row).cash_effect,
                                                   label["exception_code"], "", "", ""))
                system = services.period.statement(run_id)
                return build_statement(system.bank_ending_balance_cents, system.book_ending_balance_cents,
                                       lines).unresolved_difference_cents
            finally:
                services.database.close()

    @staticmethod
    def _external_ids(services: Services, run_id: int) -> dict[Key, str]:
        return {(item_type, row[ID_COLUMN[item_type]]): row[EXTERNAL[item_type]]
                for item_type in ITEM_TYPES for row in services.repositories.items.list(run_id, item_type)}

    @staticmethod
    def _members(services: Services, candidate_id: int) -> list[Key]:
        keys = []
        for row in services.repositories.recommendations.candidate_members(candidate_id):
            for item_type in ITEM_TYPES:
                if row[ID_COLUMN[item_type]] is not None:
                    keys.append((item_type, int(row[ID_COLUMN[item_type]])))
        return keys

    @staticmethod
    def _seconds(opened_at: str, decided_at: str) -> int:
        parse = lambda value: datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        return max(0, int((parse(decided_at) - parse(opened_at)).total_seconds()))


# ---------------------------------------------------------------------------
# The written evaluation (reports/evaluation.md)
# ---------------------------------------------------------------------------

def render_markdown(evaluation: Evaluation) -> str:
    rules, hybrid, review = evaluation.rules_only, evaluation.hybrid, evaluation.review
    lines: list[str] = []
    add = lines.append

    def figure(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.4f}"

    add("# Evaluation Results")
    add("")
    add(f"Generated by `python -m app.cli evaluate` from `{evaluation.data_directory.as_posix()}`. "
        f"Calibration artefact: `{evaluation.calibration_label}`. Every rate shows the counts it was "
        "computed from. Ground truth is read by the evaluator only.")
    add("")
    add("## 1. Matching (FR-EVL-01, FR-EVL-09)")
    add("")
    add("| Measure | Rules only | Rules plus scoring |")
    add("|---|---|---|")
    add(f"| Matches proposed | {rules.proposed} | {hybrid.proposed} |")
    add(f"| Precision | {shown(rules.correct, rules.proposed)} | {shown(hybrid.correct, hybrid.proposed)} |")
    add(f"| Recall | {shown(rules.found_groups, rules.true_groups)} | {shown(hybrid.found_groups, hybrid.true_groups)} |")
    add(f"| F1 | {figure(rules.f1)} | {figure(hybrid.f1)} |")
    add(f"| Exception classification accuracy (FR-EVL-06) | {shown(rules.exception_agreed, rules.exception_total)} | "
        f"{shown(hybrid.exception_agreed, hybrid.exception_total)} |")
    add(f"| Risk level agreement | {shown(rules.risk_agreed, rules.risk_total)} | {shown(hybrid.risk_agreed, hybrid.risk_total)} |")
    add(f"| Recommendations for review | {sum(rules.queues.values())} | {sum(hybrid.queues.values())} |")
    add(f"| Group searches stopped at the cap (EXC-08) | {rules.capped_searches} | {hybrid.capped_searches} |")
    add(f"| Elapsed, import to routed queues | {rules.elapsed_seconds:.2f} s | {hybrid.elapsed_seconds:.2f} s |")
    add("")
    add("### Recall by scenario")
    add("")
    add("| Scenario | Rules only | Rules plus scoring |")
    add("|---|---|---|")
    for scenario in sorted(set(rules.by_scenario) | set(hybrid.by_scenario)):
        r, h = rules.by_scenario.get(scenario, (0, 0)), hybrid.by_scenario.get(scenario, (0, 0))
        add(f"| {scenario} | {shown(*r)} | {shown(*h)} |")
    add("")
    add("## 2. Confidence (FR-EVL-08)")
    add("")
    add("| Band | Proposed | Correct | Observed precision |")
    add("|---|---|---|---|")
    for band in BANDS:
        correct, proposed = hybrid.bands.get(band, (0, 0))
        add(f"| {band} | {proposed} | {correct} | {shown(correct, proposed)} |")
    add("")
    if hybrid.calibration:
        quality = hybrid.calibration
        points = sum(band["n"] for band in quality.bands)
        add(f"Calibration on the evaluation data: expected calibration error {quality.expected_calibration_error:.4f}, "
            f"largest band error {quality.maximum_error:.4f}, Brier score {quality.brier_score:.4f}, n={points}.")
        add("")
        add("| Stated confidence | n | Stated | Observed | Error |")
        add("|---|---|---|---|---|")
        for band in quality.bands:
            add(f"| {band['band']} | {band['n']} | {band['stated']:.4f} | {band['observed']:.4f} | {band['error']:.4f} |")
        add("")
    else:
        add("No fitted calibration artefact: confidence is the raw score and is labelled uncalibrated.")
        add("")
    add("## 3. Automation that was not used (FR-EVL-02, FR-EVL-03)")
    add("")
    add("| Measure | Value |")
    add("|---|---|")
    add("| Review rate | **100% by design**: every item needs a recorded human decision (DD-03) |")
    add(f"| Hypothetical auto-accepted matches under BR-19 | {hybrid.hypothetical_accepted} |")
    add(f"| Hypothetical false automatic match rate (DD-01) | {shown(hybrid.hypothetical_wrong, hybrid.hypothetical_accepted)} |")
    add(f"| Hypothetical review rate if BR-19 were executed | "
        f"{shown(hybrid.items_total - hybrid.items_hypothetically_settled, hybrid.items_total)} of items |")
    add("")
    add("Nothing is ever accepted automatically. These figures say what an auto-accept policy *would* have done, "
        "so its risk can be judged before anyone considers using it.")
    add("")
    add("## 4. Reconciliation statement")
    add("")
    add("| Point | Unresolved difference |")
    add("|---|---|")
    add(f"| Before review (system proposals only) | {money(hybrid.provisional_difference_cents)} |")
    if review is not None:
        add(f"| Reviewed run {review.run_id} now | {money(review.current_difference_cents)} |")
        add(f"| Reviewed run {review.run_id} at sign-off | "
            f"{'not signed' if review.signed_difference_cents is None else money(review.signed_difference_cents)} |")
    add(f"| Correct, from ground truth | {money(evaluation.true_difference_cents)} |")
    add("")
    add("## 5. Review (FR-EVL-04, FR-EVL-05, FR-EVL-07)")
    add("")
    if review is None:
        add("No reviewed run was given. Run `python -m app.cli evaluate --run N` after a review.")
    else:
        decisions = review.decisions
        total = sum(decisions.values())
        add(f"Run {review.run_id}, status {review.status}: {review.decided} of {review.recommendations} "
            f"recommendations decided.")
        add("")
        add("| Measure | Value |")
        add("|---|---|")
        for name in ("approve", "modify", "reject", "escalate", "unresolved"):
            add(f"| {name.capitalize()} rate | {shown(decisions.get(name, 0), total)} |")
        add(f"| Approvals that were correct (ground truth) | {shown(review.approvals_correct, review.approvals_checked)} |")
        add(f"| Pairings made by hand | {review.manual_pairings} |")
        add(f"| Batches | {review.batches}, covering {review.batch_decisions} decisions |")
        add(f"| Adjustments | {', '.join(f'{k} {v}' for k, v in sorted(review.adjustments.items())) or 'none'} |")
        add(f"| Refused attempts | {sum(review.blocked_attempts.values())}"
            f"{' (' + ', '.join(f'{k} {v}' for k, v in sorted(review.blocked_attempts.items())) + ')' if review.blocked_attempts else ''} |")
        add(f"| Audit completeness | {shown(review.audit_complete, review.audit_total)} |")
        add(f"| Audit chain | {'intact' if review.chain_intact else 'BROKEN'} |")
        add("")
        add("### Time per item (DD-04)")
        add("")
        if not review.timing:
            add("Not measured: no decision in this run was made after opening its detail view.")
        else:
            add("| Person | Timed decisions | Median seconds | Mean seconds | Protocol |")
            add("|---|---|---|---|---|")
            for person, seconds in sorted(review.timing.items()):
                status = "complete" if len(seconds) >= TIMING_PROTOCOL_ITEMS else f"below {TIMING_PROTOCOL_ITEMS} items"
                add(f"| {person} | {len(seconds)} | {median(seconds):.0f} | {sum(seconds) / len(seconds):.0f} | {status} |")
            add("")
            add("Batch approvals are not timed: they are one action over many items.")
        if review.audit_incomplete:
            add("")
            add("### Audit fields found empty")
            add("")
            add("| Event type | Field | Events |")
            add("|---|---|---|")
            for event_type, fields_missing in sorted(review.audit_incomplete.items()):
                for name, count in sorted(fields_missing.items()):
                    add(f"| {event_type} | {name} | {count} |")
    add("")
    add("## 6. Errors and misses")
    add("")
    add(f"Incorrect proposals: {hybrid.proposed - hybrid.correct}.")
    for example in hybrid.wrong_examples[:10]:
        add(f"- Recommendation {example['recommendation_id']} ({example['source']}, confidence "
            f"{example['confidence']}): {', '.join(example['records'])}")
    add("")
    add("| Scenario | True groups missed | Examples |")
    add("|---|---|---|")
    for scenario, groups in sorted(hybrid.missed.items()):
        add(f"| {scenario} | {len(groups)} | {', '.join(groups[:4])}{' …' if len(groups) > 4 else ''} |")
    add("")
    add("### Exception classification, where it disagreed")
    add("")
    add("| Correct category | Proposed category | Items |")
    add("|---|---|---|")
    for (expected, proposed), count in sorted(hybrid.exception_confusion.items()):
        if expected != proposed:
            add(f"| {expected} | {proposed} | {count} |")
    add("")
    return "\n".join(lines)
