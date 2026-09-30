"""Integration, scenario and reproducibility tests (TC-I, TC-S, TC-R).

These run the real pipeline over the real dataset, so they check behaviour rather than
mocks. The dataset is seeded, which is what lets a test assert an exact count.
"""

from __future__ import annotations

import csv
from collections import defaultdict

import pytest

from tests.conftest import DATASET


def ground_truth() -> dict[tuple[str, str], dict]:
    with (DATASET / "ground_truth.csv").open() as handle:
        return {(row["item_type"], row["item_id"]): row for row in csv.DictReader(handle)}


def external_ids(harness, run_id: int) -> dict[tuple[str, int], str]:
    mapping = {}
    for item_type, table, key, external in (
            ("bank", "bank_transaction", "bank_transaction_id", "external_txn_id"),
            ("ledger", "ledger_entry", "ledger_entry_id", "external_entry_id"),
            ("carry_in", "carry_in_item", "carry_in_item_id", "external_item_id")):
        for row in harness.database.query(
                f"SELECT {key} AS item_id, {external} AS external FROM {table} WHERE run_id = ?",
                (run_id,)):
            mapping[(item_type, row["item_id"])] = row["external"]
    return mapping


def matched_pairs(harness, run_id: int) -> dict[int, list[tuple[str, int]]]:
    """The leading candidate of every proposed match. Rank 1 only: the alternatives are
    kept for the reviewer to see, but they are not what the system proposed."""
    rows = harness.database.query(
        """SELECT r.recommendation_id, cm.bank_transaction_id, cm.ledger_entry_id,
                  cm.carry_in_item_id
           FROM recommendation r
           JOIN candidate c ON c.recommendation_id = r.recommendation_id
                           AND c.rank_order = 1
           JOIN candidate_member cm ON cm.candidate_id = c.candidate_id
           WHERE r.run_id = ? AND r.kind = 'match'""", (run_id,))
    pairs: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for row in rows:
        for item_type, column in (("bank", "bank_transaction_id"), ("ledger", "ledger_entry_id"),
                                  ("carry_in", "carry_in_item_id")):
            if row[column] is not None:
                pairs[row["recommendation_id"]].append((item_type, row[column]))
    return pairs


# -- integration ------------------------------------------------------------

def test_tci_001_import_stores_every_readable_row(harness):
    run_id = harness.imported_run()
    assert len(harness.repositories.items.list(run_id, "bank")) == 611
    assert len(harness.repositories.items.list(run_id, "ledger")) == 628
    assert len(harness.repositories.items.list(run_id, "carry_in")) == 18
    assert harness.repositories.runs.get(run_id)["status"] == "validating"


def test_tci_002_stored_totals_agree_with_the_declared_control_totals(harness):
    """FR-VAL-03: the statement's own totals prove the extract is complete."""
    run_id = harness.imported_run()
    for item_type, file_type in (("bank", "bank"), ("ledger", "gl")):
        source = harness.repositories.source_files.by_type(run_id, file_type)
        actual = harness.repositories.items.totals_cents(run_id, item_type)
        assert actual["debit"] == source["control_total_debits_cents"]
        assert actual["credit"] == source["control_total_credits_cents"]


def test_tci_003_validation_finds_the_seeded_problems(harness):
    run_id = harness.imported_run()
    summary = harness.validation.validate_and_normalize(run_id)
    assert summary.passed
    assert summary.excluded_rows == 1                 # the unreadable amount
    assert summary.possible_duplicates == 12          # 4 bank, 8 ledger
    assert summary.normalized_items == 1257
    assert harness.repositories.runs.get(run_id)["status"] == "processing"


def test_tci_004_normalization_keeps_the_original_value(harness):
    run_id = harness.imported_run()
    harness.validation.validate_and_normalize(run_id)
    row = harness.repositories.items.get("bank", 1)
    assert row["description_original"]
    assert row["description_normalized"]
    assert row["description_normalized"] == row["description_normalized"].upper()
    changes = harness.repositories.normalization.for_item(run_id, "bank", 1)
    assert changes, "a changed field must leave a record (FR-NRM-05)"


def test_tci_005_every_item_ends_with_a_recommendation(harness):
    """CR-01: nothing is quietly left without a proposed treatment."""
    run_id = harness.matched_run()
    for item_type in ("bank", "ledger", "carry_in"):
        statuses = harness.repositories.items.status_counts(run_id, item_type)
        assert set(statuses) <= {"proposed", "excluded"}
    assert harness.repositories.recommendations.count(run_id) > 0


def test_tci_006_the_run_reaches_review_with_populated_queues(harness):
    run_id = harness.matched_run()
    assert harness.repositories.runs.get(run_id)["status"] == "in_review"
    queues = harness.repositories.recommendations.queue_counts(run_id)
    assert queues["CAT-01"] == 378                      # exact matches are rule-settled
    assert queues.get("CAT-02", 0) > 0                  # scoring produces high-confidence items
    assert queues.get("CAT-05", 0) > 0                  # high risk still reaches the senior queue


def test_tci_007_queue_order_puts_risk_first(harness):
    """FR-REV-02: the least certain work is reached first, not last."""
    run_id = harness.matched_run()
    queue = harness.repositories.recommendations.queue(run_id, "CAT-05")
    levels = [row["risk_level"] for row in queue]
    assert levels == sorted(levels, key=lambda level: {"high": 0, "medium": 1, "low": 2}[level])


# -- scenarios --------------------------------------------------------------

def test_tcs_001_baseline_makes_no_incorrect_match(harness):
    """Precision must be 1.0: a rules-only system may miss, but must never guess."""
    run_id = harness.matched_run(use_scoring=False)
    truth = ground_truth()
    ids = external_ids(harness, run_id)
    for members in matched_pairs(harness, run_id).values():
        groups = {truth[(item_type, ids[(item_type, item_id)])]["true_match_group"]
                  for item_type, item_id in members}
        assert len(groups) == 1 and "" not in groups


def test_tcs_002_every_exact_pair_is_found(harness):
    """SCN-01: 378 exact pairs plus 12 carry-in clearings."""
    run_id = harness.matched_run(use_scoring=False)
    truth = ground_truth()
    ids = external_ids(harness, run_id)
    expected = {row["true_match_group"] for row in truth.values()
                if row["scenario_code"] == "SCN-01" and row["true_match_group"]}
    found = set()
    for members in matched_pairs(harness, run_id).values():
        groups = {truth[(item_type, ids[(item_type, item_id)])]["true_match_group"]
                  for item_type, item_id in members}
        if len(groups) == 1:
            found |= groups
    assert expected <= found


def test_tcs_003_bank_originated_items_are_classified_by_type(harness):
    """SCN-06: 10 fees, 3 interest credits, 3 returned items."""
    run_id = harness.matched_run()
    counts = harness.repositories.recommendations.exception_counts(run_id)
    assert counts["EXC-03"] == 10
    assert counts["EXC-04"] == 3


def test_tcs_004_suspected_duplicates_are_flagged_not_matched(harness):
    """SCN-07: both copies go to a reviewer, because neither can be trusted."""
    run_id = harness.matched_run()
    assert harness.repositories.recommendations.exception_counts(run_id)["EXC-05"] == 12
    flagged = harness.database.scalar(
        "SELECT COUNT(*) FROM bank_transaction WHERE run_id = ? AND is_possible_duplicate = 1",
        (run_id,))
    assert flagged == 4


def test_tcs_005_unexplained_items_are_escalated(harness):
    """SCN-08: no candidate and no recognizable payee means senior review."""
    run_id = harness.matched_run()
    rows = harness.database.query(
        "SELECT risk_level, category_code FROM recommendation "
        "WHERE run_id = ? AND exception_code = 'EXC-07'", (run_id,))
    assert rows
    assert all(row["risk_level"] == "high" and row["category_code"] == "CAT-05" for row in rows)


def test_tcs_006_large_items_require_senior_approval(harness):
    """CR-02 and RR-01: nothing at or above the threshold sits in a first-level queue."""
    run_id = harness.matched_run()
    threshold = harness.config.parameters.senior_approval_amount_cents
    rows = harness.database.query(
        """SELECT r.category_code, b.amount_cents
           FROM recommendation r JOIN bank_transaction b
             ON b.bank_transaction_id = r.subject_item_id AND r.subject_item_type = 'bank'
           WHERE r.run_id = ? AND b.amount_cents >= ?""", (run_id, threshold))
    assert rows
    assert all(row["category_code"] == "CAT-05" for row in rows)


# -- reproducibility --------------------------------------------------------

def test_tcr_001_two_runs_of_the_same_data_agree(harness):
    """NFR-01 and SC-06: same input, same recommendations."""
    first = harness.matched_run()
    second = harness.matched_run()

    def fingerprint(run_id: int):
        return harness.database.query(
            """SELECT subject_item_type, kind, category_code, risk_level, exception_code,
                      rule_name, explanation
               FROM recommendation WHERE run_id = ?
               ORDER BY subject_item_type, subject_item_id""", (run_id,))

    assert [tuple(row) for row in fingerprint(first)] == [tuple(row) for row in fingerprint(second)]


def test_tcr_002_the_parameter_snapshot_is_frozen_on_the_run(harness):
    """ADR-10: a later configuration change cannot alter what a run reported."""
    run_id = harness.matched_run()
    stored = harness.repositories.runs.get(run_id)["parameter_snapshot"]
    assert '"senior_approval_amount_cents":1000000' in stored.replace(" ", "")


# -- hybrid layer -----------------------------------------------------------

def test_tch_001_scoring_finds_matches_the_rules_cannot(harness):
    """The point of the AI layer: recall above the rules-only baseline."""
    baseline = harness.matched_run(use_scoring=False)
    hybrid = harness.matched_run(use_scoring=True)

    def groups_found(run_id: int) -> set[str]:
        truth = ground_truth()
        ids = external_ids(harness, run_id)
        found = set()
        for members in matched_pairs(harness, run_id).values():
            groups = {truth[(item_type, ids[(item_type, item_id)])]["true_match_group"]
                      for item_type, item_id in members}
            if len(groups) == 1 and "" not in groups:
                found |= groups
        return found

    assert len(groups_found(hybrid)) > len(groups_found(baseline))


def test_tch_002_scored_matches_are_correct(harness):
    """Recall must not be bought with wrong matches."""
    run_id = harness.matched_run()
    truth = ground_truth()
    ids = external_ids(harness, run_id)
    sources = {row["recommendation_id"]: row["source"] for row in harness.database.query(
        "SELECT recommendation_id, source FROM recommendation WHERE run_id = ?", (run_id,))}

    for recommendation_id, members in matched_pairs(harness, run_id).items():
        if sources[recommendation_id] != "ai":
            continue
        groups = {truth[(item_type, ids[(item_type, item_id)])]["true_match_group"]
                  for item_type, item_id in members}
        assert len(groups) == 1 and "" not in groups, f"recommendation {recommendation_id} is wrong"


def test_tch_003_every_scored_match_keeps_its_candidates(harness):
    """FR-REV-09: the alternatives a reviewer must see are stored, not discarded."""
    run_id = harness.matched_run()
    scored = harness.database.query(
        "SELECT recommendation_id FROM recommendation WHERE run_id = ? AND source = 'ai'",
        (run_id,))
    assert scored
    for row in scored:
        candidates = harness.repositories.recommendations.candidates(row["recommendation_id"])
        assert candidates
        assert [candidate["rank_order"] for candidate in candidates] == \
            list(range(1, len(candidates) + 1))
        assert all(candidate["feature_values"] for candidate in candidates)


def test_tch_004_scored_matches_record_their_model_version(harness):
    """FR-AI-07: a confidence figure must say which artefact produced it."""
    run_id = harness.matched_run()
    rows = harness.database.query(
        """SELECT r.confidence, m.version_label
           FROM recommendation r JOIN model_version m
             ON m.model_version_id = r.model_version_id
           WHERE r.run_id = ? AND r.source = 'ai'""", (run_id,))
    assert rows
    assert all(row["confidence"] is not None and row["version_label"] for row in rows)


def test_tch_005_explanations_disclose_conflicting_evidence(harness):
    """CR-15: an ambiguous item must say why it is ambiguous."""
    run_id = harness.matched_run()
    ambiguous = harness.database.query(
        "SELECT explanation FROM recommendation "
        "WHERE run_id = ? AND source = 'ai' AND category_code = 'CAT-03'", (run_id,))
    for row in ambiguous:
        assert "Conflicting evidence" in row["explanation"] or "Other candidates" in row["explanation"]


def test_tch_006_capped_searches_become_exceptions(harness):
    """DD-07: a limited search is disclosed, never reported as nothing found."""
    run_id = harness.matched_run()
    capped = harness.database.query(
        "SELECT explanation FROM recommendation WHERE run_id = ? AND exception_code = 'EXC-08'",
        (run_id,))
    assert capped
    assert all("limited" in row["explanation"].lower() for row in capped)


def test_tch_007_rules_only_and_hybrid_agree_on_rule_matches(harness):
    """Scoring must not change what the deterministic rules decided (ADR-05)."""
    baseline = harness.matched_run(use_scoring=False)
    hybrid = harness.matched_run(use_scoring=True)

    def rule_matches(run_id: int) -> set:
        return {(row["subject_item_type"], row["rule_name"]) for row in harness.database.query(
            "SELECT subject_item_type, rule_name FROM recommendation "
            "WHERE run_id = ? AND source = 'rule' AND kind = 'match'", (run_id,))}

    assert rule_matches(baseline) == rule_matches(hybrid)
