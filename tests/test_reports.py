"""Report package tests (TC-U-163, TC-I-037..040, TC-S-008, TC-C-060..061).

TC-S-008 is the end-to-end run the demonstration follows: a complete review of the August
data, the package, sign-off and the archived package. It ends with every item either
reconciled or unresolved, the statement at the dataset's difference, and the chain intact.
"""

from __future__ import annotations

import pytest

from app.control import permissions
from app.infra.reports.render import Header, ReportData, Table, render_csv, render_html
from app.services.adjustment_service import AdjustmentService
from app.services.period_service import PeriodService
from app.services.report_service import REPORT_CODES, ReportService
from app.services.review_service import ReviewService
from tests import test_manual_match as hand

COMMENT = "Agreed to the source document"


@pytest.fixture
def services(harness, tmp_path):
    harness.run_id = harness.matched_run()
    review = ReviewService(harness.database, harness.repositories, harness.audit, harness.matching)
    period = PeriodService(harness.database, harness.repositories, harness.audit)
    reports = ReportService(harness.database, harness.repositories, harness.audit, period,
                            "Bonneville Provisions Co.", tmp_path / "reports")
    adjustments = AdjustmentService(harness.database, harness.repositories, harness.audit)
    return review, period, reports, adjustments


def events(harness, event_type):
    return [row for row in harness.audit.events(harness.run_id) if row["event_type"] == event_type]


def complete_august_review(harness, review, adjustments):
    """The review a careful team would do: batch the exact matches, pair the missed groups,
    mark misclassified items unresolved, decide the rest, and prepare the adjustments."""
    run_id = harness.run_id
    maya, ethan, priya = (harness.users[n] for n in ("Maya Castillo", "Ethan Brooks", "Priya Raman"))
    review.approve_batch(run_id, maya, [row["recommendation_id"] for row in review.queue(run_id, "CAT-01")])

    for subject, counterparts in hand.missed_groups(harness):
        rec = hand.exception_for(harness, subject)
        review.open_detail(rec["recommendation_id"], priya)
        review.manual_match(rec["recommendation_id"], priya, counterparts, "Agreed to remittance advice")

    truth, _ = hand.keys_and_truth(harness)
    for key, label in truth.items():
        if label["exception_code"] in ("EXC-05", "EXC-07"):
            for rec in harness.repositories.recommendations.open_holding(run_id, *key):
                if rec["kind"] == "exception" and rec["exception_code"] not in ("EXC-05", "EXC-07"):
                    review.open_detail(rec["recommendation_id"], priya)
                    review.decide(rec["recommendation_id"], priya, "unresolved", comment="No source document")

    for category in ("CAT-02", "CAT-03", "CAT-04", "CAT-05"):
        rows = review.senior_queue(run_id) if category == "CAT-05" else review.queue(run_id, category)
        for row in rows:
            rec = harness.repositories.recommendations.get(row["recommendation_id"])
            if rec["status"] != "open":
                continue
            status = review._subject_status(rec)
            user = priya if permissions.decision_level(rec, status) == "senior" else ethan
            review.open_detail(rec["recommendation_id"], user)
            action = "approve" if "approve" in permissions.offered_actions(rec, status) else "unresolved"
            review.decide(rec["recommendation_id"], user, action, comment=COMMENT)

    for rec in harness.repositories.recommendations.for_run(run_id):
        if rec["exception_code"] in ("EXC-03", "EXC-04", "EXC-06") and rec["status"] == "decided":
            if harness.repositories.decisions.latest(rec["recommendation_id"])["decision"] != "approve":
                continue
            suggestion = adjustments.suggestion(rec["recommendation_id"])
            adjustment_id = adjustments.propose(
                rec["recommendation_id"], maya, amount_cents=suggestion.amount_cents,
                debit_account_code=suggestion.debit_account_code,
                credit_account_code=suggestion.credit_account_code, rationale=suggestion.rationale)
            adjustments.decide(adjustment_id, priya, "approve")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_tcu_163_rendering_escapes_text_and_hashes_content_only():
    report = ReportData("RPT-06", "Exception Report", "Purpose", [("Items", "1")],
                        [Table("Rows", ["Record", "Description"], [["BT-1", "<script>alert(1)</script>"]])])
    first = render_html(report, Header("Org", "Account", "Period", 1, "2026-09-10T00:00:00Z"))
    second = render_html(report, Header("Org", "Account", "Period", 1, "2026-09-11T00:00:00Z"))
    assert "<script>alert" not in first and "&lt;script&gt;" in first
    assert report.content_hash() in first and report.content_hash() in second
    assert render_csv(report).splitlines()[0] == "RPT-06,Exception Report"


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

def test_tci_037_the_package_holds_thirteen_reports_in_two_formats(harness, services):
    review, period, reports, _ = services
    result = reports.generate_package(harness.run_id)
    assert len(result.hashes) == 26
    assert {row["report_code"] for row in harness.repositories.reports.current(harness.run_id)} == set(REPORT_CODES)
    for code in REPORT_CODES:
        stem = code.lower().replace("-", "_")
        assert (result.directory / f"{stem}.html").exists() and (result.directory / f"{stem}.csv").exists()
    event = events(harness, "REPORT_GENERATED")[-1]
    assert "RPT-09.html:" + result.hashes["RPT-09.html"] in event["evidence_refs"]


def test_tci_038_unchanged_data_gives_identical_hashes(harness, services):
    """FR-RPT-04 and ADR-14: the timestamp is excluded, so the hashes repeat exactly.

    RPT-13 is the exception by nature: generating a package adds an event to the log it lists.
    """
    _, _, reports, _ = services
    first = reports.generate_package(harness.run_id, at="2026-09-10T10:00:00Z").hashes
    second = reports.generate_package(harness.run_id, at="2026-09-10T11:00:00Z").hashes
    stable = {name: digest for name, digest in first.items() if not name.startswith("RPT-13")}
    assert stable == {name: second[name] for name in stable}
    assert len(harness.repositories.reports.history(harness.run_id, "RPT-09")) == 4   # html + csv, twice


def test_tci_039_the_report_check_reconciles_approved_items(harness, services):
    """FR-RPT-05: approved items reach 'reconciled' only through the report check."""
    review, _, reports, _ = services
    batch = review.approve_batch(harness.run_id, harness.users["Maya Castillo"],
                                 [row["recommendation_id"] for row in review.queue(harness.run_id, "CAT-01")[:5]])
    result = reports.generate_package(harness.run_id)
    assert result.promoted == 10 and result.failures == []
    for outcome in batch.outcomes:
        rec = harness.repositories.recommendations.get(outcome.recommendation_id)
        assert harness.repositories.items.get(rec["subject_item_type"], rec["subject_item_id"])["status"] == "reconciled"
    assert len(events(harness, "REPORT_VERIFIED")) == 5
    assert harness.repositories.items.status_counts(harness.run_id, "bank").get("approved", 0) == 0


def test_tci_040_exports_are_recorded(harness, services):
    _, _, reports, _ = services
    reports.generate_package(harness.run_id)
    path = reports.export(harness.run_id, "RPT-09", "csv", harness.users["Daniel Okafor"])
    assert path.exists() and path.name == "rpt_09.csv"
    assert events(harness, "REPORT_EXPORTED")[-1]["actor_user_id"] == harness.users["Daniel Okafor"]
    with pytest.raises(FileNotFoundError):
        reports.export(harness.run_id, "RPT-09", "pdf", None)


def test_tcs_008_a_complete_reconciliation_closes_at_the_datasets_difference(harness, services):
    review, period, reports, adjustments = services
    complete_august_review(harness, review, adjustments)

    result = reports.generate_package(harness.run_id)
    assert result.failures == [] and result.run_status == "ready_to_close"
    readiness = period.readiness(harness.run_id)
    assert readiness.statement.unresolved_difference_cents == -939_235

    period.sign_off(harness.run_id, harness.users["Daniel Okafor"],
                    comment="Duplicates and unexplained items are under bank research")
    archive = reports.generate_package(harness.run_id)
    assert archive.run_status == "closed" and archive.promoted == 0

    statuses = {}
    for item_type in ("bank", "ledger", "carry_in"):
        for status, n in harness.repositories.items.status_counts(harness.run_id, item_type).items():
            statuses[status] = statuses.get(status, 0) + n
    assert set(statuses) <= {"reconciled", "unresolved", "excluded"}
    signoff = harness.repositories.periods.latest_signoff(harness.run_id)
    summary = (archive.directory / "rpt_09.html").read_text()
    assert signoff["chain_head_hash"] in summary and "-9392.35" in summary
    assert harness.audit.verify(harness.run_id).intact


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def test_tcc_060_an_item_missing_from_the_report_is_not_reconciled(harness, services):
    """CR-07 condition 6: the report check names the unmet condition and changes nothing."""
    review, _, reports, _ = services
    rec = review.queue(harness.run_id, "CAT-01")[0]
    review.decide(rec["recommendation_id"], harness.users["Maya Castillo"], "approve")
    promoted, failures = reports._verify_and_reconcile(harness.run_id, {}, None, "2026-09-10T10:00:00Z")
    assert promoted == 0 and len(failures) == 2
    assert all("appears correctly in the reconciliation report" in line for line in failures)
    assert harness.repositories.items.get(rec["subject_item_type"], rec["subject_item_id"])["status"] == "approved"


def test_tcc_061_a_closed_period_package_changes_no_item(harness, services):
    review, period, reports, adjustments = services
    complete_august_review(harness, review, adjustments)
    reports.generate_package(harness.run_id)
    period.sign_off(harness.run_id, harness.users["Daniel Okafor"], comment="Under bank research")
    before = harness.repositories.items.status_counts(harness.run_id, "bank")
    reports.generate_package(harness.run_id)
    assert harness.repositories.items.status_counts(harness.run_id, "bank") == before
