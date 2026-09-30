#!/usr/bin/env python3
"""Fit the confidence calibrator on the July dataset and write the artefact.

Run once, offline, before an evaluation run:

    python3 scripts/fit_calibration.py
    python3 scripts/fit_calibration.py --data data/july_2026_calibration --out models/calibration.json

What it does
    Imports and validates the calibration dataset, applies the deterministic rules,
    generates and scores candidates for whatever the rules leave, then labels each
    candidate correct or incorrect from that dataset's ground truth and fits a monotonic
    mapping from score to probability.

Why it is a script and not part of a run
    Fitting reads ground truth, and no module under app/ may do that (FR-EVL-11). Runs
    load the artefact this script produces; nothing retrains during a run, and nothing
    learns from reviewer decisions (CR-14).

Why July
    The evaluation and the demonstration use August. Fitting and evaluating on the same
    data overstates accuracy, so the two never overlap (DD-11, ADR-06).
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config                                     # noqa: E402
from app.domain.calibration import CalibrationPoint, Calibrator, assess, fit   # noqa: E402
from app.domain.candidates import Candidate, block, generate_all       # noqa: E402
from app.domain.features import extract                                # noqa: E402
from app.domain.rules import Item, apply_rules                         # noqa: E402
from app.domain.scoring import Weights, rank_all, score_features       # noqa: E402
from app.infra.audit import AuditLog, utc_now                          # noqa: E402
from app.infra.db import open_database                                 # noqa: E402
from app.infra.repositories import Repositories                        # noqa: E402
from app.services.import_service import ImportService                  # noqa: E402
from app.services.validation_service import ValidationService          # noqa: E402

STAFF = [("Maya Castillo", "ROL-01"), ("Priya Raman", "ROL-02"), ("Daniel Okafor", "ROL-03")]
GL_ACCOUNTS = [("1010", "Cash - Operating", "asset"), ("6810", "Bank Service Charges", "expense"),
               ("6820", "Returned Item Charges", "expense"), ("7010", "Interest Income", "income"),
               ("9990", "Suspense - Under Investigation", "asset")]


def prepare_run(config, database_path: Path, data_directory: Path, period: tuple[str, str]):
    """Import, validate and normalize the calibration dataset into a scratch database."""
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
    run_id = imports.create_run(account_id, period[0], period[1], users["Maya Castillo"])
    imports.import_directory(run_id, data_directory, users["Maya Castillo"])
    summary = ValidationService(database, repositories, audit, config).validate_and_normalize(
        run_id, users["Maya Castillo"])
    if not summary.passed:
        raise SystemExit(f"validation failed on {data_directory}: {summary.failures}")
    return database, repositories, run_id


def load_items(repositories, run_id: int) -> dict[str, list[Item]]:
    return {item_type: [Item.from_row(item_type, row)
                        for row in repositories.items.list(run_id, item_type,
                                                           statuses=("validated",))]
            for item_type in ("bank", "ledger", "carry_in")}


def load_truth(data_directory: Path) -> dict[tuple[str, str], str]:
    """External identifier to true match group. Read here and nowhere else."""
    with (data_directory / "ground_truth.csv").open() as handle:
        return {(row["item_type"], row["item_id"]): row["true_match_group"]
                for row in csv.DictReader(handle)}


def collect_points(rankings, truth: dict, subject_type: str) -> list[CalibrationPoint]:
    """Label every scored candidate correct or incorrect.

    Every candidate is labelled, not only the winner: calibration has to learn what a
    mid-range score means, and that can only come from candidates that were ranked and
    turned out wrong.
    """
    points: list[CalibrationPoint] = []
    for ranking in rankings:
        subject_group = truth.get((subject_type, ranking.subject.external_id), "")
        for scored in ranking.scored:
            member_groups = {truth.get((member.item_type, member.external_id), "")
                             for member in scored.members}
            correct = bool(subject_group) and member_groups == {subject_group}
            points.append(CalibrationPoint(score=scored.score, correct=correct))
    return points


def collect_negatives(subjects, pool, truth: dict, weights: Weights, *,
                      window_business_days: int, per_subject: int = 4,
                      seed: int = 20260701) -> list[CalibrationPoint]:
    """Score deliberately wrong pairings, so the calibrator sees the whole score range.

    Candidate generation is deliberately narrow: same direction, inside the date window,
    matching amount. Almost everything it proposes is correct, which leaves a calibrator
    fitted on those alone with nothing to say about a mid-range score. It would map every
    candidate to near certainty, including the ones that are wrong.

    These negatives are pairings the generator would not propose, drawn from the same
    blocked pool. They teach the mapping what a weak pairing looks like. They are used to
    fit the curve only; no run ever sees them, and no item is judged by them.

    Sampling is seeded, so the artefact is reproducible.
    """
    import random

    rng = random.Random(seed)
    points: list[CalibrationPoint] = []
    for subject in subjects:
        subject_group = truth.get((subject.item_type, subject.external_id), "")
        nearby = block(subject, pool, window_business_days)
        wrong = [item for item in nearby
                 if truth.get((item.item_type, item.external_id), "") != subject_group
                 or not subject_group]
        if not wrong:
            continue

        # Hard negatives first. A wrong pairing with a different amount scores near zero
        # and teaches the curve nothing useful; a wrong pairing with the same amount and a
        # similar date is what the system will actually confuse, and is the case the
        # confidence figure has to get right. Most of the sample is drawn from those, with
        # a few easy ones so the low end of the curve is populated too.
        target = abs(subject.cash_effect)
        wrong.sort(key=lambda item: abs(item.amount_cents - target))
        hard_count = max(1, per_subject - 1)
        selected = wrong[:hard_count]
        remainder = wrong[hard_count:]
        if remainder:
            selected.append(rng.choice(remainder))

        for item in selected:
            features = extract(subject, [item], window_business_days=window_business_days)
            points.append(CalibrationPoint(score=score_features(features, weights),
                                           correct=False))
    return points


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit the confidence calibrator (DD-11)")
    parser.add_argument("--data", default="data/july_2026_calibration")
    parser.add_argument("--out", default="models/calibration.json")
    parser.add_argument("--period-start", default="2026-07-01")
    parser.add_argument("--period-end", default="2026-07-31")
    parser.add_argument("--label", default="")
    parser.add_argument("--database", default="calibration.db")
    args = parser.parse_args()

    config = load_config()
    data_directory = Path(args.data)
    started = time.perf_counter()

    print(f"Fitting calibration on {data_directory}")
    database, repositories, run_id = prepare_run(
        config, Path(args.database), data_directory, (args.period_start, args.period_end))

    items = load_items(repositories, run_id)
    outcome = apply_rules(
        items["bank"], items["ledger"], items["carry_in"],
        period_end=__import__("datetime").date.fromisoformat(args.period_end),
        timing_window_days=config.parameters.timing_window_business_days,
        stale_check_days=config.parameters.outstanding_check_stale_days,
        stale_deposit_days=config.parameters.deposit_in_transit_stale_days)
    print(f"  rules settled {len(outcome.matches)} pairs; "
          f"{len(outcome.unmatched_bank)} bank items go to scoring")

    weights = Weights.from_config(config.scoring)
    parameters = config.parameters
    matching = config.matching

    candidate_sets = generate_all(
        outcome.unmatched_bank, outcome.unmatched_ledger,
        window_business_days=parameters.timing_window_business_days,
        near_miss_tolerance_cents=matching.near_miss_tolerance_cents,
        candidate_cap=parameters.group_candidate_cap,
        member_cap=parameters.group_member_cap)
    rankings = rank_all(candidate_sets, weights,
                        window_business_days=parameters.timing_window_business_days,
                        one_to_one_score=config.scoring.relationship_score_one_to_one,
                        group_score=config.scoring.relationship_score_group)

    truth = load_truth(data_directory)
    points = collect_points(rankings, truth, "bank")
    correct = sum(1 for point in points if point.correct)
    print(f"  {len(points)} proposed candidates labelled, {correct} correct "
          f"({correct / len(points):.1%})")

    negatives = collect_negatives(
        outcome.unmatched_bank, outcome.unmatched_ledger, truth, weights,
        window_business_days=parameters.timing_window_business_days)
    points.extend(negatives)
    print(f"  {len(negatives)} deliberately wrong pairings added, so the curve covers "
          f"low scores as well")
    correct = sum(1 for point in points if point.correct)
    print(f"  {len(points)} points in total, {correct} correct ({correct / len(points):.1%})")

    label = args.label or f"calibration-{data_directory.name}"
    calibrator = fit(points, version_label=label, dataset=str(data_directory))
    quality = assess(calibrator, points)

    print(f"\nFitted with {calibrator.method} on {calibrator.sample_size} points")
    print(f"  {quality.summary()}")
    print("\n  score -> confidence")
    for score in (0.30, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.00):
        print(f"    {score:.2f} -> {calibrator.confidence(score):.3f}")

    print("\n  stated versus observed, on the fitting data")
    print(f"    {'band':<10}{'n':>6}{'stated':>9}{'observed':>10}{'error':>8}")
    for band in quality.bands:
        print(f"    {band['band']:<10}{band['n']:>6}{band['stated']:>9.3f}"
              f"{band['observed']:>10.3f}{band['error']:>8.3f}")

    raw_quality = assess(Calibrator.identity(), points)
    print(f"\n  raw score used directly: {raw_quality.summary()}")
    print(f"  calibrated:              {quality.summary()}")

    output = Path(args.out)
    calibrator.save(output)
    print(f"\n  artefact written to {output}")
    print(f"  elapsed {time.perf_counter() - started:.1f}s")
    print("\nThese figures are measured on the data the model was fitted to and will always")
    print("look favourable. The figures that count come from the August evaluation, where")
    print("this artefact meets data it has never seen.")

    database.close()
    Path(args.database).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(args.database + suffix).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
