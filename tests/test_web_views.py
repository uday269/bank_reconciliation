"""Web view and page tests that need no server (TC-I-041..045).

They build each screen's data with the view functions and render it with the real
templates, so a missing value or a broken template fails here, without FastAPI.
The route tests in test_web_routes.py cover the HTTP layer.
"""

from __future__ import annotations

import pytest

from app.control import permissions
from app.web import views
from markupsafe import escape

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
    assert "Exact matches" in html and "Ready to close?" in html and "What to do next" in html
    assert data["next"].action == "Approve exact matches"
    # Maya may not sign off: the button is shown disabled with the reason (W1).
    assert "Only the Controller can sign off" in html


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
    refused = views.can(daniel, permissions.IMPORT)
    assert refused.reason == "Only a Staff Accountant or a Senior Accountant can import and start a run."
    assert refused.rule == "ROLE"
    assert "Acting as" in views.can(None, permissions.VIEW).reason
    html = render("error.html", **views.base(services, None, None, [], "Error"),
                  heading="Not found", message="<script>x</script>", technical="")
    assert "<script>x" not in html and "&lt;script&gt;" in html
    assert money(-123456) == "-$1,234.56" and views.run_status(services, run_id)["status"] == "in_review"


def test_tci_046_a_database_with_no_run_shows_the_starting_point(tmp_path):
    services, _, _ = build_services(tmp_path, match=False)
    data = views.dashboard(services, None, None)
    assert data == {"run": None, "initialized": True}
    assert "No month started yet" in rendered(services, None, None, "dashboard.html", "Dashboard", page=data)


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
    assert "What doesn't fit" in html and 'checked' not in html      # nothing preselected (FR-REV-10)


def test_tci_054_high_risk_items_show_why_staff_cannot_decide(web):
    services, users, run_id = web
    rec = first_rec(services, run_id, "CAT-05", kind="match")
    data = views.item_detail(services, rec["recommendation_id"], user_row(services, users, "Maya Castillo"))
    offered = [a for a in data["actions"] if a["offered"]]
    assert offered and all(not a["allowed"] and a["rule"] == "CR-02" for a in offered)
    assert all(a["reason"] == "This needs a Senior Accountant or the Controller." for a in offered)


def test_tci_055_an_escalator_sees_the_separation_of_duties_block(web):
    """W6: the item is shown to the escalator, with the rule that stops them (CR-03)."""
    services, users, run_id = web
    priya = user_row(services, users, "Priya Raman")
    rec = first_rec(services, run_id, "CAT-03")
    services.review.decide(rec["recommendation_id"], priya["user_id"], "escalate", comment="Needs a second view")
    data = views.item_detail(services, rec["recommendation_id"], priya)
    assert all(a["rule"] == "CR-03" for a in data["actions"] if a["offered"])
    senior = views.queue(services, run_id, "CAT-05", priya)
    row = next(r for r in senior["rows"] if r["recommendation_id"] == rec["recommendation_id"])
    assert row["senior"]["escalated_by"] == "Priya Raman" and "someone else" in row["senior"]["blocked"]
    assert "someone else must make the final decision" in rendered(services, priya, senior["run"], "queue.html", "Senior", page=senior)


def test_tci_056_fee_exceptions_offer_an_adjustment_and_manual_pairing(web):
    services, users, run_id = web
    maya = user_row(services, users, "Maya Castillo")
    rec = first_rec(services, run_id, "CAT-04", exception_code="EXC-03")
    data = views.item_detail(services, rec["recommendation_id"], maya)
    assert data["adjustment"]["allowed"] and data["adjustment"]["suggestion"].debit_account_code == "6810"
    assert data["manual"] is not None
    assert not next(a for a in data["actions"] if a["decision"] == "reject")["offered"]
    html = rendered(services, maya, data["run"], "item.html", "Item", page=data)
    assert "Propose this entry" in html and "Find the matching records yourself" in html


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
    assert "Approve the ticked matches" in rendered(services, maya, data["run"], "batch.html", "Batch", page=data)

    rec = first_rec(services, run_id, "CAT-04", exception_code="EXC-03")
    suggestion = services.adjustments.suggestion(rec["recommendation_id"])
    services.adjustments.propose(rec["recommendation_id"], priya["user_id"], amount_cents=suggestion.amount_cents,
                                 debit_account_code="6810", credit_account_code="1010", rationale="Fee")
    queue = views.adjustment_queue(services, run_id, priya)
    assert queue["rows"][0]["rule"] == "CR-04" and "someone else must approve" in queue["rows"][0]["reason"]
    assert views.adjustment_queue(services, run_id, maya)["rows"][0]["reason"].startswith("Only a Senior Accountant or the Controller")
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
    assert "Results of the file checks" in html and "Go to the review lists" in html


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
    assert "start the month again" in rendered(services, None, page["run"], "import.html", "Import", page=page)
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
    assert not page["sign"].allowed and page["sign"].rule == "CR-10"

    complete_august_review(as_harness(services, users, run_id), services.review, services.adjustments)
    services.reports.generate_package(run_id, users["Maya Castillo"])
    page = views.close_page(services, run_id, daniel)
    assert page["sign"].allowed and page["comment_required"]
    assert page["statement"].unresolved_difference_cents == -939_235
    html = rendered(services, daniel, page["run"], "close.html", "Close", page=page)
    assert "-$9,392.35" in html and "Sign off and close the month" in html
    maya_page = views.close_page(services, run_id, services.repositories.users.get(users["Maya Castillo"]))
    assert maya_page["sign"].reason.startswith("Only the Controller can sign off")



def test_tci_079_help_explains_every_list_kind_and_choice_in_plain_words(web):
    """The Help page is built from the same vocabulary as the screens, so the two cannot drift."""
    from app.web import labels
    services, _, run_id = web
    html = render("help.html", **views.base(services, None, services.repositories.runs.get(run_id), [], "Help"))
    for words in (*labels.QUEUE_NAMES.values(), *labels.EXCEPTION_NAMES.values(), *labels.DECISION_BUTTONS.values()):
        assert str(escape(words)) in html, words
    assert 'class="tech"' in html                     # codes present, but only under Show details


def test_tcu_169_refusals_read_as_instructions_not_codes():
    from app.web import labels
    assert str(labels.refusal("ROLE", "Staff Accountant cannot sign off and close the period; permitted for Controller")) \
        == "Only the Controller can sign off and close the period."
    assert labels.refusal("CR-16", "'reject' requires a comment").text == "Please add a comment explaining your decision."
    assert labels.refusal("STATE", "recommendation 5 has already been decided").text == "Recommendation 5 has already been decided."
    assert all("CR-" not in text and "EXC-" not in text and "CAT-" not in text
               for text in (*labels.RULE_TEXT.values(), *labels.QUEUE_HELP.values(), *labels.EXCEPTION_HELP.values()))



def visible_text(html: str) -> str:
    """What a reader sees with Show details off: no tech elements, no tags, no styles or scripts."""
    import re
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    html = re.sub(r'<(\w+)[^>]*class="[^"]*\btech\b[^"]*"[^>]*>.*?</\1>', " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def test_tci_080_screens_speak_plainly_until_details_are_asked_for(web):
    """No category, exception or rule code is visible on the main screens with details off;
    every one is still there for an auditor under Show details."""
    import re
    services, users, run_id = web
    maya = user_row(services, users, "Maya Castillo")
    run = services.repositories.runs.get(run_id)
    pages = [rendered(services, maya, run, "dashboard.html", "Home", page=views.dashboard(services, run_id, maya))]
    for category in views.CATEGORIES:
        pages.append(rendered(services, maya, run, "queue.html", "List", page=views.queue(services, run_id, category, maya)))
    for criteria in ({"kind": "match"}, {"exception_code": "EXC-03"}):
        category = "CAT-03" if criteria.get("kind") else "CAT-04"
        rec = first_rec(services, run_id, category, **criteria)
        data = views.item_detail(services, rec["recommendation_id"], maya)
        pages.append(rendered(services, maya, data["run"], "item.html", "Item", page=data))
    batch = views.batch(services, run_id, maya)
    pages.append(rendered(services, maya, batch["run"], "batch.html", "Batch", page=batch))

    codes = re.compile(r"\b(CAT-0\d|EXC-0\d|CR-\d\d|RR-0\d|FR-[A-Z]+-\d\d)\b")
    for html in pages:
        assert not codes.findall(visible_text(html)), codes.findall(visible_text(html))[:5]
    assert any(codes.findall(html) for html in pages)          # still present behind details


def test_tcu_170_match_evidence_reads_as_sentences():
    from app.web import plain
    facts = plain.match_facts({"amount_difference_cents": 0, "business_day_gap": -1, "text_similarity": 0.2709,
                               "reference_state": "none"})
    assert facts == [("Amounts agree exactly", True), ("1 business day apart", True),
                     ("Names don't look alike (27% alike)", False)]
    assert plain.evidence_sentence("counterparty text similarity is only 0.28, so the names do not support this pairing") \
        == "The names don't look alike (28% similar)"
    assert plain.evidence_sentence("4 entries sum exactly to $6,348.00") == "4 book entries add up exactly to $6,348.00"
    assert plain.kind_of_record("ledger", "debit") == "Receipt in the books"


def test_tcu_171_rule_findings_read_as_plain_sentences():
    """Rule-settled items show what the rule found, in dollars and dates, without rule numbers."""
    from app.web import plain
    exact = ("Amount 624915 cents, reference 8437 and date 2026-08-03 agree, and no other item on either "
             "side shares them (BR-01, BR-02) Routed to CAT-01: settled by a deterministic rule.")
    assert plain.rule_evidence(exact) == ["Same amount on both: $6,249.15", "Same reference on both: 8437",
                                          "Same date on both: 3 Aug 2026",
                                          "No other record on either side has these same details"]
    stale = ("Payment of 107370 cents dated 2026-05-24 has not cleared the bank, 99 days outstanding, "
             "beyond the 45 day limit (BR-10) Suggested action: Investigate the stale check. Routed to CAT-04: x.")
    assert plain.rule_evidence(stale) == ["Payment of $1,073.70 dated 24 May 2026 has not cleared the bank, "
                                          "99 days outstanding, beyond the 45 day limit"]
    assert plain.rule_evidence(None) == []


def test_tci_081_an_exact_match_shows_its_evidence_in_one_date_style(web):
    """An exact match lists what agrees; dates read '3 Aug 2026' everywhere; a name the system
    read from the description is labelled as such, not as a cleaned-up value."""
    import re
    services, users, run_id = web
    maya = user_row(services, users, "Maya Castillo")
    rec = next(r for r in services.repositories.recommendations.queue(run_id, "CAT-01") if r["rule_name"] == "BR-01")
    data = views.item_detail(services, rec["recommendation_id"], maya)
    data["originals"] = []           # the original records are shown raw on purpose (TC-I-083)
    text = visible_text(rendered(services, maya, data["run"], "item.html", "Item", page=data))
    assert "Same amount on both" in text and "Nothing specific noted" not in text
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    assert text.count("Suggested match") == 1

    fee = first_rec(services, run_id, "CAT-04", exception_code="EXC-03")
    data = views.item_detail(services, fee["recommendation_id"], maya)
    text = visible_text(rendered(services, maya, data["run"], "item.html", "Item", page=data))
    assert "Read from the description" in text and "Cleaned up" not in text


def test_tci_082_the_month_screens_speak_plainly_too(web):
    """Adjustments, close, files, reports and audit trail: no rule, check or report code is
    visible with details off, and the codes are still there behind Show details."""
    import re
    from tests.test_reports import complete_august_review
    services, users, run_id = web
    complete_august_review(as_harness(services, users, run_id), services.review, services.adjustments)
    services.reports.generate_package(run_id, users["Maya Castillo"])
    daniel = user_row(services, users, "Daniel Okafor")
    run = services.repositories.runs.get(run_id)
    pages = {
        "adjustments": rendered(services, daniel, run, "adjustments.html", "Adj",
                                page=views.adjustment_queue(services, run_id, daniel)),
        "close": rendered(services, daniel, run, "close.html", "Close", page=views.close_page(services, run_id, daniel)),
        "files": rendered(services, daniel, run, "import.html", "Files", page=views.import_page(services, run_id, daniel)),
        "reports": rendered(services, daniel, run, "reports.html", "Reports", page=views.reports_index(services, run_id)),
        "audit": rendered(services, daniel, run, "audit.html", "Audit", page=views.audit_log(services, run_id)),
    }
    codes = re.compile(r"\b(CAT-0\d|EXC-0\d|CR-\d\d|BR-\d\d|RR-0\d|VAL-0\d|RPT-\d\d|DD-\d\d|FR-[A-Z]+-\d\d)\b")
    for name, html in pages.items():
        assert not codes.findall(visible_text(html)), (name, codes.findall(visible_text(html))[:5])
        assert not re.search(r"\b\d+ cents\b", visible_text(html)), name
    assert "Bank Service Charges" in pages["adjustments"] and "Does the bank agree with the books?" in pages["close"]
    assert "No tampering detected" in pages["audit"] and "Fixed rule applied" in pages["audit"]
    assert "built-in method" not in pages["audit"]                  # the records column shows record numbers
    assert "The reports are up to date" in pages["reports"]
    assert all(codes.findall(html) for name, html in pages.items() if name != "files")   # still behind details


def test_tcu_172_file_check_results_read_in_dollars_and_dates():
    from app.web import plain
    assert plain.check_message("declared 122718935/125051165 cents (debit/credit), stored 122718935/125051165") == (
        "Control totals: debits $1,227,189.35, credits $1,250,511.65. "
        "The rows add up to debits $1,227,189.35, credits $1,250,511.65")
    assert plain.check_message("dated 2026-07-31, outside 2026-08-01 to 2026-08-31") == (
        "Dated 31 Jul 2026, outside the month (1 Aug 2026 to 31 Aug 2026)")


def test_tci_083_original_records_are_shown_exactly_as_received(web):
    """Both sides of a match appear as their file rows, untouched, with the file name."""
    import csv
    from tests.conftest import DATASET
    services, users, run_id = web
    maya = user_row(services, users, "Maya Castillo")
    rec = next(r for r in services.repositories.recommendations.queue(run_id, "CAT-01") if r["rule_name"] == "BR-01")
    data = views.item_detail(services, rec["recommendation_id"], maya)
    rows = {}
    for name, key in (("bank_statement.csv", "txn_id"), ("gl_cash_detail.csv", "entry_id")):
        with open(DATASET / name, newline="", encoding="utf-8") as handle:
            rows.update({row[key]: row for row in csv.DictReader(handle)})
    assert len(data["originals"]) == 2
    for record in data["originals"]:
        fields = dict(record["fields"])
        original = rows[fields.get("txn_id") or fields.get("entry_id")]
        assert all(fields[column] == original[column] for column in original), (fields, original)
        assert record["file_name"].endswith(".csv")
    html = rendered(services, maya, data["run"], "item.html", "Item", page=data)
    assert "Original records, exactly as received" in html


def test_tcu_173_the_ai_is_sent_plain_names_never_codes():
    """The prompt uses the same words as the screens, so a summary cannot echo a code."""
    import re
    from app.infra.genai.base import EXCEPTION_WORDS, QUEUE_WORDS, ProseRequest
    from app.web import labels
    assert QUEUE_WORDS == labels.QUEUE_NAMES and EXCEPTION_WORDS == labels.EXCEPTION_NAMES
    request = ProseRequest(item_summary="ORIGINATION FEE", amount_cents=13995, item_date="2026-08-27",
                           category_code="CAT-04", risk_level="low", exception_code="EXC-03",
                           supporting_evidence=[], conflicting_evidence=[])
    data_lines = request.as_prompt().split("\n\n", 1)[1]
    assert not re.search(r"\b(CAT|EXC)-0\d\b", data_lines)
    assert "Unmatched items" in data_lines and "Bank fee not yet recorded" in data_lines
