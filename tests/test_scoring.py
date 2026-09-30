"""Tests for the scoring layer (TC-U): text, features, candidates, scoring, calibration.

Pure functions, so every test states its inputs in full and needs no database.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.calibration import CalibrationPoint, Calibrator, assess, fit
from app.domain.candidates import generate, group_candidates, one_to_one_candidates
from app.domain.explanation import explain_match, explain_no_candidate
from app.domain.features import (
    amount_similarity, date_distance, extract, reference_similarity, text_similarity,
)
from app.domain.rules import Item
from app.domain.scoring import Weights, rank_candidates, score_features
from app.domain.text import abbreviation_similarity, best_similarity, shared_words, tokens

OPTIONS = dict(window_business_days=3, near_miss_tolerance_cents=100,
               candidate_cap=15, member_cap=4)


def bank(amount=482000, day=14, item_id=1, description="CANYON RIDGE GROCERY",
         reference="8842", duplicate=False):
    return Item("bank", item_id, f"BT-{item_id:06d}", date(2026, 8, day), amount, "credit",
                description, reference, description, "ACH-CR", duplicate)


def ledger(amount=482000, day=14, item_id=1, description="CANYON RIDGE GROCERY",
           reference="8842"):
    return Item("ledger", item_id, f"GL-{item_id:06d}", date(2026, 8, day), amount, "debit",
                description, reference, description)


# -- text similarity --------------------------------------------------------

def test_tcu_100_abbreviations_score_close_to_the_full_name():
    """SCN-03: bank statements abbreviate, usually by dropping vowels."""
    assert best_similarity("CYN RIDGE GROC", "CANYON RIDGE GROCERY") >= 0.85
    assert best_similarity("SLVR FORK CAFE", "SILVER FORK CAFE") >= 0.85
    assert abbreviation_similarity("TIMP COFFEE", "TIMPANOGOS COFFEE") >= 0.90


def test_tcu_101_different_counterparties_score_low():
    assert best_similarity("CANYON RIDGE GROCERY", "ALTA VISTA BISTRO") < 0.60
    assert best_similarity("PARK CITY MARKET", "MOAB TRADING POST") < 0.60


def test_tcu_102_word_order_and_suffixes_do_not_matter():
    assert best_similarity("ALTA VISTA BISTRO", "BISTRO ALTA VISTA LLC") >= 0.90
    assert "LLC" not in tokens("ALTA VISTA BISTRO LLC")


def test_tcu_103_shared_words_are_reported_for_the_explanation():
    assert shared_words("CYN RIDGE GROC", "CANYON RIDGE GROCERY") == ["RIDGE"]


# -- features ---------------------------------------------------------------

def test_tcu_110_date_distance_decays_with_the_gap():
    assert date_distance(0, 3) == 1.0
    assert date_distance(1, 3) > date_distance(2, 3) > date_distance(3, 3)
    assert date_distance(3, 3) == pytest.approx(0.3679, abs=0.001)


def test_tcu_111_amount_similarity_is_one_only_when_equal():
    """A near miss must never display as an exact match (BR-04 requires an exact sum)."""
    assert amount_similarity(482000, 482000) == 1.0
    assert amount_similarity(482000, 481999) < 1.0
    assert amount_similarity(482000, 458000) < 0.6


def test_tcu_112_missing_and_different_references_are_distinguished():
    assert reference_similarity("8842", "8842") == (1.0, "equal")
    assert reference_similarity("8842", "9001") == (0.0, "different")
    assert reference_similarity("8842", None)[1] == "missing"
    assert reference_similarity(None, None)[1] == "none"


def test_tcu_113_group_text_is_averaged_not_concatenated():
    """One matching member must not carry an otherwise unrelated group."""
    members = [ledger(180000, item_id=1, description="CANYON RIDGE GROCERY"),
               ledger(160000, item_id=2, description="ALTA VISTA BISTRO"),
               ledger(142000, item_id=3, description="SILVER FORK CAFE")]
    features = extract(bank(), members, window_business_days=3)
    assert features.text_similarity < 0.70


def test_tcu_114_a_group_is_judged_on_its_furthest_member():
    members = [ledger(241000, item_id=1, day=14), ledger(241000, item_id=2, day=11)]
    features = extract(bank(), members, window_business_days=3)
    assert features.business_day_gap == -3


# -- candidate generation ---------------------------------------------------

def test_tcu_120_blocking_excludes_the_wrong_side_and_the_wrong_dates():
    pool = [ledger(482000, item_id=1),                       # correct
            ledger(482000, item_id=2, day=25),               # outside the window
            ledger(999999, item_id=3)]                       # wrong amount
    found = one_to_one_candidates(bank(), pool, window_business_days=3,
                                  near_miss_tolerance_cents=100, limit=15)
    assert [candidate.members[0].item_id for candidate in found] == [1]


def test_tcu_121_group_search_finds_mid_sized_members():
    """A search over only the largest items would miss this group entirely."""
    pool = [ledger(900000, item_id=100 + n) for n in range(20)]
    pool += [ledger(70500, item_id=1), ledger(187600, item_id=2),
             ledger(77000, item_id=3), ledger(202600, item_id=4)]
    result = generate(bank(537700), pool, **OPTIONS)
    groups = [candidate for candidate in result.candidates if candidate.is_group]
    assert groups
    assert {member.item_id for member in groups[0].members} == {1, 2, 3, 4}


def test_tcu_122_group_members_sum_exactly():
    pool = [ledger(180000, item_id=1), ledger(160000, item_id=2), ledger(142000, item_id=3)]
    groups, _, _ = group_candidates(bank(482000), pool, window_business_days=3,
                                    candidate_cap=15, member_cap=4)
    assert groups
    assert all(candidate.total_cents == 482000 for candidate in groups)


def test_tcu_123_exceeding_the_member_cap_is_reported_not_hidden():
    """DD-07: a limited search must say so, or the item looks unmatchable."""
    pool = [ledger(96400, item_id=n) for n in range(1, 6)]    # five members needed, cap is four
    result = generate(bank(482000), pool, **OPTIONS)
    assert not result.candidates
    assert result.search_capped
    assert "larger group" in result.cap_reason


def test_tcu_124_an_exhaustive_search_is_not_reported_as_capped():
    """A false disclosure is as misleading as a missing one."""
    pool = [ledger(11111, item_id=1), ledger(22222, item_id=2)]
    result = generate(bank(482000), pool, **OPTIONS)
    assert not result.candidates
    assert not result.search_capped


def test_tcu_125_a_very_large_pool_is_reported_rather_than_sampled():
    pool = [ledger(20000 + n, item_id=n) for n in range(1, 600)]
    result = generate(bank(482000), pool, **OPTIONS)
    assert result.search_capped
    assert "exceeds the search limit" in result.cap_reason


# -- scoring ----------------------------------------------------------------

def test_tcu_130_weights_must_sum_to_one():
    with pytest.raises(ValueError):
        Weights(date_distance=0.5, amount_similarity=0.5, text_similarity=0.5,
                reference_similarity=0.5, relationship_type=0.5).validate()


def test_tcu_131_a_perfect_pairing_scores_one():
    features = extract(bank(), [ledger()], window_business_days=3)
    assert score_features(features, Weights()) == 1.0


def test_tcu_132_the_better_supported_candidate_ranks_first():
    pool = [ledger(482000, item_id=1),                                    # perfect
            ledger(482000, item_id=2, day=12, reference="9001"),          # wrong reference
            ledger(481950, item_id=3, reference=None,
                   description="ALTA VISTA BISTRO")]                      # wrong everything
    ranking = rank_candidates(generate(bank(), pool, **OPTIONS), Weights(),
                              window_business_days=3)
    assert ranking.best.members[0].item_id == 1
    assert [scored.rank for scored in ranking.scored] == [1, 2, 3]
    assert ranking.margin > 0


def test_tcu_133_ranking_is_deterministic():
    pool = [ledger(482000, item_id=1, reference=None), ledger(482000, item_id=2, reference=None)]
    first = rank_candidates(generate(bank(reference=None), pool, **OPTIONS), Weights(),
                            window_business_days=3)
    second = rank_candidates(generate(bank(reference=None), list(reversed(pool)), **OPTIONS),
                             Weights(), window_business_days=3)
    assert [scored.members[0].item_id for scored in first.scored] == \
           [scored.members[0].item_id for scored in second.scored]


def test_tcu_134_a_single_item_is_preferred_to_a_group_at_the_same_score():
    pool = [ledger(482000, item_id=1, reference=None),
            ledger(241000, item_id=2, reference=None), ledger(241000, item_id=3, reference=None)]
    ranking = rank_candidates(generate(bank(reference=None), pool, **OPTIONS), Weights(),
                              window_business_days=3)
    assert not ranking.best.is_group


# -- explanations -----------------------------------------------------------

def test_tcu_140_a_close_runner_up_is_disclosed():
    pool = [ledger(482000, item_id=1, reference=None), ledger(482000, item_id=2, reference=None)]
    ranking = rank_candidates(generate(bank(reference=None), pool, **OPTIONS), Weights(),
                              window_business_days=3)
    explanation = explain_match(ranking, window_business_days=3, ambiguity_margin=0.15)
    assert explanation.has_conflict
    assert any("behind" in reason for reason in explanation.conflicting)


def test_tcu_141_a_reference_mismatch_is_disclosed():
    ranking = rank_candidates(
        generate(bank(reference="8842"), [ledger(482000, item_id=1, reference="9001")], **OPTIONS),
        Weights(), window_business_days=3)
    explanation = explain_match(ranking, window_business_days=3, ambiguity_margin=0.15)
    assert any("do not agree" in reason for reason in explanation.conflicting)


def test_tcu_142_alternatives_are_listed_for_the_reviewer():
    pool = [ledger(482000, item_id=n, reference=None) for n in range(1, 4)]
    ranking = rank_candidates(generate(bank(reference=None), pool, **OPTIONS), Weights(),
                              window_business_days=3)
    explanation = explain_match(ranking, window_business_days=3, ambiguity_margin=0.15)
    assert len(explanation.alternatives) == 2


def test_tcu_143_an_explanation_never_states_a_verdict():
    """CR-01: the system describes evidence; the reviewer decides."""
    ranking = rank_candidates(generate(bank(), [ledger()], **OPTIONS), Weights(),
                              window_business_days=3)
    text = explain_match(ranking, window_business_days=3, ambiguity_margin=0.15).as_text().lower()
    for verdict in ("approve", "recommend", "is a match", "correct match", "should be"):
        assert verdict not in text


def test_tcu_144_an_item_with_no_candidate_says_how_many_were_considered():
    explanation = explain_no_candidate(bank(), considered=41, window_business_days=3)
    assert any("41 records were considered" in reason for reason in explanation.conflicting)


# -- calibration ------------------------------------------------------------

def _points(seed: int = 3, count: int = 800) -> list[CalibrationPoint]:
    import random
    rng = random.Random(seed)
    points = []
    for _ in range(count):
        score = round(rng.random(), 4)
        points.append(CalibrationPoint(score, rng.random() < max(0.0, (score - 0.4) * 1.6)))
    return points


def test_tcu_150_calibration_is_monotonic():
    calibrator = fit(_points(), version_label="test", dataset="synthetic")
    values = [calibrator.confidence(score / 100) for score in range(101)]
    assert values == sorted(values)


def test_tcu_151_confidence_never_claims_certainty():
    """A finite sample cannot establish 1.0, and an interface showing it invites over-trust."""
    calibrator = fit(_points(), version_label="test", dataset="synthetic")
    assert max(calibrator.probabilities) <= 0.98
    assert min(calibrator.probabilities) >= 0.02


def test_tcu_152_calibration_improves_on_the_raw_score():
    points = _points()
    calibrator = fit(points, version_label="test", dataset="synthetic")
    assert assess(calibrator, points).expected_calibration_error < \
           assess(Calibrator.identity(), points).expected_calibration_error


def test_tcu_153_too_few_points_is_refused():
    with pytest.raises(ValueError):
        fit(_points(count=50), version_label="test", dataset="synthetic")


def test_tcu_154_an_unfitted_calibrator_labels_itself(tmp_path):
    calibrator = Calibrator.identity()
    assert calibrator.version_label == "uncalibrated"
    assert calibrator.confidence(0.83) == 0.83


def test_tcu_155_an_artefact_survives_a_round_trip(tmp_path):
    calibrator = fit(_points(), version_label="round-trip", dataset="synthetic")
    path = calibrator.save(tmp_path / "calibration.json")
    loaded = Calibrator.load(path)
    assert all(loaded.confidence(score / 50) == calibrator.confidence(score / 50)
               for score in range(51))
    assert loaded.sample_size == calibrator.sample_size


def test_tcu_156_an_unknown_artefact_version_is_refused(tmp_path):
    import json
    path = tmp_path / "calibration.json"
    calibrator = fit(_points(), version_label="v", dataset="synthetic")
    calibrator.save(path)
    data = json.loads(path.read_text())
    data["artefact_version"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        Calibrator.load(path)
