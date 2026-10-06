"""Statement and period control tests (TC-U-161..162, TC-I-030..034, TC-C-049..053).

Closing needs a generated report package and verified items, which the report service
produces. Until it exists, `complete_review` stands in for it: it records first-level
decisions, marks items reconciled and registers a package, so these tests exercise the
period controls on their own. The full end-to-end close is tested with the report service.
"""

from __future__ import annotations

import csv

import pytest

from app.control import ControlViolation
from app.domain.exceptions import statement_section
from app.domain.rules import Item
from app.domain.statement import StatementLine, build_statement, money
from app.infra.audit import AuditEvent, RunContext
from app.services.period_service import PeriodInputError, PeriodService
from tests.conftest import DATASET

AT = "2026-09-10T17:00:00Z"
EXTERNAL = {"bank": "external_txn_id", "ledger": "external_entry_id", "carry_in": "external_item_id"}


@pytest.fixture
def period(harness):
    harness.run_id = harness.matched_run()
    return PeriodService(harness.database, harness.repositories, harness.audit)


def events(harness, event_type):
    return [row for row in harness.audit.events(harness.run_id) if row["event_type"] == event_type]


def register_package(harness):
    """Stand-in for the report service: 13 current reports and a REPORT_GENERATED event."""
    with harness.database.transaction() as connection:
        for number in range(1, 14):
            harness.repositories.reports.add(
                connection, run_id=harness.run_id, report_code=f"RPT-{number:02d}", output_format="html",
                file_path=f"reports/run/rpt_{number:02d}.html", content_sha256="0" * 64, generated_at=AT)
        harness.audit.write(connection, RunContext.load(harness.database, harness.run_id),
                            AuditEvent(event_type="REPORT_GENERATED", process_code="P8",
                                       entity_type="reconciliation_run", entity_id=harness.run_id))


def complete_review(harness, reviewer="Maya Castillo"):
    """Stand-in for a finished review: decisions recorded, items reconciled, package current."""
    repos = harness.repositories
    with harness.database.transaction() as connection:
        for row in repos.recommendations.queue(harness.run_id, "CAT-01")[:2]:
            repos.decisions.add(connection, run_id=harness.run_id,
                                recommendation_id=row["recommendation_id"], decision="approve",
                                decision_level="first", decided_by=harness.users[reviewer],
                                decided_at=AT, previous_status="proposed", new_status="approved")
        for item_type in ("bank", "ledger", "carry_in"):
            key = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id",
                   "carry_in": "carry_in_item_id"}[item_type]
            for row in repos.items.list(harness.run_id, item_type):
                if row["status"] != "excluded":
                    repos.items.set_status(connection, item_type, row[key], "reconciled")
    register_package(harness)


def refused(harness, rule, call, *args, **kwargs):
    blocked_before = len(events(harness, "BLOCKED_ATTEMPT"))
    with pytest.raises(ControlViolation) as caught:
        call(*args, **kwargs)
    assert caught.value.rule == rule
    blocked = events(harness, "BLOCKED_ATTEMPT")
    assert len(blocked) == blocked_before + 1 and blocked[-1]["rule_name"] == rule
    return caught.value


# ---------------------------------------------------------------------------
# Statement (BR-17, DD-12)
# ---------------------------------------------------------------------------

def test_tcu_161_statement_arithmetic():
    lines = [StatementLine("GL-1", "deposits_in_transit", 5_000, 5_000, "EXC-02", "", "2026-08-30", "approved"),
             StatementLine("GL-2", "outstanding_checks", 12_000, -12_000, "EXC-01", "", "2026-08-29", "approved"),
             StatementLine("BT-1", "bank_originated", 2_500, -2_500, "EXC-03", "", "2026-08-31", "approved"),
             StatementLine("BT-2", "bank_originated", 400, 400, "EXC-04", "", "2026-08-31", "approved"),
             StatementLine("BT-3", "unresolved", 900, -900, "EXC-07", "", "2026-08-15", "unresolved")]
    statement = build_statement(100_000, 99_000, lines)
    assert statement.adjusted_bank_balance_cents == 100_000 + 5_000 - 12_000
    assert statement.adjusted_book_balance_cents == 99_000 + 400 - 2_500
    assert statement.unresolved_difference_cents == 93_000 - 96_900
    assert statement.unresolved_item_count == 1
    assert money(-123_456) == "-$1,234.56"
    with pytest.raises(ValueError):
        build_statement(0, 0, [StatementLine("X", "elsewhere", 1, 1, None, "", "", "")])


def test_tci_030_the_statement_ties_to_the_dataset_when_dispositions_are_correct(harness, period):
    """With ground-truth dispositions, the statement reproduces the generator's difference.

    This checks the BR-17 arithmetic and the use of source balances against an
    independent calculation, separately from how well the system classified items.
    """
    truth = {(row["item_type"], row["item_id"]): row
             for row in csv.DictReader((DATASET / "ground_truth.csv").open())}
    lines = []
    for item_type in ("bank", "ledger", "carry_in"):
        for row in harness.repositories.items.list(harness.run_id, item_type):
            label = truth.get((item_type, row[EXTERNAL[item_type]]))
            if row["status"] == "excluded" or not label or not label["exception_code"]:
                continue
            lines.append(StatementLine(row[EXTERNAL[item_type]],
                                       statement_section(label["exception_code"]) or "unresolved",
                                       row["amount_cents"], Item.from_row(item_type, row).cash_effect,
                                       label["exception_code"], "", "", ""))
    system = period.statement(harness.run_id)
    expected = build_statement(system.bank_ending_balance_cents, system.book_ending_balance_cents, lines)
    assert expected.unresolved_difference_cents == -939_235


def test_tci_031_readiness_names_each_unmet_condition(harness, period):
    readiness = period.readiness(harness.run_id)
    unmet = [c.number for c in readiness.conditions if not c.met]
    assert 1 in unmet and 5 in unmet
    assert period.refresh_status(harness.run_id) == "in_review"
    assert events(harness, "STATUS_CHANGED") == []


# ---------------------------------------------------------------------------
# Sign-off, lock and reopening
# ---------------------------------------------------------------------------

def test_tci_032_sign_off_records_statement_and_chain_head_then_locks(harness, period):
    complete_review(harness)
    assert period.refresh_status(harness.run_id) == "ready_to_close"
    statement = period.statement(harness.run_id)
    head_before = harness.audit.head_hash(harness.run_id)

    signoff_id = period.sign_off(harness.run_id, harness.users["Daniel Okafor"],
                                 comment="Difference relates to items under bank research", at=AT)
    signoff = harness.repositories.periods.latest_signoff(harness.run_id)
    assert signoff["period_signoff_id"] == signoff_id
    assert signoff["chain_head_hash"] == head_before
    assert signoff["unresolved_difference_cents"] == statement.unresolved_difference_cents
    assert signoff["outstanding_checks_cents"] == statement.outstanding_checks_cents
    assert harness.repositories.runs.get(harness.run_id)["status"] == "closed"
    assert [e["event_type"] for e in harness.audit.events(harness.run_id)[-2:]] == ["SIGNOFF", "PERIOD_LOCKED"]
    assert harness.audit.verify(harness.run_id).intact


def test_tci_033_reopen_then_a_new_sign_off_is_required(harness, period):
    """FR-PER-04 and FR-PER-05: both sign-offs and both states stay on record."""
    complete_review(harness)
    period.refresh_status(harness.run_id)
    period.sign_off(harness.run_id, harness.users["Daniel Okafor"], comment="Under research")

    request_id = period.request_reopen(harness.run_id, harness.users["Priya Raman"],
                                       reason="Bank issued a corrected fee notice")
    assert harness.repositories.runs.get(harness.run_id)["status"] == "reopen_requested"
    assert period.decide_reopen(request_id, harness.users["Daniel Okafor"], "approve",
                                comment="Correct the fee before re-close") == "in_review"

    assert period.refresh_status(harness.run_id) == "in_review"     # package predates the reopening
    register_package(harness)
    assert period.refresh_status(harness.run_id) == "ready_to_close"
    period.sign_off(harness.run_id, harness.users["Daniel Okafor"], comment="Fee corrected; rest under research")

    assert len(harness.repositories.periods.signoffs(harness.run_id)) == 2
    history = harness.repositories.periods.reopen_history(harness.run_id)
    assert (history[0]["original_status"], history[0]["revised_status"]) == ("closed", "in_review")


def test_tci_034_a_rejected_reopening_leaves_the_period_closed(harness, period):
    complete_review(harness)
    period.refresh_status(harness.run_id)
    period.sign_off(harness.run_id, harness.users["Daniel Okafor"], comment="Under research")
    request_id = period.request_reopen(harness.run_id, harness.users["Maya Castillo"], reason="Typo in a memo")
    assert period.decide_reopen(request_id, harness.users["Daniel Okafor"], "reject",
                                comment="Memo text does not affect the reconciliation") == "closed"
    assert harness.repositories.periods.pending_reopen_request(harness.run_id) is None


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def test_tcc_049_sign_off_is_refused_until_every_condition_holds(harness, period):
    """CR-10: the refusal lists each unmet condition."""
    violation = refused(harness, "CR-10", period.sign_off, harness.run_id,
                        harness.users["Daniel Okafor"], comment="Early")
    assert any(line.startswith("1.") for line in violation.details)
    assert harness.repositories.periods.signoffs(harness.run_id) == []


def test_tcc_050_a_difference_needs_the_signers_comment(harness, period):
    complete_review(harness)
    period.refresh_status(harness.run_id)
    assert period.statement(harness.run_id).unresolved_difference_cents != 0
    refused(harness, "CR-10", period.sign_off, harness.run_id, harness.users["Daniel Okafor"])
    assert harness.repositories.runs.get(harness.run_id)["status"] == "ready_to_close"


def test_tcc_051_only_an_independent_controller_signs(harness, period):
    """Permission matrix, then CR-05 as defence in depth.

    A Controller cannot make first-level decisions through the service, so CR-05 can only
    be reached if that rule were bypassed; the decision is written directly to prove the
    sign-off check still holds on its own.
    """
    complete_review(harness, reviewer="Daniel Okafor")
    period.refresh_status(harness.run_id)
    refused(harness, "ROLE", period.sign_off, harness.run_id, harness.users["Priya Raman"], comment="x")
    refused(harness, "CR-05", period.sign_off, harness.run_id, harness.users["Daniel Okafor"], comment="x")


def test_tcc_052_reopening_needs_the_right_roles_and_a_reason(harness, period):
    """A Controller cannot request and staff cannot approve, so CR-06 holds through roles."""
    complete_review(harness)
    period.refresh_status(harness.run_id)
    period.sign_off(harness.run_id, harness.users["Daniel Okafor"], comment="Under research")

    refused(harness, "ROLE", period.request_reopen, harness.run_id, harness.users["Daniel Okafor"], "Fix")
    with pytest.raises(PeriodInputError):
        period.request_reopen(harness.run_id, harness.users["Maya Castillo"], "   ")
    request_id = period.request_reopen(harness.run_id, harness.users["Maya Castillo"], "Late correction")
    refused(harness, "ROLE", period.decide_reopen, request_id, harness.users["Maya Castillo"], "approve")
    refused(harness, "CR-16", period.decide_reopen, request_id, harness.users["Daniel Okafor"], "reject")


def test_tcc_053_reopening_only_from_closed_and_only_once_at_a_time(harness, period):
    refused(harness, "STATE", period.request_reopen, harness.run_id, harness.users["Maya Castillo"], "Early")
    complete_review(harness)
    period.refresh_status(harness.run_id)
    period.sign_off(harness.run_id, harness.users["Daniel Okafor"], comment="Under research")
    request_id = period.request_reopen(harness.run_id, harness.users["Maya Castillo"], "Late correction")
    refused(harness, "STATE", period.request_reopen, harness.run_id, harness.users["Priya Raman"], "Again")
    period.decide_reopen(request_id, harness.users["Daniel Okafor"], "reject", comment="Not needed")
    refused(harness, "STATE", period.decide_reopen, request_id, harness.users["Daniel Okafor"], "approve")
