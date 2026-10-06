"""HTTP route tests for the reviewer interface (TC-I-047..052).

These use FastAPI's test client, so they need the packages in requirements.txt. Each
test starts from a fresh database with the August run matched.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient  # noqa: E402

from app.web.main import create_app  # noqa: E402
from tests.web_support import build_services  # noqa: E402


@pytest.fixture
def web(tmp_path):
    services, users, run_id = build_services(tmp_path)
    client = TestClient(create_app(services=services))
    return client, services, users, run_id


def act_as(client, users, name):
    return client.post("/identity", data={"user_id": str(users[name])})


def events(services, run_id, event_type):
    return [e for e in services.audit.events(run_id) if e["event_type"] == event_type]


def test_tci_047_dashboard_and_identity_selection(web):
    client, services, users, run_id = web
    response = client.get("/")
    assert response.status_code == 200 and f"Run {run_id}" in response.text
    assert "no identity selected" in response.text

    response = act_as(client, users, "Maya Castillo")
    assert response.status_code == 200 and "Acting as Maya Castillo" in response.text
    assert events(services, run_id, "IDENTITY_SELECTED")[-1]["actor_user_id"] == users["Maya Castillo"]
    assert client.get("/static/app.css").status_code == 200


def test_tci_048_queues_and_unknown_records(web):
    client, _, _, run_id = web
    for category in ("CAT-01", "CAT-02", "CAT-03", "CAT-04", "CAT-05"):
        assert client.get(f"/runs/{run_id}/queues/{category}").status_code == 200
    assert client.get(f"/runs/{run_id}/queues/CAT-09").status_code == 404
    assert client.get("/runs/999/audit").status_code == 404


def test_tci_049_audit_log_page_and_filter(web):
    client, _, _, run_id = web
    response = client.get(f"/runs/{run_id}/audit", params={"event_type": "IMPORT", "page": 1})
    assert response.status_code == 200 and "Chain intact" in response.text
    assert response.text.count("<td>IMPORT</td>") == 3


def test_tci_050_report_package_view_and_export(web):
    client, services, users, run_id = web
    response = client.post(f"/runs/{run_id}/reports")
    assert "Select an identity" in response.text                      # nothing generated without one
    assert services.repositories.reports.current(run_id) == []

    act_as(client, users, "Maya Castillo")
    response = client.post(f"/runs/{run_id}/reports")
    assert response.status_code == 200 and "Report package generated" in response.text
    summary = client.get(f"/runs/{run_id}/reports/RPT-09")
    assert summary.status_code == 200 and "Reconciliation Summary" in summary.text
    export = client.get(f"/runs/{run_id}/reports/RPT-09.csv")
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
    assert events(services, run_id, "REPORT_EXPORTED")[-1]["actor_user_id"] == users["Maya Castillo"]
    assert client.get(f"/runs/{run_id}/reports/RPT-99").status_code == 404


def test_tci_051_json_status_and_chain_verification(web):
    client, services, _, run_id = web
    status = client.get(f"/api/runs/{run_id}/status").json()
    assert status["status"] == "in_review" and sum(status["queues"].values()) == 675
    verify = client.get(f"/api/runs/{run_id}/audit/verify").json()
    assert verify["intact"] is True
    assert events(services, run_id, "CHAIN_VERIFIED")


def test_tci_052_clearing_the_identity(web):
    client, _, users, _ = web
    act_as(client, users, "Priya Raman")
    response = client.post("/identity", data={"user_id": ""})
    assert "no identity selected" in response.text


# ---------------------------------------------------------------------------
# Review screens (W3..W6)
# ---------------------------------------------------------------------------

def first_rec(services, run_id, category, **criteria):
    return next(row for row in services.repositories.recommendations.queue(run_id, category)
                if all(row[key] == value for key, value in criteria.items()))


def test_tci_059_opening_and_approving_an_item_through_the_screens(web):
    """The detail view records when it was opened; the decision takes its timing from that."""
    client, services, users, run_id = web
    act_as(client, users, "Maya Castillo")
    rec = first_rec(services, run_id, "CAT-02")
    assert client.get(f"/items/{rec['recommendation_id']}").status_code == 200
    response = client.post(f"/items/{rec['recommendation_id']}/decision", data={"decision": "approve"})
    assert response.status_code == 200 and "approved" in response.text
    decision = services.repositories.decisions.latest(rec["recommendation_id"])
    assert decision["decided_by"] == users["Maya Castillo"] and decision["opened_at"]


def test_tci_060_a_refused_action_names_the_rule_and_leaves_evidence(web):
    client, services, users, run_id = web
    act_as(client, users, "Maya Castillo")
    rec = first_rec(services, run_id, "CAT-05", kind="match")
    client.get(f"/items/{rec['recommendation_id']}")
    response = client.post(f"/items/{rec['recommendation_id']}/decision",
                           data={"decision": "approve", "comment": "Looks right"})
    assert "Not allowed" in response.text and "CR-02" in response.text
    assert events(services, run_id, "BLOCKED_ATTEMPT")[-1]["rule_name"] == "CR-02"
    assert services.repositories.decisions.for_recommendation(rec["recommendation_id"]) == []

    reject = first_rec(services, run_id, "CAT-01")
    response = client.post(f"/items/{reject['recommendation_id']}/decision", data={"decision": "reject"})
    assert "CR-16" in response.text


def test_tci_061_batch_approval_through_the_screen(web):
    client, services, users, run_id = web
    act_as(client, users, "Maya Castillo")
    assert client.get(f"/runs/{run_id}/batch").status_code == 200
    ids = [str(row["recommendation_id"]) for row in services.repositories.recommendations.queue(run_id, "CAT-01")[:5]]
    response = client.post(f"/runs/{run_id}/batch", data={"recommendation_id": ids, "action": "approve"})
    assert "5 item(s) approved" in response.text
    assert len(services.repositories.decisions.for_run(run_id)) == 5
    response = client.post(f"/runs/{run_id}/batch", data={"action": "approve"})
    assert "select at least one row" in response.text


def test_tci_062_adjustment_proposal_and_approval_through_the_screens(web):
    client, services, users, run_id = web
    rec = first_rec(services, run_id, "CAT-04", exception_code="EXC-03")
    act_as(client, users, "Maya Castillo")
    response = client.post(f"/items/{rec['recommendation_id']}/adjustment",
                           data={"amount": "abc", "debit": "6810", "credit": "1010", "rationale": "Fee"})
    assert "Nothing was saved" in response.text
    client.post(f"/items/{rec['recommendation_id']}/adjustment",
                data={"amount": "86.90", "debit": "6810", "credit": "1010", "rationale": "Monthly fee"})
    adjustment = services.repositories.adjustments.for_recommendation(rec["recommendation_id"])[0]
    assert adjustment["amount_cents"] == 8690 and adjustment["status"] == "proposed"

    assert client.get(f"/runs/{run_id}/adjustments").status_code == 200
    act_as(client, users, "Priya Raman")
    response = client.post(f"/adjustments/{adjustment['adjustment_id']}/decision", data={"decision": "approve"})
    assert "approved" in response.text
    assert services.repositories.adjustments.get(adjustment["adjustment_id"])["status"] == "approved"


def test_tci_063_manual_pairing_through_the_screen(web):
    from types import SimpleNamespace

    from tests import test_manual_match as hand
    client, services, users, run_id = web
    subject, counterparts = hand.missed_groups(SimpleNamespace(repositories=services.repositories, run_id=run_id))[0]
    rec = services.repositories.recommendations.open_holding(run_id, *subject)[0]
    act_as(client, users, "Priya Raman")
    client.get(f"/items/{rec['recommendation_id']}")
    response = client.post(f"/items/{rec['recommendation_id']}/manual-match",
                           data={"counterpart": [f"{t}:{i}" for t, i in counterparts],
                                 "comment": "Agreed to remittance advice"})
    assert "paired by hand" in response.text
    assert services.repositories.decisions.latest(rec["recommendation_id"])["decision"] == "modify"


# ---------------------------------------------------------------------------
# Starting a run (W2) and closing it (W7)
# ---------------------------------------------------------------------------

def test_tci_067_a_run_started_entirely_from_the_screens(tmp_path):
    from tests.web_support import UPLOAD_NAMES, dataset_uploads
    services, users, _ = build_services(tmp_path, match=False)
    client = TestClient(create_app(services=services))
    act_as(client, users, "Maya Castillo")
    assert client.get("/import").status_code == 200

    response = client.post("/runs", data={"period_start": "2026-08-01", "period_end": "2026-08-31"})
    assert "Import its files next" in response.text
    run_id = services.repositories.runs.latest()["run_id"]

    uploads = dataset_uploads()
    files = {name: (UPLOAD_NAMES[name], content, "text/csv") for name, content in uploads.items()}
    response = client.post(f"/runs/{run_id}/import", files=files)
    assert "Imported" in response.text and len(services.repositories.source_files.for_run(run_id)) == 3

    response = client.post(f"/runs/{run_id}/match")
    assert "675 recommendations" in response.text
    assert services.repositories.runs.get(run_id)["status"] == "in_review"


def test_tci_068_sign_off_and_reopening_through_the_close_screen(web):
    from tests.test_reports import complete_august_review
    from tests.web_support import as_harness
    client, services, users, run_id = web
    complete_august_review(as_harness(services, users, run_id), services.review, services.adjustments)

    act_as(client, users, "Daniel Okafor")
    client.post(f"/runs/{run_id}/reports")
    response = client.post(f"/runs/{run_id}/close", data={"comment": ""})
    assert "CR-10" in response.text                                  # a difference needs a comment
    response = client.post(f"/runs/{run_id}/close", data={"comment": "Duplicates under bank research"})
    assert "signed off and locked" in response.text
    assert services.repositories.runs.get(run_id)["status"] == "closed"

    act_as(client, users, "Maya Castillo")
    client.post(f"/runs/{run_id}/reopen-request", data={"reason": "Bank issued a corrected statement"})
    request_id = services.repositories.periods.pending_reopen_request(run_id)["reopen_request_id"]
    act_as(client, users, "Daniel Okafor")
    response = client.post(f"/reopen-requests/{request_id}/decision",
                           data={"decision": "approve", "comment": "Re-close after the correction"})
    assert "back in review" in response.text
    assert services.repositories.runs.get(run_id)["status"] == "in_review"
