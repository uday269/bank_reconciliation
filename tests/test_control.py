"""Control layer tests (TC-C-019..034, TC-U-157..164).

The control functions are pure, so each rule is tested on plain dictionaries: the
refusal it must raise, the rule it must name, and the case it must allow. The
service tests in later files add the other half of each control test: that a
refusal writes a BLOCKED_ATTEMPT event and nothing else.
"""

from __future__ import annotations

import pytest

from app.control import ERROR_CODES, ControlViolation
from app.control import completion, duties, period_lock, permissions, states

MAYA = {"user_id": 1, "role_code": "ROL-01", "is_active": 1}
ETHAN = {"user_id": 2, "role_code": "ROL-01", "is_active": 1}
PRIYA = {"user_id": 3, "role_code": "ROL-02", "is_active": 1}
DANIEL = {"user_id": 4, "role_code": "ROL-03", "is_active": 1}
FORMER = {"user_id": 5, "role_code": "ROL-02", "is_active": 0}


def rec(category="CAT-02", risk="low", source="ai", status="open", run_id=1, rec_id=10):
    return {"recommendation_id": rec_id, "run_id": run_id, "category_code": category,
            "risk_level": risk, "source": source, "status": status}


def refused(rule, call, *args, **kwargs) -> ControlViolation:
    """Run a check that must refuse, and return the violation for further assertions."""
    with pytest.raises(ControlViolation) as caught:
        call(*args, **kwargs)
    assert caught.value.rule == rule
    assert caught.value.code == ERROR_CODES[rule]
    return caught.value


# ---------------------------------------------------------------------------
# Permissions (DOC-02 section 2.1, CR-02, CR-16, FR-REV-04, FR-REV-10, CR-11)
# ---------------------------------------------------------------------------

def test_tcc_019_roles_outside_the_matrix_are_refused():
    refused("ROLE", permissions.require_role, DANIEL, permissions.IMPORT)
    refused("ROLE", permissions.require_role, MAYA, permissions.DECIDE_ADJUSTMENT)
    refused("ROLE", permissions.require_role, PRIYA, permissions.SIGN_OFF)
    refused("ROLE", permissions.require_role, PRIYA, permissions.DECIDE_REOPEN)
    refused("ROLE", permissions.require_role, FORMER, permissions.VIEW)
    permissions.require_role(DANIEL, permissions.SIGN_OFF)
    permissions.require_role(MAYA, permissions.REQUEST_REOPEN)


def test_tcc_020_cat_05_and_escalated_items_need_a_senior_decision():
    """CR-02: a Staff Accountant cannot decide either, whatever the action."""
    refused("CR-02", permissions.require_decision_permitted, MAYA,
            rec("CAT-05", "high", "rule"), "proposed", "approve")
    refused("CR-02", permissions.require_decision_permitted, MAYA, rec("CAT-02"), "escalated", "approve")
    assert permissions.require_decision_permitted(PRIYA, rec("CAT-05", "high"), "proposed", "approve") == "senior"
    assert permissions.require_decision_permitted(DANIEL, rec("CAT-02"), "escalated", "reject") == "senior"


def test_tcc_021_the_controller_does_not_make_first_level_decisions():
    refused("ROLE", permissions.require_decision_permitted, DANIEL, rec("CAT-02"), "proposed", "approve")
    assert permissions.require_decision_permitted(MAYA, rec("CAT-02"), "proposed", "approve") == "first"


def test_tcc_022_only_offered_actions_are_accepted():
    """FR-REV-04: e.g. an exact match cannot be 'modified', a senior cannot re-escalate."""
    refused("FR-REV-04", permissions.require_decision_permitted, MAYA,
            rec("CAT-01", source="rule"), "proposed", "modify")
    refused("FR-REV-04", permissions.require_decision_permitted, MAYA, rec("CAT-02"), "proposed", "unresolved")
    refused("FR-REV-04", permissions.require_decision_permitted, PRIYA, rec("CAT-05", "high"), "proposed", "escalate")
    assert permissions.offered_actions(rec("CAT-03"), "proposed") == {
        "approve", "reject", "modify", "escalate", "unresolved"}


def test_tcc_023_comment_rule():
    """CR-16: reject, modify, escalate, unresolved and High-risk approval need a comment."""
    for decision in ("reject", "modify", "escalate", "unresolved"):
        refused("CR-16", permissions.require_comment, decision, "low", None)
        refused("CR-16", permissions.require_comment, decision, "low", "   ")
    refused("CR-16", permissions.require_comment, "approve", "high", "")
    permissions.require_comment("approve", "low", None)
    permissions.require_comment("approve", "high", "Checked against the vendor invoice")


def test_tcc_024_non_exact_approval_requires_the_detail_view():
    """FR-REV-10: AI-scored items cannot be approved from the queue list."""
    refused("FR-REV-10", permissions.require_detail_opened, rec("CAT-02"), "approve", None)
    refused("FR-REV-10", permissions.require_detail_opened, rec("CAT-05", "high", "rule"), "modify", None)
    permissions.require_detail_opened(rec("CAT-01", source="rule"), "approve", None)
    permissions.require_detail_opened(rec("CAT-02"), "approve", "2026-09-08T16:00:00Z")
    permissions.require_detail_opened(rec("CAT-02"), "reject", None)


def test_tcc_025_batches_hold_only_open_low_risk_exact_matches():
    """CR-11: one ineligible item refuses the whole batch and is named in the details."""
    good = [rec("CAT-01", source="rule", rec_id=i) for i in (1, 2, 3)]
    statuses = {1: "proposed", 2: "proposed", 3: "proposed"}
    permissions.require_batch_eligible(1, good, statuses)

    violation = refused("CR-11", permissions.require_batch_eligible, 1,
                        good + [rec("CAT-02", rec_id=4)], {**statuses, 4: "proposed"})
    assert any("recommendation 4" in line for line in violation.details)
    refused("CR-11", permissions.require_batch_eligible, 1, [], {})
    refused("CR-11", permissions.require_batch_eligible, 1, good, {**statuses, 2: "approved"})
    refused("CR-11", permissions.require_batch_eligible, 2, good, statuses)


# ---------------------------------------------------------------------------
# Separation of duties (CR-03..CR-06)
# ---------------------------------------------------------------------------

def test_tcc_026_senior_approver_is_not_the_escalator_or_a_reviewer():
    refused("CR-03", duties.require_independent_senior, PRIYA["user_id"], PRIYA["user_id"], [])
    refused("CR-03", duties.require_independent_senior, PRIYA["user_id"], MAYA["user_id"], [PRIYA["user_id"]])
    duties.require_independent_senior(DANIEL["user_id"], PRIYA["user_id"], [MAYA["user_id"]])
    duties.require_independent_senior(PRIYA["user_id"], None, [])      # routed to CAT-05 by rule


def test_tcc_027_adjustment_approver_is_not_the_preparer():
    refused("CR-04", duties.require_independent_adjustment_approver, PRIYA["user_id"], PRIYA["user_id"])
    duties.require_independent_adjustment_approver(DANIEL["user_id"], PRIYA["user_id"])


def test_tcc_028_signer_is_not_the_only_first_level_reviewer():
    refused("CR-05", duties.require_independent_signer, DANIEL["user_id"], [DANIEL["user_id"]])
    duties.require_independent_signer(DANIEL["user_id"], [MAYA["user_id"], DANIEL["user_id"]])
    duties.require_independent_signer(DANIEL["user_id"], [MAYA["user_id"]])


def test_tcc_029_reopen_approver_is_not_the_requester():
    refused("CR-06", duties.require_independent_reopen_approver, DANIEL["user_id"], DANIEL["user_id"])
    duties.require_independent_reopen_approver(DANIEL["user_id"], MAYA["user_id"])


# ---------------------------------------------------------------------------
# State models (DOC-03)
# ---------------------------------------------------------------------------

def test_tcc_030_items_follow_the_doc_03_state_model():
    """In particular: nothing reaches 'reconciled' without passing 'report_verified'."""
    refused("STATE", states.require_item_transition, "proposed", "reconciled")
    refused("STATE", states.require_item_transition, "approved", "reconciled")
    refused("STATE", states.require_item_transition, "excluded", "proposed")
    for current, new in [("proposed", "approved"), ("proposed", "proposed"), ("escalated", "approved"),
                         ("approved", "report_verified"), ("report_verified", "reconciled"),
                         ("reconciled", "proposed"), ("unresolved", "proposed")]:
        states.require_item_transition(current, new)


def test_tcc_031_runs_follow_the_doc_03_state_model():
    refused("STATE", states.require_run_transition, "in_review", "closed")
    refused("STATE", states.require_run_transition, "closed", "in_review")
    for current, new in [("in_review", "ready_to_close"), ("ready_to_close", "closed"),
                         ("closed", "reopen_requested"), ("reopen_requested", "in_review"),
                         ("reopen_requested", "closed")]:
        states.require_run_transition(current, new)


# ---------------------------------------------------------------------------
# Period lock and stage gates (CR-17, CR-18)
# ---------------------------------------------------------------------------

def test_tcc_032_closed_or_reopen_pending_periods_refuse_changes():
    """CR-18: a pending reopen request does not unlock the period."""
    for status in ("closed", "reopen_requested"):
        refused("CR-18", period_lock.require_writable, {"status": status}, "record a decision")
        refused("CR-18", period_lock.require_importable, {"status": status})
    refused("STATE", period_lock.require_writable, {"status": "processing"}, "record a decision")
    period_lock.require_writable({"status": "ready_to_close"}, "record a decision")
    refused("CR-17", period_lock.require_matchable, {"status": "processing"}, True)
    refused("CR-17", period_lock.require_matchable, {"status": "validation_failed"}, False)
    period_lock.require_matchable({"status": "processing"}, False)


def test_tcc_033_reopen_requests_and_decisions_are_gated():
    closed = {"run_id": 1, "status": "closed"}
    refused("STATE", period_lock.require_reopen_requestable, {"run_id": 1, "status": "in_review"}, None)
    refused("STATE", period_lock.require_reopen_requestable, closed, {"reopen_request_id": 7})
    period_lock.require_reopen_requestable(closed, None)

    request = {"run_id": 1, "reopen_request_id": 7}
    refused("STATE", period_lock.require_reopen_decidable, closed, request, None)
    refused("STATE", period_lock.require_reopen_decidable,
            {"run_id": 1, "status": "reopen_requested"}, request, {"decision": "approve"})
    period_lock.require_reopen_decidable({"run_id": 1, "status": "reopen_requested"}, request, None)


# ---------------------------------------------------------------------------
# Close conditions (CR-10) and the six-condition rule (CR-07)
# ---------------------------------------------------------------------------

READY = period_lock.CloseFacts(items_awaiting_decision=0, escalations_open=0, adjustments_pending=0,
                               chain_intact=True, package_generated=True, approved_not_verified=0,
                               unresolved_difference_cents=0)


def test_tcc_034_close_reports_each_unmet_condition():
    """CR-10: the refusal lists every unmet condition, not just the first."""
    facts = period_lock.CloseFacts(items_awaiting_decision=3, escalations_open=1, adjustments_pending=0,
                                   chain_intact=False, package_generated=True, approved_not_verified=2,
                                   unresolved_difference_cents=93_235)
    conditions = period_lock.evaluate_close_conditions(facts)
    assert [c.met for c in conditions] == [False, False, True, False, False, False]
    violation = refused("CR-10", period_lock.require_close_ready, conditions)
    assert len(violation.details) == 5
    assert not period_lock.ready_for_signoff(conditions)


def test_tcu_157_a_difference_is_acceptable_only_with_the_signers_comment():
    from dataclasses import replace
    with_difference = replace(READY, unresolved_difference_cents=93_235)
    conditions = period_lock.evaluate_close_conditions(with_difference)
    assert period_lock.ready_for_signoff(conditions)          # 1..5 met: may proceed to sign-off
    refused("CR-10", period_lock.require_close_ready, conditions)
    period_lock.require_close_ready(period_lock.evaluate_close_conditions(
        replace(with_difference, signoff_comment="Two fees under investigation with the bank")))
    period_lock.require_close_ready(period_lock.evaluate_close_conditions(READY))


def _evidence(**overrides):
    values = dict(item_ref="BT-0412", validated=True, disposition_proposed=True, approval_recorded=True,
                  evidence_linked=True, audit_written=True, report_verified=True)
    values.update(overrides)
    return completion.ItemEvidence(**values)


def test_tcu_158_an_item_is_reconciled_only_when_all_six_conditions_hold():
    """CR-07 and brief section 14: each condition on its own blocks reconciliation."""
    assert completion.is_reconciled(_evidence())
    completion.require_reconciled(_evidence())
    for field in ("validated", "disposition_proposed", "approval_recorded",
                  "evidence_linked", "audit_written", "report_verified"):
        evidence = _evidence(**{field: False})
        assert not completion.is_reconciled(evidence)
        violation = refused("CR-07", completion.require_reconciled, evidence)
        assert len(violation.details) == 1


def test_tcu_159_approval_counts_only_if_it_is_the_latest_decision():
    approve = {"decision": "approve", "decision_level": "first"}
    escalate = {"decision": "escalate", "decision_level": "first"}
    senior = {"decision": "approve", "decision_level": "senior"}
    assert completion.approval_satisfied([approve], requires_senior=False)
    assert not completion.approval_satisfied([], requires_senior=False)
    assert not completion.approval_satisfied([approve, escalate], requires_senior=False)
    assert not completion.approval_satisfied([approve], requires_senior=True)
    assert completion.approval_satisfied([escalate, senior], requires_senior=True)
    assert completion.approval_satisfied([{"decision": "modify", "decision_level": "first"}], False)


def test_tcu_160_violations_name_rule_and_code():
    violation = ControlViolation("CR-04", "an adjustment must be approved by someone other than its preparer")
    assert str(violation).startswith("E-CTL-004 [CR-04]")
    with pytest.raises(ValueError):
        ControlViolation("CR-99", "no such rule")


def test_tcc_059_investigate_only_exceptions_are_never_approved():
    """Approving a duplicate, an unexplained item or a capped search would mark it reconciled
    while the money is still unexplained; the reviewer pairs, escalates or leaves it unresolved."""
    for code in ("EXC-05", "EXC-07", "EXC-08"):
        exception = {**rec("CAT-04", source="rule"), "kind": "exception", "exception_code": code}
        assert "approve" not in permissions.offered_actions(exception, "proposed")
        assert "unresolved" in permissions.offered_actions(exception, "proposed")
    for code in ("EXC-01", "EXC-02", "EXC-03", "EXC-04", "EXC-06"):
        exception = {**rec("CAT-04", source="rule"), "kind": "exception", "exception_code": code}
        assert "approve" in permissions.offered_actions(exception, "proposed")
    senior = {**rec("CAT-05", "high", "rule"), "kind": "exception", "exception_code": "EXC-07"}
    assert permissions.offered_actions(senior, "proposed") == {"unresolved"}
