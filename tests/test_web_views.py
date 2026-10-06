"""Web view and page tests that need no server (TC-I-041..045).

They build each screen's data with the view functions and render it with the real
templates, so a missing value or a broken template fails here, without FastAPI.
The route tests in test_web_routes.py cover the HTTP layer.
"""

from __future__ import annotations

import pytest

from app.control import permissions
from app.web import views
from app.web.rendering import money, render
from tests.web_support import build_services


@pytest.fixture
def web(tmp_path):
    return build_services(tmp_path)


def rendered(services, user, run, template, title, **context):
    return render(template, **views.base(services, user, run, [], title), **context)


def test_tci_041_the_dashboard_shows_the_job_queues_and_close_conditions(web):
    services, users, run_id = web
    maya = services.repositories.users.get(users["Maya Castillo"])
    data = views.dashboard(services, None, maya)
    assert data["run"]["run_id"] == run_id
    assert [q["code"] for q in data["queues"]] == ["CAT-01", "CAT-02", "CAT-03", "CAT-04", "CAT-05"]
    assert sum(q["open"] for q in data["queues"]) == 675
    assert [c.number for c in data["readiness"].conditions] == [1, 2, 3, 4, 5, 6]
    html = rendered(services, maya, data["run"], "dashboard.html", "Dashboard", page=data)
    assert "CAT-01 Exact" in html and "Close readiness" in html
    # Maya may not sign off: the button is shown disabled with the reason (W1).
    assert "Staff Accountant cannot sign off" in html


def test_tci_042_queues_list_every_open_item_including_low_confidence(web):
    services, _, run_id = web
    for category in views.CATEGORIES:
        data = views.queue(services, run_id, category)
        assert len(data["rows"]) == len(views.queue_rows(services, run_id, category))
        html = rendered(services, None, data["run"], "queue.html", category, page=data)
        assert category in html
    with pytest.raises(KeyError):
        views.queue(services, run_id, "CAT-09")


def test_tci_043_the_audit_page_verifies_the_chain_and_pages_events(web):
    services, _, run_id = web
    data = views.audit_log(services, run_id, page=1)
    assert data["verification"].intact and data["total"] == services.audit.count(run_id)
    assert len(data["rows"]) == min(views.AUDIT_PAGE_SIZE, data["total"])
    html = rendered(services, None, services.repositories.runs.get(run_id), "audit.html", "Audit", page=data)
    assert "Chain intact" in html
    filtered = views.audit_log(services, run_id, "IMPORT")
    assert {row["event_type"] for row in filtered["rows"]} == {"IMPORT"}


def test_tci_044_reports_index_before_and_after_generation(web):
    services, users, run_id = web
    assert not views.reports_index(services, run_id)["generated"]
    services.reports.generate_package(run_id, users["Maya Castillo"])
    data = views.reports_index(services, run_id)
    assert data["generated"] and all(r["hash"] for r in data["reports"])
    html = rendered(services, None, services.repositories.runs.get(run_id), "reports.html", "Reports", page=data)
    assert "Reconciliation Summary" in html


def test_tci_045_blocked_actions_carry_their_reason_and_text_is_escaped(web):
    services, users, run_id = web
    daniel = services.repositories.users.get(users["Daniel Okafor"])
    assert views.can(daniel, permissions.SIGN_OFF).allowed
    assert "cannot import" in views.can(daniel, permissions.IMPORT).reason
    assert views.can(None, permissions.VIEW).reason == "select an identity first"
    html = render("error.html", **views.base(services, None, None, [], "Error"),
                  heading="Not found", message="<script>x</script>")
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert money(-123456) == "-$1,234.56" and views.run_status(services, run_id)["status"] == "in_review"


def test_tci_046_a_database_with_no_run_shows_the_starting_point(tmp_path):
    services, _, _ = build_services(tmp_path, match=False)
    data = views.dashboard(services, None, None)
    assert data == {"run": None, "initialized": True}
    assert "No reconciliation run yet" in rendered(services, None, None, "dashboard.html", "Dashboard", page=data)


# ---------------------------------------------------------------------------
# Review screens (W3..W6)
# ---------------------------------------------------------------------------

def user_row(services, users, name):
    return services.repositories.users.get(users[name])


def first_rec(services, run_id, category, **criteria):
    return next(row for row in services.repositories.recommendations.queue(run_id, category)
                if all(row[key] == value for key, value in criteria.items()))


def test_tcu_164_explanations_split_into_supporting_and_conflicting_blocks():
    text = ("Supporting evidence: 4 entries sum exactly to $5,376.40; same business date. "
            "Conflicting evidence: counterparty text similarity is only 0.27, so the names do not support "
            "this pairing. Confidence 0.02 (Low band), routed to CAT-03.")
    parts = views.split_explanation(text)
    assert parts["supporting"] == ["4 entries sum exactly to $5,376.40", "same business date"]
    assert parts["conflicting"] == ["counterparty text similarity is only 0.27, so the names do not support this pairing"]
    assert parts["notes"].startswith("Confidence 0.02")
    assert views.split_explanation(None)["notes"] == ""


def test_tci_053_item_detail_shows_every_candidate_and_the_conflicting_evidence(web):
    services, users, run_id = web
    maya = user_row(services, users, "Maya Castillo")
    rec = next(row for row in services.repositories.recommendations.queue(run_id, "CAT-03")
               if "Conflicting evidence" in (row["explanation"] or ""))
    data = views.item_detail(services, rec["recommendation_id"], maya)
    assert len(data["candidates"]) == len(services.repositories.recommendations.candidates(rec["recommendation_id"]))
    assert data["explanation"]["conflicting"]
    states = {a["decision"]: a for a in data["actions"]}
    assert states["reject"]["allowed"] and states["reject"]["comment_required"]
    html = rendered(services, maya, data["run"], "item.html", "Item", page=data)
    assert "Conflicting evidence" in html and 'checked' not in html      # nothing preselected (FR-REV-10)


def test_tci_054_high_risk_items_show_why_staff_cannot_decide(web):
    services, users, run_id = web
    rec = first_rec(services, run_id, "CAT-05", kind="match")
    data = views.item_detail(services, rec["recommendation_id"], user_row(services, users, "Maya Castillo"))
    offered = [a for a in data["actions"] if a["offered"]]
    assert offered and all(not a["allowed"] and "CR-02" in a["reason"] for a in offered)


def test_tci_055_an_escalator_sees_the_separation_of_duties_block(web):
    """W6: the item is shown to the escalator, with the rule that stops them (CR-03)."""
    services, users, run_id = web
    priya = user_row(services, users, "Priya Raman")
    rec = first_rec(services, run_id, "CAT-03")
    services.review.decide(rec["recommendation_id"], priya["user_id"], "escalate", comment="Needs a second view")
    data = views.item_detail(services, rec["recommendation_id"], priya)
    assert all("CR-03" in a["reason"] for a in data["actions"] if a["offered"])
    senior = views.queue(services, run_id, "CAT-05", priya)
    row = next(r for r in senior["rows"] if r["recommendation_id"] == rec["recommendation_id"])
    assert row["senior"]["escalated_by"] == "Priya Raman" and "CR-03" in row["senior"]["blocked"]
    assert "You cannot decide" in rendered(services, priya, senior["run"], "queue.html", "Senior", page=senior)


def test_tci_056_fee_exceptions_offer_an_adjustment_and_manual_pairing(web):
    services, users, run_id = web
    maya = user_row(services, users, "Maya Castillo")
    rec = first_rec(services, run_id, "CAT-04", exception_code="EXC-03")
    data = views.item_detail(services, rec["recommendation_id"], maya)
    assert data["adjustment"]["allowed"] and data["adjustment"]["suggestion"].debit_account_code == "6810"
    assert data["manual"] is not None
    assert not next(a for a in data["actions"] if a["decision"] == "reject")["offered"]
    html = rendered(services, maya, data["run"], "item.html", "Item", page=data)
    assert "Propose adjustment" in html and "Pair by hand" in html


def test_tci_057_unexplained_items_can_only_be_left_unresolved(web):
    services, users, run_id = web
    rec = first_rec(services, run_id, "CAT-05", exception_code="EXC-07")
    data = views.item_detail(services, rec["recommendation_id"], user_row(services, users, "Priya Raman"))
    assert [a["decision"] for a in data["actions"] if a["offered"]] == ["unresolved"]


def test_tci_058_batch_and_adjustment_screens(web):
    services, users, run_id = web
    maya, priya = user_row(services, users, "Maya Castillo"), user_row(services, users, "Priya Raman")
    data = views.batch(services, run_id, maya)
    assert len(data["rows"]) == 378 and data["permission"].allowed
    assert "Approve selected" in rendered(services, maya, data["run"], "batch.html", "Batch", page=data)

    rec = first_rec(services, run_id, "CAT-04", exception_code="EXC-03")
    suggestion = services.adjustments.suggestion(rec["recommendation_id"])
    services.adjustments.propose(rec["recommendation_id"], priya["user_id"], amount_cents=suggestion.amount_cents,
                                 debit_account_code="6810", credit_account_code="1010", rationale="Fee")
    queue = views.adjustment_queue(services, run_id, priya)
    assert "CR-04" in queue["rows"][0]["reason"]
    assert "cannot approve" in views.adjustment_queue(services, run_id, maya)["rows"][0]["reason"]
    daniel = user_row(services, users, "Daniel Okafor")
    assert views.adjustment_queue(services, run_id, daniel)["rows"][0]["allowed"]
    assert "Dr 6810 / Cr 1010" in rendered(services, daniel, queue["run"], "adjustments.html", "Adj", page=queue)


# ---------------------------------------------------------------------------
# Starting a run (W2) and closing it (W7)
# ---------------------------------------------------------------------------

from app.control import ControlViolation  # noqa: E402
from app.services.run_service import RunInputError  # noqa: E402
from tests.web_support import as_harness, dataset_uploads  # noqa: E402


def test_tci_064_a_run_is_created_imported_validated_and_matched(tmp_path):
    services, users, _ = build_services(tmp_path, match=False)
    maya = users["Maya Castillo"]
    run_id = services.runs.create(maya, "2026-08-01", "2026-08-31")
    page = views.import_page(services, run_id, services.repositories.users.get(maya))
    assert page["can_upload"] and not page["files"]

    services.runs.import_files(run_id, maya, dataset_uploads())
    page = views.import_page(services, run_id, services.repositories.users.get(maya))
    assert len(page["files"]) == 3 and page["can_process"]

    result = services.runs.process(run_id, maya)
    assert result.validation.passed and result.matching.recommendations == 675
    page = views.import_page(services, run_id, services.repositories.users.get(maya))
    assert page["summary"]["passed"] > 0 and not page["summary"]["file_failures"]
    html = rendered(services, None, page["run"], "import.html", "Import", page=page)
    assert "Validation results" in html and "Go to the review queues" in html


def test_tci_065_a_file_level_failure_blocks_matching_and_keeps_the_evidence(tmp_path):
    """CR-17: the run stays 'validation_failed'; the correction is a new run."""
    services, users, _ = build_services(tmp_path, match=False)
    maya = users["Maya Castillo"]
    run_id = services.runs.create(maya, "2026-08-01", "2026-08-31")
    services.runs.import_files(run_id, maya, dataset_uploads(break_control_total=True))
    result = services.runs.process(run_id, maya)
    assert result.matching is None and not result.validation.passed
    assert services.repositories.runs.get(run_id)["status"] == "validation_failed"
    page = views.import_page(services, run_id, None)
    assert page["failed"] and page["summary"]["file_failures"]
    assert "start a new run" in rendered(services, None, page["run"], "import.html", "Import", page=page)
    with pytest.raises(ControlViolation):
        services.runs.import_files(run_id, maya, dataset_uploads())


def test_tcc_062_run_start_controls(tmp_path):
    services, users, _ = build_services(tmp_path, match=False)
    with pytest.raises(ControlViolation) as caught:
        services.runs.create(users["Daniel Okafor"], "2026-08-01", "2026-08-31")
    assert caught.value.rule == "ROLE"
    with pytest.raises(RunInputError):
        services.runs.create(users["Maya Castillo"], "2026-08-31", "2026-08-01")
    run_id = services.runs.create(users["Maya Castillo"], "2026-08-01", "2026-08-31")
    with pytest.raises(RunInputError) as missing:
        services.runs.import_files(run_id, users["Maya Castillo"], {"bank": b"x"})
    assert len(missing.value.problems) == 2
    with pytest.raises(ControlViolation) as early:
        services.runs.process(run_id, users["Maya Castillo"])
    assert early.value.rule == "STATE"
    assert [e["event_type"] for e in services.audit.events(run_id)] == ["BLOCKED_ATTEMPT"]


def test_tci_066_the_close_screen_explains_who_may_sign_and_why(web):
    from tests.test_reports import complete_august_review
    services, users, run_id = web
    daniel = services.repositories.users.get(users["Daniel Okafor"])
    page = views.close_page(services, run_id, daniel)
    assert not page["sign"].allowed and "CR-10" in page["sign"].reason

    complete_august_review(as_harness(services, users, run_id), services.review, services.adjustments)
    services.reports.generate_package(run_id, users["Maya Castillo"])
    page = views.close_page(services, run_id, daniel)
    assert page["sign"].allowed and page["comment_required"]
    assert page["statement"].unresolved_difference_cents == -939_235
    html = rendered(services, daniel, page["run"], "close.html", "Close", page=page)
    assert "-$9,392.35" in html and "Sign off and lock the period" in html
    maya_page = views.close_page(services, run_id, services.repositories.users.get(users["Maya Castillo"]))
    assert "cannot sign off" in maya_page["sign"].reason
