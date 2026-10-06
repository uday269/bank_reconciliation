"""Manual pairing tests (TC-U-162, TC-I-035..036, TC-S-007, TC-C-054..058).

The headline test is TC-S-007: a review that pairs the missed groups by hand and marks
misclassified items unresolved brings the system's own statement to exactly the
difference the dataset was built with. That is the end-to-end evidence that the
statement, the dispositions and the controls fit together.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date

import pytest

from app.control import ControlViolation
from app.domain.manual_match import check_manual_match
from app.domain.rules import Item
from app.services.period_service import PeriodService
from app.services.review_service import ManualMatchError, ReviewService
from tests.conftest import DATASET

EXTERNAL = {"bank": "external_txn_id", "ledger": "external_entry_id", "carry_in": "external_item_id"}
IDS = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id", "carry_in": "carry_in_item_id"}
COMMENT = "Agreed to the customer's remittance advice"


@pytest.fixture
def review(harness):
    harness.run_id = harness.matched_run()
    return ReviewService(harness.database, harness.repositories, harness.audit, harness.matching)


def keys_and_truth(harness):
    keys, truth, groups = {}, {}, defaultdict(list)
    for item_type in EXTERNAL:
        for row in harness.repositories.items.list(harness.run_id, item_type):
            keys[(item_type, row[EXTERNAL[item_type]])] = (item_type, row[IDS[item_type]])
    for label in csv.DictReader((DATASET / "ground_truth.csv").open()):
        key = keys.get((label["item_type"], label["item_id"]))
        if key:
            truth[key] = label
            if label["true_match_group"]:
                groups[label["true_match_group"]].append(key)
    return truth, groups


def held_only_by_own_exception(harness, key):
    holders = harness.repositories.recommendations.open_holding(harness.run_id, *key)
    return bool(holders) and all(h["kind"] == "exception" and
                                 (h["subject_item_type"], h["subject_item_id"]) == key for h in holders)


def missed_groups(harness):
    """SCN-05 groups the automated search could not settle, as (subject, counterparts)."""
    _, groups = keys_and_truth(harness)
    found = []
    for group_id, members in sorted(groups.items()):
        if not group_id.startswith("SCN-05") or len(members) < 2:
            continue
        if not all(held_only_by_own_exception(harness, key) for key in members):
            continue
        banks = [key for key in members if key[0] == "bank"]
        subject = banks[0] if len(banks) == 1 else next(key for key in members if key[0] != "bank")
        found.append((subject, [key for key in members if key != subject]))
    return found


def exception_for(harness, key):
    return harness.repositories.recommendations.open_holding(harness.run_id, *key)[0]


def item(item_type, item_id, amount, direction, ref=None):
    return Item(item_type, item_id, ref or f"{item_type}-{item_id}", date(2026, 8, 20), amount,
                direction, "", None, None)


def events(harness, event_type):
    return [row for row in harness.audit.events(harness.run_id) if row["event_type"] == event_type]


# ---------------------------------------------------------------------------
# Domain check
# ---------------------------------------------------------------------------

def test_tcu_162_a_hand_pairing_must_reconcile_to_the_cent():
    deposit = item("bank", 1, 30_000, "credit")
    receipts = [item("ledger", 1, 10_000, "debit"), item("ledger", 2, 20_000, "debit")]
    assert check_manual_match(deposit, receipts) == []
    assert check_manual_match(deposit, []) != []
    assert any("agree to the cent" in p for p in check_manual_match(deposit, receipts[:1]))
    assert any("more than once" in p for p in check_manual_match(deposit, receipts + receipts[:1]))
    assert any("must be ledger" in p for p in check_manual_match(deposit, [item("bank", 2, 30_000, "credit")]))
    # A payment cannot settle a deposit even if the amounts are equal.
    assert any("agree to the cent" in p for p in check_manual_match(deposit, [item("ledger", 3, 30_000, "credit")]))
    # No member cap: five receipts are fine when they add up.
    assert check_manual_match(item("bank", 3, 50_000, "credit"),
                              [item("ledger", n, 10_000, "debit") for n in range(10, 15)]) == []


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_tci_035_a_hand_pairing_is_recorded_beside_the_systems_candidates(harness, review):
    """Reviewer-selected and unscored; the counterparts' exceptions are superseded."""
    subject, counterparts = missed_groups(harness)[0]
    rec = exception_for(harness, subject)
    before = harness.repositories.recommendations.candidates(rec["recommendation_id"])
    priya = harness.users["Priya Raman"]
    review.open_detail(rec["recommendation_id"], priya)
    outcome = review.manual_match(rec["recommendation_id"], priya, counterparts, COMMENT)

    after = harness.repositories.recommendations.candidates(rec["recommendation_id"])
    assert len(after) == len(before) + 1
    added = after[-1]
    assert added["score"] == 0.0 and added["confidence"] is None
    assert json.loads(added["feature_values"])["origin"] == "reviewer"
    assert outcome.decision == "modify" and outcome.new_status == "approved"
    assert len(outcome.superseded) == len(counterparts)
    for key in [subject] + counterparts:
        assert harness.repositories.items.get(*key)["status"] == "approved"
    event = events(harness, "DECISION")[-1]
    assert json.loads(event["revised_values"])["manual_match"] is True


def test_tci_036_the_picker_offers_only_free_records_on_the_other_side(harness, review):
    subject, counterparts = missed_groups(harness)[0]
    rec = exception_for(harness, subject)
    options = review.manual_match_options(rec["recommendation_id"], limit=500)
    offered = {(t, row[IDS[t]]) for row in options for t in IDS if IDS[t] in row.keys()}
    assert set(counterparts) <= offered
    assert all((key[0] == "bank") != (subject[0] == "bank") for key in offered)
    assert all(held_only_by_own_exception(harness, key) for key in offered)


def test_tcs_007_a_correct_review_ties_the_statement_to_the_dataset(harness, review):
    """SCN-05 and SCN-08: pair the missed groups, mark misclassified items unresolved.

    The system's statement then shows exactly the difference the dataset was generated
    with, -$9,392.35, made up of the genuinely unexplained and duplicated items.
    """
    priya = harness.users["Priya Raman"]
    groups = missed_groups(harness)
    assert len(groups) == 10
    for subject, counterparts in groups:
        rec = exception_for(harness, subject)
        review.open_detail(rec["recommendation_id"], priya)
        review.manual_match(rec["recommendation_id"], priya, counterparts, COMMENT)

    truth, _ = keys_and_truth(harness)
    for key, label in truth.items():
        if label["exception_code"] not in ("EXC-05", "EXC-07"):
            continue
        for rec in harness.repositories.recommendations.open_holding(harness.run_id, *key):
            if rec["kind"] == "exception" and rec["exception_code"] not in ("EXC-05", "EXC-07"):
                review.open_detail(rec["recommendation_id"], priya)
                review.decide(rec["recommendation_id"], priya, "unresolved",
                              comment="Not a timing item; no source document found")

    statement = PeriodService(harness.database, harness.repositories, harness.audit).statement(harness.run_id)
    assert statement.unresolved_difference_cents == -939_235
    assert harness.audit.verify(harness.run_id).intact


# ---------------------------------------------------------------------------
# Controls and input
# ---------------------------------------------------------------------------

def test_tcc_054_a_pairing_that_does_not_reconcile_writes_nothing(harness, review):
    subject, counterparts = missed_groups(harness)[0]
    rec = exception_for(harness, subject)
    priya = harness.users["Priya Raman"]
    review.open_detail(rec["recommendation_id"], priya)
    candidates_before = len(harness.repositories.recommendations.candidates(rec["recommendation_id"]))
    blocked_before = len(events(harness, "BLOCKED_ATTEMPT"))
    with pytest.raises(ManualMatchError):
        review.manual_match(rec["recommendation_id"], priya, counterparts[:1], COMMENT)
    assert len(harness.repositories.recommendations.candidates(rec["recommendation_id"])) == candidates_before
    assert len(events(harness, "BLOCKED_ATTEMPT")) == blocked_before
    assert harness.repositories.decisions.for_recommendation(rec["recommendation_id"]) == []


def test_tcc_055_matches_are_modified_by_candidate_not_by_hand(harness, review):
    rec = harness.repositories.recommendations.queue(harness.run_id, "CAT-02")[0]
    with pytest.raises(ControlViolation) as caught:
        review.manual_match(rec["recommendation_id"], harness.users["Maya Castillo"], [], COMMENT)
    assert caught.value.rule == "FR-REV-04"
    assert events(harness, "BLOCKED_ATTEMPT")[-1]["rule_name"] == "FR-REV-04"


def test_tcc_056_high_risk_exceptions_are_paired_by_a_senior(harness, review):
    rec = next(row for row in harness.repositories.recommendations.queue(harness.run_id, "CAT-05")
               if row["kind"] == "exception")
    with pytest.raises(ControlViolation) as caught:
        review.manual_match(rec["recommendation_id"], harness.users["Maya Castillo"], [], COMMENT)
    assert caught.value.rule == "CR-02"


def test_tcc_057_a_record_another_match_proposes_cannot_be_taken(harness, review):
    """Refused before any arithmetic: two approvals can never claim the same record."""
    subject, _ = missed_groups(harness)[0]
    rec = exception_for(harness, subject)
    match = next(row for row in harness.repositories.recommendations.queue(harness.run_id, "CAT-01")
                 if row["subject_item_type"] == "bank")
    if subject[0] == "bank":
        leading = harness.repositories.recommendations.candidates(match["recommendation_id"])[0]
        taken = next(("ledger", m["ledger_entry_id"]) for m in harness.repositories.recommendations
                     .candidate_members(leading["candidate_id"]) if m["ledger_entry_id"])
    else:
        taken = ("bank", match["subject_item_id"])
    priya = harness.users["Priya Raman"]
    review.open_detail(rec["recommendation_id"], priya)
    with pytest.raises(ControlViolation) as caught:
        review.manual_match(rec["recommendation_id"], priya, [taken], COMMENT)
    assert caught.value.rule == "STATE"
    assert events(harness, "BLOCKED_ATTEMPT")[-1]["rule_name"] == "STATE"


def test_tcc_058_a_hand_pairing_needs_a_comment_and_the_detail_view(harness, review):
    subject, counterparts = missed_groups(harness)[0]
    rec = exception_for(harness, subject)
    priya = harness.users["Priya Raman"]
    with pytest.raises(ControlViolation) as caught:
        review.manual_match(rec["recommendation_id"], priya, counterparts, COMMENT)
    assert caught.value.rule == "FR-REV-10"
    review.open_detail(rec["recommendation_id"], priya)
    with pytest.raises(ControlViolation) as caught:
        review.manual_match(rec["recommendation_id"], priya, counterparts, "")
    assert caught.value.rule == "CR-16"
