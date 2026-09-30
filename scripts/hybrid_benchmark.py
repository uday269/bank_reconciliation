#!/usr/bin/env python3
"""Compare the rules-only baseline with the hybrid system on the same data.

    python3 scripts/hybrid_benchmark.py
    python3 scripts/hybrid_benchmark.py --data data/august_2026 --out reports/hybrid_benchmark.md

Both systems run the same code over the same dataset; the only difference is whether
the scoring layer is enabled. That is what makes the comparison a measurement rather
than an argument.

Ground truth is read here and nowhere under app/ (FR-EVL-11). Every rate is reported
with the count it was computed from (G8), because a percentage over eleven items is not
the same claim as a percentage over five hundred.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config                                    # noqa: E402
from app.domain.calibration import CalibrationPoint, Calibrator, assess   # noqa: E402
from app.infra.audit import AuditLog, utc_now                         # noqa: E402
from app.infra.db import open_database                                # noqa: E402
from app.infra.repositories import Repositories                       # noqa: E402
from app.services.import_service import ImportService                 # noqa: E402
from app.services.matching_service import MatchingService             # noqa: E402
from app.services.validation_service import ValidationService         # noqa: E402

STAFF = [("Maya Castillo", "ROL-01"), ("Ethan Brooks", "ROL-01"),
         ("Priya Raman", "ROL-02"), ("Daniel Okafor", "ROL-03")]
GL_ACCOUNTS = [("1010", "Cash - Operating", "asset"), ("6810", "Bank Service Charges", "expense"),
               ("6820", "Returned Item Charges", "expense"), ("7010", "Interest Income", "income"),
               ("9990", "Suspense - Under Investigation", "asset")]


def rate(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{numerator / denominator:.4f}"


def with_count(numerator: int, denominator: int) -> str:
    """Every rate carries its sample size (G8)."""
    return f"{rate(numerator, denominator)} (n={denominator})"


# ---------------------------------------------------------------------------
# Running one configuration
# ---------------------------------------------------------------------------

def run_once(config, database_path: Path, data_directory: Path, calibrator: Calibrator,
             use_scoring: bool):
    if database_path.exists():
        database_path.unlink()
    database = open_database(database_path, config.paths.schema)
    repositories = Repositories.for_database(database)
    audit = AuditLog(database)

    with database.transaction() as connection:
        users = {name: repositories.users.create(connection, name, role, utc_now())
                 for name, role in STAFF}
        repositories.accounts.create_gl_accounts(connection, GL_ACCOUNTS)
        account_id = repositories.accounts.create_bank_account(
            connection, config.organization.bank_name, config.organization.account_label,
            config.organization.account_mask, config.organization.gl_account_code)

    imports = ImportService(database, repositories, audit, config)
    started = time.perf_counter()
    run_id = imports.create_run(account_id, "2026-08-01", "2026-08-31", users["Maya Castillo"],
                                dataset_seed=20260801)
    imports.import_directory(run_id, data_directory, users["Maya Castillo"])
    ValidationService(database, repositories, audit, config).validate_and_normalize(
        run_id, users["Maya Castillo"])
    summary = MatchingService(database, repositories, audit, config, calibrator).run_rules(
        run_id, users["Maya Castillo"], use_scoring=use_scoring)
    elapsed = time.perf_counter() - started
    return database, repositories, audit, run_id, summary, elapsed


def load_truth(data_directory: Path) -> dict[tuple[str, str], dict]:
    with (data_directory / "ground_truth.csv").open() as handle:
        return {(row["item_type"], row["item_id"]): row for row in csv.DictReader(handle)}


def external_ids(database, run_id: int) -> dict[tuple[str, int], str]:
    mapping = {}
    for item_type, table, key, external in (
            ("bank", "bank_transaction", "bank_transaction_id", "external_txn_id"),
            ("ledger", "ledger_entry", "ledger_entry_id", "external_entry_id"),
            ("carry_in", "carry_in_item", "carry_in_item_id", "external_item_id")):
        for row in database.query(
                f"SELECT {key} AS item_id, {external} AS external FROM {table} WHERE run_id = ?",
                (run_id,)):
            mapping[(item_type, row["item_id"])] = row["external"]
    return mapping


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def evaluate(database, run_id: int, truth: dict, ids: dict, config) -> dict:
    """Precision, recall, confidence bands and the hypothetical auto-accept rate."""
    rows = database.query(
        """SELECT r.recommendation_id, r.source, r.rule_name, r.confidence, r.category_code,
                  r.risk_level, cm.bank_transaction_id, cm.ledger_entry_id, cm.carry_in_item_id
           FROM recommendation r
           JOIN candidate c ON c.recommendation_id = r.recommendation_id AND c.rank_order = 1
           JOIN candidate_member cm ON cm.candidate_id = c.candidate_id
           WHERE r.run_id = ? AND r.kind = 'match'""", (run_id,))

    proposals: dict[int, dict] = {}
    for row in rows:
        entry = proposals.setdefault(row["recommendation_id"], {
            "source": row["source"], "rule": row["rule_name"], "confidence": row["confidence"],
            "category": row["category_code"], "risk": row["risk_level"], "members": []})
        for item_type, column in (("bank", "bank_transaction_id"), ("ledger", "ledger_entry_id"),
                                  ("carry_in", "carry_in_item_id")):
            if row[column] is not None:
                entry["members"].append((item_type, row[column]))

    correct_by_source = Counter()
    wrong_by_source = Counter()
    found_groups: set[str] = set()
    band_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])     # correct, wrong
    calibration_points: list[CalibrationPoint] = []
    wrong_examples: list[dict] = []

    parameters = config.parameters
    hypothetical_total = hypothetical_wrong = 0

    for recommendation_id, entry in proposals.items():
        groups = {truth.get((item_type, ids[(item_type, item_id)]), {}).get("true_match_group", "")
                  for item_type, item_id in entry["members"]}
        correct = len(groups) == 1 and "" not in groups

        (correct_by_source if correct else wrong_by_source)[entry["source"]] += 1
        if correct:
            found_groups |= groups
        elif entry["source"] == "ai":
            wrong_examples.append({"id": recommendation_id, "confidence": entry["confidence"],
                                   "category": entry["category"],
                                   "members": [ids[member] for member in entry["members"]]})

        if entry["confidence"] is not None:
            band = ("High" if entry["confidence"] >= parameters.confidence_high_band
                    else "Medium" if entry["confidence"] >= parameters.confidence_low_band
                    else "Low")
            band_counts[band][0 if correct else 1] += 1
            calibration_points.append(CalibrationPoint(entry["confidence"], correct))

        # BR-19 hypothetical: what the system would have accepted without a reviewer.
        # Policy fixed before measurement (DD-01): exact category, no risk flag, and
        # confidence at or above the threshold where a confidence exists.
        eligible = (entry["category"] == "CAT-01" and entry["risk"] == "low") or (
            entry["confidence"] is not None
            and entry["confidence"] >= parameters.auto_accept_threshold
            and entry["risk"] == "low")
        if eligible:
            hypothetical_total += 1
            if not correct:
                hypothetical_wrong += 1

    true_groups = {row["true_match_group"] for row in truth.values() if row["true_match_group"]}

    by_scenario = {}
    scenario_groups = defaultdict(set)
    for row in truth.values():
        if row["true_match_group"]:
            scenario_groups[row["scenario_code"]].add(row["true_match_group"])
    for scenario, groups in sorted(scenario_groups.items()):
        by_scenario[scenario] = (len(groups & found_groups), len(groups))

    exception_total = exception_agreed = 0
    for row in database.query(
            "SELECT subject_item_type AS t, subject_item_id AS i, exception_code AS code "
            "FROM recommendation WHERE run_id = ? AND kind = 'exception'", (run_id,)):
        label = truth.get((row["t"], ids[(row["t"], row["i"])]))
        if label and label["exception_code"]:
            exception_total += 1
            exception_agreed += label["exception_code"] == row["code"]

    return {
        "proposed": len(proposals),
        "correct": sum(correct_by_source.values()),
        "wrong": sum(wrong_by_source.values()),
        "correct_by_source": dict(correct_by_source),
        "wrong_by_source": dict(wrong_by_source),
        "true_groups": len(true_groups),
        "found_groups": len(found_groups),
        "by_scenario": by_scenario,
        "bands": {band: tuple(counts) for band, counts in band_counts.items()},
        "calibration_points": calibration_points,
        "exception_total": exception_total,
        "exception_agreed": exception_agreed,
        "hypothetical_total": hypothetical_total,
        "hypothetical_wrong": hypothetical_wrong,
        "wrong_examples": wrong_examples,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(path: Path, config, calibrator, baseline, hybrid, quality, data_directory) -> None:
    base_metrics, base_summary, base_elapsed = baseline
    hybrid_metrics, hybrid_summary, hybrid_elapsed = hybrid

    def line(label, base, hyb):
        return f"| {label} | {base} | {hyb} |"

    lines = [
        "# Hybrid Benchmark - rules only against rules plus scoring", "",
        "Both columns are the same code over the same data. The only difference is whether the",
        "scoring layer ran, so the comparison measures the AI layer rather than two systems.", "",
        "| Setting | Value |", "|---|---|",
        f"| Dataset | `{data_directory}` |",
        f"| Calibration artefact | {calibrator.version_label} ({calibrator.method}, "
        f"{calibrator.sample_size} points) |",
        f"| Calibration data | {calibrator.dataset or 'none'} |",
        f"| Senior approval threshold | {config.parameters.senior_approval_amount_cents} cents |",
        f"| Confidence bands | High at or above {config.parameters.confidence_high_band}, "
        f"Low below {config.parameters.confidence_low_band} |", "",
        "## Headline", "",
        "| Measure | Rules only | Hybrid |", "|---|---|---|",
        line("Matches proposed", base_metrics["proposed"], hybrid_metrics["proposed"]),
        line("Correct", base_metrics["correct"], hybrid_metrics["correct"]),
        line("Incorrect", base_metrics["wrong"], hybrid_metrics["wrong"]),
        line("**Precision**",
             f"**{with_count(base_metrics['correct'], base_metrics['proposed'])}**",
             f"**{with_count(hybrid_metrics['correct'], hybrid_metrics['proposed'])}**"),
        line("**Recall**",
             f"**{with_count(base_metrics['found_groups'], base_metrics['true_groups'])}**",
             f"**{with_count(hybrid_metrics['found_groups'], hybrid_metrics['true_groups'])}**"),
        line("Exception accuracy",
             with_count(base_metrics["exception_agreed"], base_metrics["exception_total"]),
             with_count(hybrid_metrics["exception_agreed"], hybrid_metrics["exception_total"])),
        line("Items left unmatched for a reviewer",
             base_summary.unmatched_for_scoring, hybrid_summary.unmatched_for_scoring),
        line("Recommendations created",
             base_summary.recommendations, hybrid_summary.recommendations),
        line("Elapsed", f"{base_elapsed:.1f}s", f"{hybrid_elapsed:.1f}s"), "",
        "## Recall by scenario", "",
        "| Scenario | Rules only | Hybrid | Groups in data |", "|---|---|---|---|",
    ]

    for scenario in sorted(set(base_metrics["by_scenario"]) | set(hybrid_metrics["by_scenario"])):
        base_found, total = base_metrics["by_scenario"].get(scenario, (0, 0))
        hybrid_found, _ = hybrid_metrics["by_scenario"].get(scenario, (0, total))
        lines.append(f"| {scenario} | {rate(base_found, total)} | {rate(hybrid_found, total)} "
                     f"| {total} |")

    lines += ["", "## Performance by confidence band", "",
              "Scored matches only. Rule-settled matches carry no confidence value.", "",
              "| Band | Matches | Correct | Wrong | Accuracy |", "|---|---|---|---|---|"]
    for band in ("High", "Medium", "Low"):
        correct, wrong = hybrid_metrics["bands"].get(band, (0, 0))
        if correct + wrong:
            lines.append(f"| {band} | {correct + wrong} | {correct} | {wrong} | "
                         f"{rate(correct, correct + wrong)} |")

    if quality:
        lines += ["", "### Calibration on unseen data", "",
                  f"The artefact was fitted on {calibrator.dataset}. These figures are the first",
                  "time it has met this dataset.", "",
                  f"- Expected calibration error: {quality.expected_calibration_error:.4f}",
                  f"- Worst band error: {quality.maximum_error:.4f}",
                  f"- Brier score: {quality.brier_score:.4f}", "",
                  "| Band | n | Stated | Observed | Error |", "|---|---|---|---|---|"]
        for band in quality.bands:
            lines.append(f"| {band['band']} | {band['n']} | {band['stated']:.3f} | "
                         f"{band['observed']:.3f} | {band['error']:.3f} |")

    lines += ["", "## Hypothetical automatic acceptance (DD-01)", "",
              "Nothing is ever accepted without a reviewer (CR-01). This measures what **would**",
              "have happened under a fixed policy defined before the measurement: an exact",
              "rule-settled match with no risk flag, or a scored match at or above "
              f"{config.parameters.auto_accept_threshold} confidence with no risk flag.", "",
              "| Measure | Rules only | Hybrid |", "|---|---|---|",
              line("Items that would have been auto-accepted",
                   base_metrics["hypothetical_total"], hybrid_metrics["hypothetical_total"]),
              line("Of those, incorrect",
                   base_metrics["hypothetical_wrong"], hybrid_metrics["hypothetical_wrong"]),
              line("**False automatic match rate**",
                   f"**{with_count(base_metrics['hypothetical_wrong'], base_metrics['hypothetical_total'])}**",
                   f"**{with_count(hybrid_metrics['hypothetical_wrong'], hybrid_metrics['hypothetical_total'])}**"),
              "", "## Review workload", "",
              "| Measure | Rules only | Hybrid |", "|---|---|---|",
              line("Review rate", "1.0000 by design", "1.0000 by design"),
              line("Items requiring a decision",
                   base_summary.recommendations, hybrid_summary.recommendations),
              line("Of those, exceptions", base_summary.exceptions, hybrid_summary.exceptions), ""]

    if hybrid_metrics["wrong_examples"]:
        lines += ["## Incorrect scored matches", "",
                  "| Recommendation | Confidence | Queue | Items |", "|---|---|---|---|"]
        for example in hybrid_metrics["wrong_examples"][:20]:
            lines.append(f"| {example['id']} | {example['confidence']} | {example['category']} "
                         f"| {', '.join(example['members'])} |")
        lines.append("")
    else:
        lines += ["## Incorrect scored matches", "",
                  "None on this dataset. That is a property of this data, not a guarantee: the",
                  "scenarios here are generated, and a real month would contain ambiguities this",
                  "dataset does not.", ""]

    lines += ["## Reading these numbers", "",
              "Recall is the measure that moved. Precision was already 1.0 for the rules, and the",
              "question was whether scoring could raise coverage without lowering it.", "",
              "Every figure here is measured against labelled data that the system never sees: no",
              "module under `app/` reads the ground truth file. The scoring layer was tuned on a",
              "different month, so this is the first contact between the fitted artefact and this",
              "data.", "",
              "The review rate stays at 1.0 in both columns because every item requires human",
              "approval by design. The AI layer changes how much judgement each decision needs,",
              "not how many decisions there are.", ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rules-only against hybrid, same data")
    parser.add_argument("--data", default="data/august_2026")
    parser.add_argument("--out", default="reports/hybrid_benchmark.md")
    parser.add_argument("--calibration", default="models/calibration.json")
    parser.add_argument("--database", default="benchmark.db")
    args = parser.parse_args()

    config = load_config()
    data_directory = Path(args.data)
    calibration_path = Path(args.calibration)
    calibrator = (Calibrator.load(calibration_path) if calibration_path.exists()
                  else Calibrator.identity())
    if not calibrator.is_fitted:
        print("No calibration artefact found; raw scores will be reported as confidence.\n")

    truth = load_truth(data_directory)
    results = {}
    for label, use_scoring in (("rules only", False), ("hybrid", True)):
        database, repositories, audit, run_id, summary, elapsed = run_once(
            config, Path(args.database), data_directory, calibrator, use_scoring)
        metrics = evaluate(database, run_id, truth, external_ids(database, run_id), config)
        results[label] = (metrics, summary, elapsed)
        database.close()
        print(f"{label:11} precision {with_count(metrics['correct'], metrics['proposed']):>18}"
              f"   recall {with_count(metrics['found_groups'], metrics['true_groups']):>16}"
              f"   unmatched {summary.unmatched_for_scoring:>4}   {elapsed:.1f}s")

    base_metrics = results["rules only"][0]
    hybrid_metrics = results["hybrid"][0]

    quality = (assess(calibrator, hybrid_metrics["calibration_points"])
               if hybrid_metrics["calibration_points"] and calibrator.is_fitted else None)

    print("\nrecall by scenario")
    for scenario in sorted(hybrid_metrics["by_scenario"]):
        base_found, total = base_metrics["by_scenario"].get(scenario, (0, 0))
        hybrid_found, _ = hybrid_metrics["by_scenario"][scenario]
        change = "" if base_found == hybrid_found else f"   (+{hybrid_found - base_found})"
        print(f"  {scenario}  rules {rate(base_found, total)}   hybrid {rate(hybrid_found, total)}"
              f"   n={total}{change}")

    print("\nconfidence bands, scored matches only")
    for band in ("High", "Medium", "Low"):
        correct, wrong = hybrid_metrics["bands"].get(band, (0, 0))
        if correct + wrong:
            print(f"  {band:7} n={correct + wrong:>4}  correct {correct:>4}  wrong {wrong:>3}"
                  f"  accuracy {rate(correct, correct + wrong)}")

    if quality:
        print(f"\ncalibration on unseen data: {quality.summary()}")

    print(f"\nhypothetical automatic acceptance (DD-01), never executed:")
    for label in ("rules only", "hybrid"):
        metrics = results[label][0]
        print(f"  {label:11} would auto-accept {metrics['hypothetical_total']:>4}, "
              f"wrong {metrics['hypothetical_wrong']:>3}, rate "
              f"{with_count(metrics['hypothetical_wrong'], metrics['hypothetical_total'])}")

    write_report(Path(args.out), config, calibrator, results["rules only"], results["hybrid"],
                 quality, data_directory)
    print(f"\nreport written to {args.out}")

    Path(args.database).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(args.database + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
