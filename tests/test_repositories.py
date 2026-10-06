"""Repository tests for Stage 8 (TC-I-008..014, TC-C-015..018).

Two things are pinned here. First, a status change reads the previous status on the
caller's connection, so a control check and the write it guards see the same
uncommitted state; the tests prove it with a second connection to the same file.
Second, the review, adjustment, period and report repositories store what the
services will need, and the evidence tables they write stay append-only.
"""

from __future__ import annotations

import pytest

from app.infra.db import AppendOnlyViolation, Database, DatabaseError, PeriodLockedError, update
from app.infra.repositories import Repositories, RepositoryError

NOW = "2026-09-08T16:00:00Z"
LATER = "2026-09-08T16:05:00Z"


def _second_connection_repositories(harness) -> Repositories:
    """Repositories backed by a different connection to the same database file."""
    other = Database(harness.database.path, harness.database.schema_path)
    other.connect()
    return Repositories.for_database(other)


def _first_open(harness, run_id: int, category: str = "CAT-01"):
    return harness.repositories.recommendations.queue(run_id, category)[0]


# ---------------------------------------------------------------------------
# The connection fix
# ---------------------------------------------------------------------------

def test_tci_008_status_changes_read_the_previous_status_on_the_callers_connection(harness):
    """The second change in one transaction must report the first, uncommitted change.

    The repositories here hold a different connection from the one writing. Before
    the fix they read the committed status from their own connection and returned
    the wrong previous status; now they read on the caller's connection.
    """
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id)
    item_type = recommendation["subject_item_type"]
    item_id = recommendation["subject_item_id"]
    other = _second_connection_repositories(harness)

    with harness.database.transaction() as connection:
        assert other.runs.set_status(connection, run_id, "ready_to_close") == "in_review"
        assert other.runs.set_status(connection, run_id, "in_review") == "ready_to_close"

        assert other.items.set_status(connection, item_type, item_id, "approved") == "proposed"
        assert other.items.set_status(connection, item_type, item_id, "proposed") == "approved"

        rec_id = recommendation["recommendation_id"]
        assert other.recommendations.set_status(connection, rec_id, "decided") == "open"
        assert other.recommendations.set_status(connection, rec_id, "open") == "decided"


def test_tci_009_reads_given_a_connection_see_uncommitted_rows(harness):
    """A control check inside a transaction must see what that transaction wrote."""
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id, "CAT-04")
    maya = harness.users["Maya Castillo"]
    adjustments = harness.repositories.adjustments

    with harness.database.transaction() as connection:
        adjustment_id = adjustments.create(
            connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
            amount_cents=2500, debit_account_code="6810", credit_account_code="1010",
            rationale="Monthly service charge", prepared_by=maya, prepared_at=NOW)
        assert adjustments.get(adjustment_id, connection)["status"] == "proposed"
        assert harness.repositories.runs.is_closed(run_id, connection) is False

    with pytest.raises(RepositoryError):
        adjustments.get(adjustment_id + 999)


# ---------------------------------------------------------------------------
# Decisions and batches
# ---------------------------------------------------------------------------

def test_tci_010_decision_history_escalator_and_deciders(harness):
    """CR-03 and CR-05 depend on who escalated and who decided; both must be recoverable."""
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id, "CAT-03")
    rec_id = recommendation["recommendation_id"]
    maya, priya = harness.users["Maya Castillo"], harness.users["Priya Raman"]
    decisions = harness.repositories.decisions

    with harness.database.transaction() as connection:
        decisions.add(connection, run_id=run_id, recommendation_id=rec_id, decision="escalate",
                      decision_level="first", decided_by=maya, decided_at=NOW,
                      previous_status="proposed", new_status="escalated",
                      comment="Reference differs from the remittance", opened_at=NOW)
        assert decisions.escalated_by(rec_id, connection) == maya
        decisions.add(connection, run_id=run_id, recommendation_id=rec_id, decision="approve",
                      decision_level="senior", decided_by=priya, decided_at=LATER,
                      previous_status="escalated", new_status="approved",
                      comment="Confirmed with the customer remittance", opened_at=LATER)

    history = decisions.for_recommendation(rec_id)
    assert [row["decision"] for row in history] == ["escalate", "approve"]
    assert decisions.latest(rec_id)["decision"] == "approve"
    assert decisions.escalated_by(rec_id) == maya
    assert decisions.deciders(run_id) == {maya, priya}
    assert decisions.deciders(run_id, "first") == {maya}
    assert decisions.counts(run_id) == {"escalate": 1, "approve": 1}
    assert decisions.escalated_by(_first_open(harness, run_id)["recommendation_id"]) is None


def test_tci_011_a_batch_writes_one_decision_per_item(harness):
    """DD-02 (pending): one reviewer action, one decision record per item."""
    run_id = harness.matched_run()
    items = harness.repositories.recommendations.queue(run_id, "CAT-01")[:3]
    maya = harness.users["Maya Castillo"]
    decisions = harness.repositories.decisions

    with harness.database.transaction() as connection:
        batch_id = decisions.create_batch(connection, run_id, maya, NOW, len(items))
        for row in items:
            decisions.add(connection, run_id=run_id, recommendation_id=row["recommendation_id"],
                          decision="approve", decision_level="first", decided_by=maya,
                          decided_at=NOW, previous_status="proposed", new_status="approved",
                          review_batch_id=batch_id)

    assert decisions.get_batch(batch_id)["item_count"] == 3
    written = decisions.for_batch(batch_id)
    assert len(written) == 3
    assert {row["recommendation_id"] for row in written} == {r["recommendation_id"] for r in items}


# ---------------------------------------------------------------------------
# Adjustments, period control, reports
# ---------------------------------------------------------------------------

def test_tci_012_adjustment_proposal_decision_and_totals(harness):
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id, "CAT-04")
    maya, priya = harness.users["Maya Castillo"], harness.users["Priya Raman"]
    adjustments = harness.repositories.adjustments

    with harness.database.transaction() as connection:
        first = adjustments.create(
            connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
            amount_cents=2500, debit_account_code="6810", credit_account_code="1010",
            rationale="Monthly service charge", prepared_by=maya, prepared_at=NOW,
            evidence_refs="BT-0412")
        second = adjustments.create(
            connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
            amount_cents=900, debit_account_code="6820", credit_account_code="1010",
            rationale="Returned item fee", prepared_by=maya, prepared_at=NOW)
        adjustments.add_decision(connection, adjustment_id=first, decision="approve",
                                 decided_by=priya, decided_at=LATER)
        assert adjustments.set_status(connection, first, "approved") == "proposed"

    assert [row["decision"] for row in adjustments.decisions(first)] == ["approve"]
    assert adjustments.decisions(second) == []
    assert len(adjustments.for_recommendation(recommendation["recommendation_id"])) == 2
    assert [row["adjustment_id"] for row in adjustments.for_run(run_id, ["proposed"])] == [second]
    assert adjustments.totals_cents(run_id) == {"proposed": 900, "approved": 2500, "rejected": 0}


def test_tci_013_signoff_and_reopen_records(harness):
    """A request stays pending until decided; a re-close keeps the earlier sign-off."""
    run_id = harness.matched_run()
    priya, daniel = harness.users["Priya Raman"], harness.users["Daniel Okafor"]
    periods = harness.repositories.periods

    with harness.database.transaction() as connection:
        periods.add_signoff(connection, run_id=run_id, signed_by=daniel, signed_at=NOW,
                            bank_ending_balance_cents=100_000, book_ending_balance_cents=100_000,
                            chain_head_hash="a" * 64)
        request_id = periods.add_reopen_request(connection, run_id=run_id, requested_by=priya,
                                                reason="Late bank correction", requested_at=NOW,
                                                original_status="closed")
        assert periods.pending_reopen_request(run_id, connection)["reopen_request_id"] == request_id

    assert periods.reopen_decision(request_id) is None
    with harness.database.transaction() as connection:
        periods.add_reopen_decision(connection, reopen_request_id=request_id, decision="approve",
                                    decided_by=daniel, decided_at=LATER, revised_status="in_review")
        periods.add_signoff(connection, run_id=run_id, signed_by=daniel, signed_at=LATER,
                            bank_ending_balance_cents=100_000, book_ending_balance_cents=99_100,
                            unresolved_difference_cents=900, unresolved_item_count=1,
                            comment="Fee under investigation", chain_head_hash="b" * 64)

    assert periods.pending_reopen_request(run_id) is None
    assert periods.get_reopen_request(request_id)["original_status"] == "closed"
    history = periods.reopen_history(run_id)
    assert len(history) == 1 and history[0]["revised_status"] == "in_review"
    assert len(periods.signoffs(run_id)) == 2
    assert periods.latest_signoff(run_id)["chain_head_hash"] == "b" * 64


def test_tci_014_regenerated_reports_keep_history(harness):
    """ADR-14: the newest row per code and format is current; earlier rows remain."""
    run_id = harness.matched_run()
    reports = harness.repositories.reports

    with harness.database.transaction() as connection:
        reports.add(connection, run_id=run_id, report_code="RPT-09", output_format="html",
                    file_path="reports/run_1/rpt_09.html", content_sha256="1" * 64, generated_at=NOW)
        reports.add(connection, run_id=run_id, report_code="RPT-02", output_format="csv",
                    file_path="reports/run_1/rpt_02.csv", content_sha256="2" * 64, generated_at=NOW)
        newest = reports.add(connection, run_id=run_id, report_code="RPT-09", output_format="html",
                             file_path="reports/run_1/rpt_09.html", content_sha256="3" * 64,
                             generated_at=LATER)

    current = reports.current(run_id)
    assert [(row["report_code"], row["output_format"]) for row in current] == [
        ("RPT-02", "csv"), ("RPT-09", "html")]
    assert next(row for row in current if row["report_code"] == "RPT-09")["report_id"] == newest
    assert len(reports.history(run_id, "RPT-09")) == 2
    assert reports.get(newest)["content_sha256"] == "3" * 64


# ---------------------------------------------------------------------------
# Controls enforced underneath the repositories
# ---------------------------------------------------------------------------

def test_tcc_015_stage_8_evidence_tables_are_append_only(harness):
    """CR-09: decisions, adjustment decisions, sign-offs and reopen records cannot be edited."""
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id, "CAT-04")
    maya, priya, daniel = (harness.users[name] for name in
                           ("Maya Castillo", "Priya Raman", "Daniel Okafor"))
    r = harness.repositories

    with harness.database.transaction() as connection:
        r.decisions.add(connection, run_id=run_id,
                        recommendation_id=recommendation["recommendation_id"], decision="approve",
                        decision_level="first", decided_by=maya, decided_at=NOW,
                        previous_status="proposed", new_status="approved")
        adjustment_id = r.adjustments.create(
            connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
            amount_cents=2500, debit_account_code="6810", credit_account_code="1010",
            rationale="Service charge", prepared_by=maya, prepared_at=NOW)
        r.adjustments.add_decision(connection, adjustment_id=adjustment_id, decision="approve",
                                   decided_by=priya, decided_at=NOW)
        r.periods.add_signoff(connection, run_id=run_id, signed_by=daniel, signed_at=NOW,
                              bank_ending_balance_cents=1, book_ending_balance_cents=1,
                              chain_head_hash="c" * 64)
        request_id = r.periods.add_reopen_request(connection, run_id=run_id, requested_by=priya,
                                                  reason="Correction", requested_at=NOW,
                                                  original_status="closed")
        r.periods.add_reopen_decision(connection, reopen_request_id=request_id, decision="reject",
                                      decided_by=daniel, decided_at=NOW, revised_status="closed")

    for table, column in [("review_decision", "comment"), ("adjustment_decision", "comment"),
                          ("period_signoff", "comment"), ("reopen_request", "reason"),
                          ("reopen_decision", "comment")]:
        with pytest.raises(AppendOnlyViolation):
            with harness.database.transaction() as connection:
                update(connection, table, {column: "edited"}, "1 = 1", ())


def test_tcc_016_signoff_with_a_difference_requires_a_comment(harness):
    """CR-10: a non-zero unresolved difference cannot be signed without explanation."""
    run_id = harness.matched_run()
    with pytest.raises(DatabaseError):
        with harness.database.transaction() as connection:
            harness.repositories.periods.add_signoff(
                connection, run_id=run_id, signed_by=harness.users["Daniel Okafor"],
                signed_at=NOW, bank_ending_balance_cents=100_000,
                book_ending_balance_cents=99_100, unresolved_difference_cents=900,
                chain_head_hash="d" * 64)
    assert harness.repositories.periods.signoffs(run_id) == []


def test_tcc_017_no_adjustment_or_decision_in_a_closed_period(harness):
    """FR-PER-03 and CR-18: once closed, only a reopening can change anything."""
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id, "CAT-04")
    maya = harness.users["Maya Castillo"]
    with harness.database.transaction() as connection:
        harness.repositories.runs.set_status(connection, run_id, "closed")

    with pytest.raises(PeriodLockedError):
        with harness.database.transaction() as connection:
            harness.repositories.adjustments.create(
                connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
                amount_cents=2500, debit_account_code="6810", credit_account_code="1010",
                rationale="Service charge", prepared_by=maya, prepared_at=NOW)
    with pytest.raises(PeriodLockedError):
        with harness.database.transaction() as connection:
            harness.repositories.decisions.add(
                connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
                decision="approve", decision_level="first", decided_by=maya, decided_at=NOW,
                previous_status="proposed", new_status="approved")


def test_tcc_018_an_adjustment_cannot_debit_and_credit_the_same_account(harness):
    """Schema guard on proposed adjustments: a one-account entry is not an adjustment."""
    run_id = harness.matched_run()
    recommendation = _first_open(harness, run_id, "CAT-04")
    with pytest.raises(DatabaseError):
        with harness.database.transaction() as connection:
            harness.repositories.adjustments.create(
                connection, run_id=run_id, recommendation_id=recommendation["recommendation_id"],
                amount_cents=2500, debit_account_code="1010", credit_account_code="1010",
                rationale="Invalid", prepared_by=harness.users["Maya Castillo"], prepared_at=NOW)
