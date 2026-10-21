"""Reversing a decision (CR-19, FR-AUD-06): TC-I-086..089, TC-C-066..070.

A reversal never edits the decision it corrects. It adds a DECISION_REVERSED event that
references the decision and its audit event, marks the decided recommendation superseded,
and returns the records to review through a fresh copy. These tests check each half.
"""

from __future__ import annotations

import json

import pytest

from app.control import ControlViolation
from tests.web_support import build_services


@pytest.fixture
def web(tmp_path):
    return build_services(tmp_path)


def approved_exact(services, users, run_id, by="Maya Castillo"):
    """An exact match with low risk, approved at first level."""
    rec = next(r for r in services.review.queue(run_id, "CAT-01") if r["risk_level"] == "low")
    services.review.decide(rec["recommendation_id"], users[by], "approve")
    return services.repositories.recommendations.get(rec["recommendation_id"])


def members(services, rec):
    return services.review._leading(rec, services.database.connect())[1]


def events(services, run_id, event_type):
    return [e for e in services.audit.events(run_id) if e["event_type"] == event_type]


# ---------------------------------------------------------------------------
# What a reversal does
# ---------------------------------------------------------------------------

def test_tci_086_a_reversal_returns_the_records_to_review_and_keeps_the_decision(web):
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    keys = members(services, rec)
    assert {services.repositories.items.get(*k)["status"] for k in keys} == {"approved"}
    decision = services.repositories.decisions.latest(rec["recommendation_id"])

    outcome = services.review.reverse(rec["recommendation_id"], users["Priya Raman"], "Wrong invoice")

    assert {services.repositories.items.get(*k)["status"] for k in keys} == {"proposed"}
    assert services.repositories.recommendations.get(rec["recommendation_id"])["status"] == "superseded"
    copy = services.repositories.recommendations.get(outcome.new_recommendation_id)
    assert copy["status"] == "open" and copy["category_code"] == rec["category_code"]
    assert members(services, copy) == keys
    # the decision is untouched and still the only one on the original recommendation
    assert [dict(d) for d in services.repositories.decisions.for_recommendation(rec["recommendation_id"])] \
        == [dict(decision)]


def test_tci_087_the_reversal_event_references_what_it_corrects(web):
    """FR-AUD-06: a correction is a new event that names the corrected event."""
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    original = events(services, run_id, "DECISION")[-1]
    outcome = services.review.reverse(rec["recommendation_id"], users["Priya Raman"], "Wrong invoice")

    event = events(services, run_id, "DECISION_REVERSED")[-1]
    refs = json.loads(event["evidence_refs"])
    assert f"review_decision:{outcome.reversed_decision_id}" in refs
    assert f"audit_event:{original['sequence_no']}" in refs
    assert f"recommendation:{outcome.new_recommendation_id}" in refs
    assert event["actor_user_id"] == users["Priya Raman"] and event["comment"] == "Wrong invoice"
    assert json.loads(event["original_values"])["corrects_audit_event"] == original["sequence_no"]
    assert services.audit.verify(run_id).intact


def test_tci_088_the_reissued_item_can_be_decided_again(web):
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    outcome = services.review.reverse(rec["recommendation_id"], users["Priya Raman"], "Check again")
    services.review.decide(outcome.new_recommendation_id, users["Ethan Brooks"], "approve")
    copy = services.repositories.recommendations.get(outcome.new_recommendation_id)
    assert copy["status"] == "decided"
    assert {services.repositories.items.get(*k)["status"] for k in members(services, copy)} == {"approved"}


def test_tci_089_the_item_screen_offers_and_explains_a_reversal(web):
    from app.web import views
    from app.web.rendering import render
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    priya = services.repositories.users.get(users["Priya Raman"])
    maya = services.repositories.users.get(users["Maya Castillo"])

    page = views.item_detail(services, rec["recommendation_id"], priya)
    assert page["reverse"].allowed
    blocked = views.item_detail(services, rec["recommendation_id"], maya)["reverse"]
    assert not blocked.allowed and blocked.reason.startswith("Only a Senior Accountant or the Controller")

    outcome = services.review.reverse(rec["recommendation_id"], users["Priya Raman"], "Wrong invoice")
    old = views.item_detail(services, rec["recommendation_id"], priya)
    new = views.item_detail(services, outcome.new_recommendation_id, priya)
    assert old["replaced_by"] == outcome.new_recommendation_id and old["reverse"] is None
    assert new["replaces"]["recommendation_id"] == rec["recommendation_id"]
    html = render("item.html", **views.base(services, priya, new["run"], [], "x"), page=new)
    assert "This item was decided before" in html and "Wrong invoice" in html
    assert any(h["what"] == "Decision reversed" for h in old["history"])


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def refused(services, run_id, rule, *args):
    before = len(services.audit.events(run_id))
    with pytest.raises(ControlViolation) as caught:
        services.review.reverse(*args)
    assert caught.value.rule == rule
    written = services.audit.events(run_id)[before:]
    assert [e["event_type"] for e in written] == ["BLOCKED_ATTEMPT"]


def test_tcc_066_only_a_senior_or_the_controller_may_reverse(web):
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    refused(services, run_id, "ROLE", rec["recommendation_id"], users["Ethan Brooks"], "Wrong")


def test_tcc_067_the_person_who_decided_cannot_reverse_it(web):
    services, users, run_id = web
    rec = approved_exact(services, users, run_id, by="Priya Raman")
    refused(services, run_id, "CR-19", rec["recommendation_id"], users["Priya Raman"], "Changed my mind")
    services.review.reverse(rec["recommendation_id"], users["Daniel Okafor"], "Wrong pairing")


def test_tcc_068_a_reversal_needs_a_comment(web):
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    refused(services, run_id, "CR-16", rec["recommendation_id"], users["Priya Raman"], "  ")


def test_tcc_069_nothing_is_reversed_in_a_closed_period_or_twice(web):
    services, users, run_id = web
    rec = approved_exact(services, users, run_id)
    open_rec = next(r for r in services.review.queue(run_id, "CAT-02"))
    refused(services, run_id, "STATE", open_rec["recommendation_id"], users["Priya Raman"], "Not decided yet")
    services.review.reverse(rec["recommendation_id"], users["Priya Raman"], "Wrong")
    refused(services, run_id, "STATE", rec["recommendation_id"], users["Daniel Okafor"], "Again")

    other = approved_exact(services, users, run_id)
    with services.database.transaction() as connection:
        services.repositories.runs.set_status(connection, run_id, "closed")
    refused(services, run_id, "CR-18", other["recommendation_id"], users["Priya Raman"], "Too late")


def test_tcc_070_a_decision_behind_a_live_adjustment_is_not_reversed(web):
    services, users, run_id = web
    fee = next(r for r in services.review.queue(run_id, "CAT-04") if r["exception_code"] == "EXC-03")
    services.review.open_detail(fee["recommendation_id"], users["Maya Castillo"])
    services.review.decide(fee["recommendation_id"], users["Maya Castillo"], "approve")
    suggestion = services.adjustments.suggestion(fee["recommendation_id"])
    services.adjustments.propose(fee["recommendation_id"], users["Maya Castillo"],
                                 amount_cents=suggestion.amount_cents,
                                 debit_account_code=suggestion.debit_account_code,
                                 credit_account_code=suggestion.credit_account_code, rationale="Fee")
    refused(services, run_id, "STATE", fee["recommendation_id"], users["Priya Raman"], "Wrong")
