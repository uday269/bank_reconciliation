"""Review service tests (TC-I-015..024, TC-C-035..042).

Each control test checks both halves of a refusal: the rule it names, and that the
only thing written was a BLOCKED_ATTEMPT event. Integration tests run real decisions
against the seeded August dataset and check statuses, records and the audit chain.
"""

from __future__ import annotations

import pytest

from app.control import ControlViolation
from app.services.review_service import ReviewService

OPENED = "2026-09-08T16:00:00Z"
DECIDED = "2026-09-08T16:01:30Z"


@pytest.fixture
def review(harness):
    harness.run_id = harness.matched_run()
    return ReviewService(harness.database, harness.repositories, harness.audit, harness.matching)


def user(harness, name):
    return harness.users[name]


def first(harness, category, kind=None, risk=None):
    rows = harness.repositories.recommendations.queue(harness.run_id, category)
    return next(row for row in rows
                if (kind is None or row["kind"] == kind) and (risk is None or row["risk_level"] == risk))


def item_status(harness, recommendation):
    return harness.repositories.items.get(recommendation["subject_item_type"],
                                          recommendation["subject_item_id"])["status"]


def events(harness, event_type):
    return [row for row in harness.audit.events(harness.run_id) if row["event_type"] == event_type]


def refused(harness, rule, call, *args, **kwargs):
    """Assert a refusal naming `rule`, with one BLOCKED_ATTEMPT event and no decision written."""
    decisions_before = len(harness.repositories.decisions.for_run(harness.run_id))
    blocked_before = len(events(harness, "BLOCKED_ATTEMPT"))
    with pytest.raises(ControlViolation) as caught:
        call(*args, **kwargs)
    assert caught.value.rule == rule
    assert len(harness.repositories.decisions.for_run(harness.run_id)) == decisions_before
    blocked = events(harness, "BLOCKED_ATTEMPT")
    assert len(blocked) == blocked_before + 1
    assert blocked[-1]["rule_name"] == rule
    return caught.value


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_tci_015_approving_a_scored_match_records_who_when_and_how_long(harness, review):
    """FR-REV-05 and FR-REV-11: identity, decision, timestamps and status change are recorded."""
    rec = first(harness, "CAT-02")
    maya = user(harness, "Maya Castillo")
    review.open_detail(rec["recommendation_id"], maya, at=OPENED)
    outcome = review.decide(rec["recommendation_id"], maya, "approve", at=DECIDED)

    assert (outcome.decision_level, outcome.previous_status, outcome.new_status) == ("first", "proposed", "approved")
    stored = harness.repositories.decisions.latest(rec["recommendation_id"])
    assert (stored["opened_at"], stored["decided_at"], stored["decided_by"]) == (OPENED, DECIDED, maya)
    assert harness.repositories.recommendations.get(rec["recommendation_id"])["status"] == "decided"
    leading = harness.repositories.recommendations.candidates(rec["recommendation_id"])[0]
    for member in harness.repositories.recommendations.candidate_members(leading["candidate_id"]):
        if member["ledger_entry_id"]:
            assert harness.repositories.items.get("ledger", member["ledger_entry_id"])["status"] == "approved"

    event = events(harness, "DECISION")[-1]
    assert (event["actor_user_id"], event["decision"], event["approval_status"]) == (maya, "approve", "approved")
    assert harness.audit.verify(harness.run_id).intact


def test_tci_016_rejecting_a_match_requeues_both_sides_as_exceptions(harness, review):
    """The rejected recommendation is kept, superseded; each released item gets a new one."""
    rec = first(harness, "CAT-01")
    outcome = review.decide(rec["recommendation_id"], user(harness, "Maya Castillo"), "reject",
                            comment="Reference belongs to a different invoice")

    assert harness.repositories.recommendations.get(rec["recommendation_id"])["status"] == "superseded"
    assert len(harness.repositories.recommendations.candidates(rec["recommendation_id"])) == 1
    assert len(outcome.requeued) == 2
    for new_id in outcome.requeued:
        new = harness.repositories.recommendations.get(new_id)
        assert (new["kind"], new["status"]) == ("exception", "open")
        assert new["category_code"] in ("CAT-04", "CAT-05")
        assert f"Released from recommendation {rec['recommendation_id']}" in new["explanation"]
        assert item_status(harness, new) == "proposed"


def test_tci_017_escalation_reaches_the_senior_queue_and_an_independent_senior_decides(harness, review):
    """FR-REV-08, CR-02, CR-03: escalate at first level, approve at senior level."""
    rec = first(harness, "CAT-03")
    rec_id = rec["recommendation_id"]
    ethan, priya = user(harness, "Ethan Brooks"), user(harness, "Priya Raman")
    review.decide(rec_id, ethan, "escalate", comment="Two invoices share this amount")

    assert item_status(harness, rec) == "escalated"
    assert rec_id in [row["recommendation_id"] for row in review.senior_queue(harness.run_id)]
    assert rec_id not in [row["recommendation_id"] for row in review.queue(harness.run_id, "CAT-03")]

    review.open_detail(rec_id, priya, at=OPENED)
    outcome = review.decide(rec_id, priya, "approve", at=DECIDED)
    assert (outcome.decision_level, outcome.previous_status, outcome.new_status) == ("senior", "escalated", "approved")
    assert harness.repositories.decisions.escalated_by(rec_id) == ethan


def test_tci_018_high_risk_items_are_decided_by_a_senior_with_a_comment(harness, review):
    rec = first(harness, "CAT-05", kind="match")
    priya = user(harness, "Priya Raman")
    review.open_detail(rec["recommendation_id"], priya)
    outcome = review.decide(rec["recommendation_id"], priya, "approve",
                            comment="Amount agreed to the signed purchase order")
    assert (outcome.decision_level, outcome.new_status) == ("senior", "approved")


def test_tci_019_modify_selects_another_candidate_once_it_is_free(harness, review):
    """FR-REV-07: the original recommendation and all its candidates are kept.

    In this dataset every alternative candidate is also proposed by another match, so the
    competing match is rejected first, which releases its records as exceptions. Selecting
    the alternative then supersedes those exception recommendations.
    """
    maya, priya = user(harness, "Maya Castillo"), user(harness, "Priya Raman")
    rec, alternative = _recommendation_with_alternative(harness, review)
    for holder_id in _competing_matches(harness, review, rec, alternative):
        # Priya can reject at either level, so a competing CAT-05 match is handled too.
        review.decide(holder_id, priya, "reject", comment="Pairing does not hold")

    # The released records are re-proposed; some land in CAT-05. Selecting the
    # alternative would settle them, so a Staff Accountant is refused (CR-02) ...
    review.open_detail(rec["recommendation_id"], maya)
    senior_needed = any(
        harness.repositories.recommendations.get(h)["category_code"] == "CAT-05"
        for key in review._members(alternative["candidate_id"], harness.database.connect())
        for h in [r["recommendation_id"] for r in harness.repositories.recommendations.open_holding(
            harness.run_id, *key) if r["kind"] == "exception"])
    if senior_needed:
        refused(harness, "CR-02", review.decide, rec["recommendation_id"], maya, "modify",
                comment="Second candidate agrees", chosen_candidate_id=alternative["candidate_id"])

    # ... and a Senior Accountant, independent of those items, may.
    review.open_detail(rec["recommendation_id"], priya)
    outcome = review.decide(rec["recommendation_id"], priya, "modify",
                            comment="Second candidate agrees to the remittance advice",
                            chosen_candidate_id=alternative["candidate_id"])

    assert outcome.new_status == "approved"
    assert outcome.superseded, "the released records' exception recommendations are superseded"
    for superseded_id in outcome.superseded:
        assert harness.repositories.recommendations.get(superseded_id)["status"] == "superseded"
    assert harness.repositories.decisions.latest(rec["recommendation_id"])["chosen_candidate_id"] \
        == alternative["candidate_id"]
    assert len(harness.repositories.recommendations.candidates(rec["recommendation_id"])) >= 2


def test_tci_020_unresolved_leaves_the_item_as_a_reconciling_difference(harness, review):
    rec = first(harness, "CAT-04")
    review.decide(rec["recommendation_id"], user(harness, "Maya Castillo"), "unresolved",
                  comment="Awaiting bank research")
    assert item_status(harness, rec) == "unresolved"
    assert harness.repositories.recommendations.get(rec["recommendation_id"])["status"] == "decided"


def test_tci_021_batch_approval_writes_one_decision_and_one_event_per_item(harness, review):
    """DD-02 (pending): effort is batched, the audit trail is per item."""
    ids = [row["recommendation_id"] for row in harness.repositories.recommendations.queue(harness.run_id, "CAT-01")[:25]]
    before = len(events(harness, "DECISION"))
    batch = review.approve_batch(harness.run_id, user(harness, "Maya Castillo"), ids)

    assert len(batch.outcomes) == 25
    assert harness.repositories.decisions.get_batch(batch.review_batch_id)["item_count"] == 25
    assert len(harness.repositories.decisions.for_batch(batch.review_batch_id)) == 25
    assert len(events(harness, "DECISION")) == before + 25
    assert all(o.new_status == "approved" for o in batch.outcomes)
    assert harness.audit.verify(harness.run_id).intact


def test_tci_022_batch_rejection_requeues_every_item(harness, review):
    ids = [row["recommendation_id"] for row in harness.repositories.recommendations.queue(harness.run_id, "CAT-01")[:3]]
    batch = review.reject_batch(harness.run_id, user(harness, "Ethan Brooks"), ids,
                                comment="Statement re-issued by the bank")
    assert sum(len(o.requeued) for o in batch.outcomes) == 6
    assert all(harness.repositories.recommendations.get(i)["status"] == "superseded" for i in ids)


def test_tci_023_detail_opened_is_recorded_per_user(harness, review):
    """FR-REV-10: one reviewer opening the item does not count for another."""
    rec = first(harness, "CAT-02")
    review.open_detail(rec["recommendation_id"], user(harness, "Ethan Brooks"))
    refused(harness, "FR-REV-10", review.decide, rec["recommendation_id"],
            user(harness, "Maya Castillo"), "approve")


def test_tci_024_every_decision_is_on_the_chain(harness, review):
    maya = user(harness, "Maya Castillo")
    for row in harness.repositories.recommendations.queue(harness.run_id, "CAT-04")[:3]:
        review.open_detail(row["recommendation_id"], maya)
        review.decide(row["recommendation_id"], maya, "approve")
    assert len(events(harness, "DECISION")) == 3
    assert harness.audit.verify(harness.run_id).intact


# ---------------------------------------------------------------------------
# Controls: refusal plus BLOCKED_ATTEMPT, nothing else written
# ---------------------------------------------------------------------------

def test_tcc_035_scored_match_cannot_be_approved_unseen(harness, review):
    rec = first(harness, "CAT-02")
    refused(harness, "FR-REV-10", review.decide, rec["recommendation_id"],
            user(harness, "Maya Castillo"), "approve")
    assert item_status(harness, rec) == "proposed"


def test_tcc_036_reject_requires_a_comment(harness, review):
    rec = first(harness, "CAT-01")
    refused(harness, "CR-16", review.decide, rec["recommendation_id"],
            user(harness, "Maya Castillo"), "reject", comment=" ")
    refused(harness, "CR-16", review.reject_batch, harness.run_id,
            user(harness, "Maya Castillo"), [rec["recommendation_id"]], None)


def test_tcc_037_staff_cannot_decide_high_risk_items(harness, review):
    rec = first(harness, "CAT-05")
    refused(harness, "CR-02", review.decide, rec["recommendation_id"],
            user(harness, "Maya Castillo"), "approve", comment="Looks right")


def test_tcc_038_the_escalator_cannot_approve_the_escalation(harness, review):
    """CR-03: Priya may escalate as a first-level reviewer, but then cannot decide it."""
    rec = first(harness, "CAT-03")
    priya = user(harness, "Priya Raman")
    review.decide(rec["recommendation_id"], priya, "escalate", comment="Needs a second view")
    review.open_detail(rec["recommendation_id"], priya)
    refused(harness, "CR-03", review.decide, rec["recommendation_id"], priya, "approve")
    daniel = user(harness, "Daniel Okafor")
    review.open_detail(rec["recommendation_id"], daniel)
    assert review.decide(rec["recommendation_id"], daniel, "approve").new_status == "approved"


def test_tcc_039_a_batch_with_one_ineligible_item_is_refused_whole(harness, review):
    """CR-11: nothing in the batch is approved, and the blocked event lists the items."""
    ids = [row["recommendation_id"] for row in harness.repositories.recommendations.queue(harness.run_id, "CAT-01")[:3]]
    ids.append(first(harness, "CAT-02")["recommendation_id"])
    refused(harness, "CR-11", review.approve_batch, harness.run_id, user(harness, "Maya Castillo"), ids)
    assert all(harness.repositories.recommendations.get(i)["status"] == "open" for i in ids)


def test_tcc_040_no_decisions_in_a_closed_period(harness, review):
    with harness.database.transaction() as connection:
        harness.repositories.runs.set_status(connection, harness.run_id, "closed")
    rec = first(harness, "CAT-01")
    refused(harness, "CR-18", review.decide, rec["recommendation_id"],
            user(harness, "Maya Castillo"), "approve")


def test_tcc_041_a_decided_item_cannot_be_decided_again(harness, review):
    rec = first(harness, "CAT-04")
    maya = user(harness, "Maya Castillo")
    review.open_detail(rec["recommendation_id"], maya)
    review.decide(rec["recommendation_id"], maya, "approve")
    refused(harness, "STATE", review.decide, rec["recommendation_id"], maya, "approve")


def test_tcc_042_exceptions_are_not_rejected_and_controllers_do_not_review(harness, review):
    """FR-REV-04 and the permission matrix."""
    rec = first(harness, "CAT-04")
    refused(harness, "FR-REV-04", review.decide, rec["recommendation_id"],
            user(harness, "Maya Castillo"), "reject", comment="Not a fee")
    refused(harness, "ROLE", review.decide, rec["recommendation_id"],
            user(harness, "Daniel Okafor"), "approve")


# ---------------------------------------------------------------------------
# helpers for the modify test
# ---------------------------------------------------------------------------

def _recommendation_with_alternative(harness, review):
    connection = harness.database.connect()
    for category in ("CAT-03", "CAT-02"):
        for rec in harness.repositories.recommendations.queue(harness.run_id, category):
            candidates = harness.repositories.recommendations.candidates(rec["recommendation_id"])
            if len(candidates) < 2:
                continue
            leading = set(review._members(candidates[0]["candidate_id"], connection))
            alternative = candidates[1]
            members = review._members(alternative["candidate_id"], connection)
            if any(key not in leading for key in members):
                return rec, alternative
    raise AssertionError("the dataset should contain a recommendation with an alternative candidate")


def _competing_matches(harness, review, rec, alternative):
    connection = harness.database.connect()
    holders = set()
    for key in review._members(alternative["candidate_id"], connection):
        for holder in harness.repositories.recommendations.open_holding(harness.run_id, *key):
            if holder["recommendation_id"] != rec["recommendation_id"] and holder["kind"] == "match":
                holders.add(holder["recommendation_id"])
    return sorted(holders)
