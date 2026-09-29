#!/usr/bin/env python3
"""Measure the deterministic baseline against the labelled dataset.

Produces the rules-only figures that Stage 9 compares the AI layer with. Ground truth is
read here and nowhere else: no module under app/ opens this file (FR-EVL-11).

Usage
    python3 scripts/baseline_benchmark.py
    python3 scripts/baseline_benchmark.py --data data/august_2026 --out reports/baseline_benchmark.md
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


def percentage(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{numerator / denominator:.4f}"


def run_pipeline(config, database_path: Path, data_directory: Path):
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
    matching = MatchingService(database, repositories, audit, config).run_rules(
        run_id, users["Maya Castillo"])
    return database, repositories, audit, run_id, matching, time.perf_counter() - started


def load_truth(data_directory: Path) -> dict:
    with (data_directory / "ground_truth.csv").open() as handle:
        return {(row["item_type"], row["item_id"]): row for row in csv.DictReader(handle)}


def external_ids(database, run_id: int) -> dict:
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


def evaluate(database, run_id: int, truth: dict, ids: dict) -> dict:
    rows = database.query(
        """SELECT r.recommendation_id, r.rule_name, cm.bank_transaction_id,
                  cm.ledger_entry_id, cm.carry_in_item_id
           FROM recommendation r
           JOIN candidate c ON c.recommendation_id = r.recommendation_id
           JOIN candidate_member cm ON cm.candidate_id = c.candidate_id
           WHERE r.run_id = ? AND r.kind = 'match'""", (run_id,))

    proposed = defaultdict(list)
    rules = {}
    for row in rows:
        rules[row["recommendation_id"]] = row["rule_name"]
        for item_type, column in (("bank", "bank_transaction_id"), ("ledger", "ledger_entry_id"),
                                  ("carry_in", "carry_in_item_id")):
            if row[column] is not None:
                proposed[row["recommendation_id"]].append((item_type, row[column]))

    correct = incorrect = 0
    found_groups: set[str] = set()
    by_rule = Counter()
    for recommendation_id, members in proposed.items():
        groups = {truth.get((item_type, ids[(item_type, item_id)]), {}).get("true_match_group", "")
                  for item_type, item_id in members}
        if len(groups) == 1 and "" not in groups:
            correct += 1
            found_groups |= groups
            by_rule[rules[recommendation_id]] += 1
        else:
            incorrect += 1

    true_groups = defaultdict(set)
    for label in truth.values():
        if label["true_match_group"]:
            true_groups[label["true_match_group"]].add(label["scenario_code"])

    recall_by_scenario = {}
    for scenario in sorted({code for codes in true_groups.values() for code in codes}):
        groups = {group for group, codes in true_groups.items() if scenario in codes}
        recall_by_scenario[scenario] = (len(groups & found_groups), len(groups))

    exception_total = exception_agreed = 0
    for row in database.query(
            "SELECT subject_item_type AS t, subject_item_id AS i, exception_code AS code "
            "FROM recommendation WHERE run_id = ? AND kind = 'exception'", (run_id,)):
        label = truth.get((row["t"], ids[(row["t"], row["i"])]))
        if label and label["exception_code"]:
            exception_total += 1
            exception_agreed += label["exception_code"] == row["code"]

    return {"proposed": len(proposed), "correct": correct, "incorrect": incorrect,
            "true_groups": len(true_groups), "found_groups": len(found_groups),
            "recall_by_scenario": recall_by_scenario, "by_rule": dict(by_rule),
            "exception_total": exception_total, "exception_agreed": exception_agreed}


def write_report(path: Path, config, matching, metrics: dict, audit_result, events: int,
                 elapsed: float, data_directory: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Baseline Benchmark - deterministic rules only", "",
        "Produced by `scripts/baseline_benchmark.py`. These are the figures the hybrid system is",
        "compared against in DOC-07. No scoring, no confidence and no model are involved: the",
        "system either proves a match by rule or leaves the item for a reviewer.", "",
        "| Setting | Value |", "|---|---|",
        f"| Dataset | `{data_directory}` |",
        f"| Model version | {config.model.version_label} |",
        f"| Senior approval threshold (PRM-01) | {config.parameters.senior_approval_amount_cents} cents |",
        f"| Elapsed | {elapsed:.1f}s |", "",
        "## Matching", "", "| Measure | Value |", "|---|---|",
        f"| Matches proposed | {metrics['proposed']} |",
        f"| Correct | {metrics['correct']} |",
        f"| Incorrect | {metrics['incorrect']} |",
        f"| **Precision** | **{percentage(metrics['correct'], metrics['proposed'])}** |",
        f"| True match groups in the data | {metrics['true_groups']} |",
        f"| Groups found | {metrics['found_groups']} |",
        f"| **Recall** | **{percentage(metrics['found_groups'], metrics['true_groups'])}** |", "",
        "## Recall by scenario", "",
        "| Scenario | Groups found | Groups in data | Recall |", "|---|---|---|---|",
    ]
    for scenario, (found, total) in sorted(metrics["recall_by_scenario"].items()):
        lines.append(f"| {scenario} | {found} | {total} | {percentage(found, total)} |")

    lines += ["", "## Matches by rule", "", "| Rule | Matches |", "|---|---|"]
    for rule, count in sorted(metrics["by_rule"].items()):
        lines.append(f"| {rule} | {count} |")

    lines += ["", "## Exceptions", "", "| Measure | Value |", "|---|---|",
              f"| Exceptions raised | {matching.exceptions} |",
              f"| Items with a labelled exception code | {metrics['exception_total']} |",
              f"| Baseline agrees with the label | {metrics['exception_agreed']} |",
              f"| **Exception accuracy** | **{percentage(metrics['exception_agreed'], metrics['exception_total'])}** |",
              "", "## Workload", "", "| Measure | Value |", "|---|---|",
              f"| Recommendations created | {matching.recommendations} |",
              f"| Items left unmatched for a reviewer | {matching.unmatched_for_scoring} |",
              "| Review rate | 1.0000 (every item requires approval, by design) |",
              "", "## Queues", "", "| Category | Items |", "|---|---|"]
    for category, count in sorted(matching.by_category.items()):
        lines.append(f"| {category} | {count} |")

    lines += ["", "## Audit", "", "| Measure | Value |", "|---|---|",
              f"| Events written | {events} |",
              f"| Chain | {audit_result.summary()} |", "",
              "## Reading these numbers", "",
              "Precision of 1.0 is expected and is not an achievement: a rule that acts only when it",
              "is certain cannot propose a wrong match. The numbers that matter are recall and the",
              f"count of items left for a person to resolve by hand ({matching.unmatched_for_scoring}).",
              "Closing that gap without losing precision is what the AI layer has to do.", "",
              "Exception accuracy is lower than matching accuracy for the same reason: without",
              "scoring, an unmatched timing difference looks exactly like an outstanding check, so",
              "the baseline files it as one.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Baseline benchmark against labelled data")
    parser.add_argument("--data", default="data/august_2026")
    parser.add_argument("--out", default="reports/baseline_benchmark.md")
    parser.add_argument("--database", default="benchmark.db")
    args = parser.parse_args()

    config = load_config()
    data_directory = Path(args.data)
    database_path = Path(args.database)
    if database_path.exists():
        database_path.unlink()

    database, repositories, audit, run_id, matching, elapsed = run_pipeline(
        config, database_path, data_directory)
    truth = load_truth(data_directory)
    ids = external_ids(database, run_id)
    metrics = evaluate(database, run_id, truth, ids)

    print("Baseline benchmark")
    print(f"  matches proposed    {metrics['proposed']}")
    print(f"  correct             {metrics['correct']}")
    print(f"  incorrect           {metrics['incorrect']}")
    print(f"  precision           {percentage(metrics['correct'], metrics['proposed'])}")
    print(f"  recall              {percentage(metrics['found_groups'], metrics['true_groups'])}"
          f"  ({metrics['found_groups']} of {metrics['true_groups']} groups)")
    print(f"  exception accuracy  {percentage(metrics['exception_agreed'], metrics['exception_total'])}")
    print(f"  left for a reviewer {matching.unmatched_for_scoring}")
    print("\n  recall by scenario")
    for scenario, (found, total) in sorted(metrics["recall_by_scenario"].items()):
        print(f"    {scenario}  {found:>3} of {total:>3}   {percentage(found, total)}")

    verification = audit.verify(run_id)
    events = audit.count(run_id)
    write_report(Path(args.out), config, matching, metrics, verification, events, elapsed,
                 data_directory)
    print(f"\n  audit               {events} events, {'intact' if verification.intact else 'BROKEN'}")
    print(f"  elapsed             {elapsed:.1f}s")
    print(f"  report written      {args.out}")

    database.close()
    database_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
