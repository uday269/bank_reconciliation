"""Adjustment service tests (TC-I-025..029, TC-C-043..048).

Adjustments are proposals with an approval trail. These tests prove the trail is
complete, that CR-04 holds, and that nothing is ever posted.
"""

from __future__ import annotations

import pytest

from app.control import ControlViolation
from app.services.adjustment_service import AdjustmentInputError, AdjustmentService

AT = "2026-09-09T15:00:00Z"


@pytest.fixture
def adjustments(harness):
    harness.run_id = harness.matched_run()
    return AdjustmentService(harness.database, harness.repositories, harness.audit)


def exception_rec(harness, code):
    rows = harness.database.query(
        "SELECT * FROM recommendation WHERE run_id = ? AND exception_code = ? "
        "ORDER BY recommendation_id LIMIT 1", (harness.run_id, code))
    return rows[0]


def events(harness, event_type):
    return [row for row in harness.audit.events(harness.run_id) if row["event_type"] == event_type]


def propose_fee(harness, adjustments, preparer="Maya Castillo"):
    rec = exception_rec(harness, "EXC-03")
    suggestion = adjustments.suggestion(rec["recommendation_id"])
    adjustment_id = adjustments.propose(
        rec["recommendation_id"], harness.users[preparer], amount_cents=suggestion.amount_cents,
        debit_account_code=suggestion.debit_account_code,
        credit_account_code=suggestion.credit_account_code, rationale=suggestion.rationale, at=AT)
    return rec, adjustment_id


def refused(harness, rule, call, *args, **kwargs):
    blocked_before = len(events(harness, "BLOCKED_ATTEMPT"))
    with pytest.raises(ControlViolation) as caught:
        call(*args, **kwargs)
    assert caught.value.rule == rule
    blocked = events(harness, "BLOCKED_ATTEMPT")
    assert len(blocked) == blocked_before + 1 and blocked[-1]["rule_name"] == rule
    return caught.value


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_tci_025_the_suggestion_comes_from_the_exception_category(harness, adjustments):
    """BR-16: a fee suggests debit 6810, credit 1010, for the fee amount."""
    rec = exception_rec(harness, "EXC-03")
    item = harness.repositories.items.get(rec["subject_item_type"], rec["subject_item_id"])
    suggestion = adjustments.suggestion(rec["recommendation_id"])
    assert (suggestion.debit_account_code, suggestion.credit_account_code) == ("6810", "1010")
    assert suggestion.amount_cents == item["amount_cents"]
    interest = adjustments.suggestion(exception_rec(harness, "EXC-04")["recommendation_id"])
    assert (interest.debit_account_code, interest.credit_account_code) == ("1010", "7010")
    assert adjustments.suggestion(exception_rec(harness, "EXC-01")["recommendation_id"]) is None


def test_tci_026_a_proposal_is_recorded_and_awaits_approval(harness, adjustments):
    rec, adjustment_id = propose_fee(harness, adjustments)
    stored = harness.repositories.adjustments.get(adjustment_id)
    assert (stored["status"], stored["prepared_by"]) == ("proposed", harness.users["Maya Castillo"])
    assert [row["adjustment_id"] for row in adjustments.awaiting_approval(harness.run_id)] == [adjustment_id]
    event = events(harness, "ADJUSTMENT_PROPOSED")[-1]
    assert event["entity_id"] == adjustment_id and '"posted":false' in event["revised_values"]


def test_tci_027_an_independent_approver_approves(harness, adjustments):
    """UC-09: decision row, status change and audit event, all together."""
    _, adjustment_id = propose_fee(harness, adjustments)
    assert adjustments.decide(adjustment_id, harness.users["Priya Raman"], "approve", at=AT) == "approved"
    decisions = harness.repositories.adjustments.decisions(adjustment_id)
    assert [(d["decision"], d["decided_by"]) for d in decisions] == [("approve", harness.users["Priya Raman"])]
    assert events(harness, "ADJUSTMENT_DECIDED")[-1]["approval_status"] == "approved"
    assert adjustments.awaiting_approval(harness.run_id) == []
    assert harness.audit.verify(harness.run_id).intact


def test_tci_028_a_rejected_adjustment_is_kept_and_can_be_replaced(harness, adjustments):
    """FR-ADJ-03: rejection keeps the record; a corrected proposal is then allowed."""
    rec, first_id = propose_fee(harness, adjustments)
    adjustments.decide(first_id, harness.users["Daniel Okafor"], "reject",
                       comment="Charge belongs to the payroll account")
    _, second_id = propose_fee(harness, adjustments)
    statuses = {row["adjustment_id"]: row["status"]
                for row in harness.repositories.adjustments.for_recommendation(rec["recommendation_id"])}
    assert statuses == {first_id: "rejected", second_id: "proposed"}


def test_tci_029_nothing_is_ever_posted(harness, adjustments):
    """CR-12: approval changes the adjustment's status and nothing in any ledger table."""
    ledger_before = harness.database.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total FROM ledger_entry WHERE run_id = ?",
        (harness.run_id,))[0]
    _, adjustment_id = propose_fee(harness, adjustments)
    adjustments.decide(adjustment_id, harness.users["Priya Raman"], "approve")
    ledger_after = harness.database.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total FROM ledger_entry WHERE run_id = ?",
        (harness.run_id,))[0]
    assert tuple(ledger_before) == tuple(ledger_after)


# ---------------------------------------------------------------------------
# Controls and input validation
# ---------------------------------------------------------------------------

def test_tcc_043_the_preparer_cannot_approve(harness, adjustments):
    """CR-04: Priya prepares, so Priya cannot approve; Daniel can."""
    _, adjustment_id = propose_fee(harness, adjustments, preparer="Priya Raman")
    refused(harness, "CR-04", adjustments.decide, adjustment_id, harness.users["Priya Raman"], "approve")
    assert harness.repositories.adjustments.get(adjustment_id)["status"] == "proposed"
    assert adjustments.decide(adjustment_id, harness.users["Daniel Okafor"], "approve") == "approved"


def test_tcc_044_staff_cannot_approve_and_controllers_do_not_prepare(harness, adjustments):
    _, adjustment_id = propose_fee(harness, adjustments)
    refused(harness, "ROLE", adjustments.decide, adjustment_id, harness.users["Ethan Brooks"], "approve")
    rec = exception_rec(harness, "EXC-04")
    refused(harness, "ROLE", adjustments.propose, rec["recommendation_id"], harness.users["Daniel Okafor"],
            amount_cents=100, debit_account_code="1010", credit_account_code="7010", rationale="Interest")


def test_tcc_045_only_fees_interest_and_missing_entries_take_adjustments(harness, adjustments):
    """FR-ADJ-01 and UC-08: an outstanding check is carried forward, not adjusted."""
    rec = exception_rec(harness, "EXC-01")
    refused(harness, "FR-ADJ-01", adjustments.propose, rec["recommendation_id"],
            harness.users["Maya Castillo"], amount_cents=100, debit_account_code="9990",
            credit_account_code="1010", rationale="Clear it")


def test_tcc_046_one_live_adjustment_per_item_and_no_second_decision(harness, adjustments):
    _, adjustment_id = propose_fee(harness, adjustments)
    refused(harness, "STATE", propose_fee, harness, adjustments)
    adjustments.decide(adjustment_id, harness.users["Priya Raman"], "approve")
    refused(harness, "STATE", adjustments.decide, adjustment_id, harness.users["Daniel Okafor"], "reject",
            comment="Changed my mind")


def test_tcc_047_no_adjustment_activity_in_a_closed_period(harness, adjustments):
    """FR-PER-03: the service check is the only lock on adjustment decisions."""
    _, adjustment_id = propose_fee(harness, adjustments)
    with harness.database.transaction() as connection:
        harness.repositories.runs.set_status(connection, harness.run_id, "closed")
    refused(harness, "CR-18", adjustments.decide, adjustment_id, harness.users["Priya Raman"], "approve")
    refused(harness, "CR-18", propose_fee, harness, adjustments, "Ethan Brooks")


def test_tcc_048_rejection_needs_a_comment_and_bad_entries_list_every_problem(harness, adjustments):
    _, adjustment_id = propose_fee(harness, adjustments)
    refused(harness, "CR-16", adjustments.decide, adjustment_id, harness.users["Priya Raman"], "reject")

    rec = exception_rec(harness, "EXC-06")
    blocked_before = len(events(harness, "BLOCKED_ATTEMPT"))
    with pytest.raises(AdjustmentInputError) as caught:
        adjustments.propose(rec["recommendation_id"], harness.users["Maya Castillo"], amount_cents=0,
                            debit_account_code="6820", credit_account_code="6810", rationale=" ")
    assert len(caught.value.problems) == 3     # amount, no cash side, rationale
    assert len(events(harness, "BLOCKED_ATTEMPT")) == blocked_before   # input errors are not controls
    assert harness.repositories.adjustments.for_recommendation(rec["recommendation_id"]) == []
