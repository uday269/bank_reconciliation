"""Evaluation tests (TC-U-168, TC-I-074..078, TC-R-003, TC-C-065).

The evaluator is the one place the application reads ground truth. These tests pin its
figures to the benchmarks, prove it never writes to the working database, and check the
definitions it applies: what counts as a correct match, a complete audit event, and a
timed decision.
"""

from __future__ import annotations

import pytest

from app.services.evaluation_service import (EvaluationService, load_truth, render_markdown,
                                             TIMING_PROTOCOL_ITEMS)
from tests.conftest import DATASET, REPOSITORY_ROOT
from tests.test_reports import complete_august_review
from tests.web_support import as_harness, build_services

CALIBRATION = REPOSITORY_ROOT / "models" / "calibration.json"


@pytest.fixture(scope="module")
def pipeline_evaluation(tmp_path_factory):
    services, _, _ = build_services(tmp_path_factory.mktemp("evaluation"), match=False)
    return EvaluationService(services.config, CALIBRATION).evaluate(DATASET)


@pytest.fixture
def evaluator(tmp_path):
    services, users, run_id = build_services(tmp_path)
    return EvaluationService(services.config, CALIBRATION), services, users, run_id


def test_tci_074_pipeline_figures_match_the_benchmarks(pipeline_evaluation):
    """FR-EVL-01, -06, -09: the same figures as reports/hybrid_benchmark.md."""
    rules, hybrid = pipeline_evaluation.rules_only, pipeline_evaluation.hybrid
    assert (rules.correct, rules.proposed, rules.found_groups, rules.true_groups) == (390, 390, 390, 568)
    assert (hybrid.correct, hybrid.proposed, hybrid.found_groups, hybrid.true_groups) == (556, 556, 556, 568)
    assert hybrid.by_scenario["SCN-05"] == (0, 10) and hybrid.by_scenario["SCN-02"] == (95, 95)
    assert sum(proposed for _, proposed in hybrid.bands.values()) == 166
    assert hybrid.calibration is not None and hybrid.capped_searches == 28
    assert (hybrid.exception_agreed, hybrid.exception_total) == (69, 79)


def test_tci_075_the_hypothetical_auto_accept_policy_and_the_statement(pipeline_evaluation):
    """FR-EVL-02, -03 (DD-01, DD-03), and the before-review and correct differences."""
    hybrid = pipeline_evaluation.hybrid
    assert hybrid.hypothetical_accepted > 0 and hybrid.hypothetical_wrong == 0
    assert 0 < hybrid.items_hypothetically_settled < hybrid.items_total == 1257
    assert hybrid.provisional_difference_cents == -7_766_305
    assert pipeline_evaluation.true_difference_cents == -939_235


def test_tci_076_a_reviewed_run_is_measured_against_ground_truth(evaluator):
    """FR-EVL-04, -05, -07 on a complete review."""
    service, services, users, run_id = evaluator
    complete_august_review(as_harness(services, users, run_id), services.review, services.adjustments)
    services.reports.generate_package(run_id, users["Maya Castillo"])
    services.period.sign_off(run_id, users["Daniel Okafor"], comment="Duplicates under bank research")

    review = service.measure_review(services, run_id, load_truth(DATASET))
    assert review.decided > 600 and review.decisions["approve"] > 0
    assert review.approvals_checked > 0 and review.approvals_correct == review.approvals_checked
    assert review.manual_pairings == 10 and review.batches == 1
    assert review.audit_complete == review.audit_total and review.chain_intact
    assert review.signed_difference_cents == -939_235
    assert "Priya Raman" in review.timing          # decisions made after opening the detail view
    assert "Maya Castillo" not in review.timing    # her only decisions were one batch


def test_tci_077_an_unreviewed_run_reports_counts_not_figures(evaluator):
    """FR-EVL-10: nothing decided means n = 0, never a rate."""
    service, services, _, run_id = evaluator
    review = service.measure_review(services, run_id, load_truth(DATASET))
    assert review.decided == 0 and review.timing == {}
    assert review.audit_complete == review.audit_total


def test_tci_078_the_written_evaluation_shows_counts_beside_every_rate(pipeline_evaluation):
    text = render_markdown(pipeline_evaluation)
    assert "1.0000 (556/556)" in text and "0.9789 (556/568)" in text
    assert "100% by design" in text and "No reviewed run was given" in text
    assert "-$9,392.35" in text and "SCN-05 | 10 |" in text


def test_tcu_168_audit_completeness_applies_fields_by_event_type():
    """FR-EVL-07: a rejection without a comment is incomplete; a detail-view event needs none."""
    base = {"actor_type": "human", "actor_user_id": 1, "rule_name": "BR-01", "model_version_id": None,
            "item_refs": '["BT-1"]', "decision": "reject", "approval_status": "rejected", "risk_level": "low",
            "explanation": "x", "evidence_refs": '["r:1"]', "previous_status": "proposed",
            "new_status": "proposed", "comment": None, "event_type": "DECISION"}
    assert EvaluationService._missing_fields(base) == ["comment"]
    assert EvaluationService._missing_fields({**base, "comment": "Wrong invoice"}) == []
    assert EvaluationService._missing_fields({**base, "rule_name": None, "comment": "x"}) == [
        "rule_name or model_version_id"]
    assert EvaluationService._missing_fields({"event_type": "DETAIL_OPENED", "actor_type": "human",
                                              "actor_user_id": 1}) == []


def test_tcr_003_the_timing_sample_is_seeded_stratified_and_disjoint(evaluator):
    service, services, _, run_id = evaluator
    first, second = service.timing_sample(services, run_id)
    assert service.timing_sample(services, run_id) == [first, second]
    assert not set(first) & set(second)
    assert abs(len(first) - TIMING_PROTOCOL_ITEMS) <= 2 and abs(len(second) - TIMING_PROTOCOL_ITEMS) <= 2
    categories = {services.repositories.recommendations.get(i)["category_code"] for i in first}
    assert {"CAT-01", "CAT-02", "CAT-04"} <= categories


def test_tcc_065_evaluation_never_writes_to_the_working_database(evaluator):
    """FR-EVL-11: pipeline runs use their own databases; the reviewed run is only read."""
    service, services, _, run_id = evaluator
    events_before = services.audit.count(run_id)
    runs_before = services.repositories.runs.latest()["run_id"]
    service.evaluate(DATASET, services, run_id)
    assert services.audit.count(run_id) == events_before
    assert services.repositories.runs.latest()["run_id"] == runs_before
