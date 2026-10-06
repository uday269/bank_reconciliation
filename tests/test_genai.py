"""Optional generative prose (TC-U-165..167, TC-I-071..072, TC-C-063..064).

The adapter may restate evidence; it may never decide. These tests feed it providers
that misbehave on purpose and check that nothing they say reaches a score, a risk
level, a category, a status or a decision (CR-13, DD-10, NFR-05).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import ConfigError
from app.infra.genai.base import GenAIAdapter, StubAdapter, build_adapter, contains_decision_language
from app.services.prose_service import ProseService, ProseUnavailable
from tests.web_support import build_services


class ScriptedAdapter(GenAIAdapter):
    """A provider that returns whatever the test tells it to, or fails."""

    name = "scripted"

    def __init__(self, reply: str | Exception):
        super().__init__(model="test")
        self.reply = reply

    def _generate(self, request):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def enabled(services):
    """The same services with prose switched on; data stays declared synthetic."""
    return replace(services.config, genai=replace(services.config.genai, enabled=True))


@pytest.fixture
def web(tmp_path):
    return build_services(tmp_path)


def first_scored(services, run_id):
    return next(row for row in services.repositories.recommendations.queue(run_id, "CAT-03"))


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

def test_tcu_165_decision_language_is_detected():
    for text in ("I recommend approving this match.", "These items are a match.",
                 "There is a 92% chance this is correct.", "You should reject it.",
                 "Confidence is high."):
        assert contains_decision_language(text) is not None, text
    evidence = "Supporting evidence: text similarity 0.94"
    assert contains_decision_language("Text similarity is 0.94 and the amount matches exactly.", evidence) is None


def test_tcu_166_a_provider_that_decides_is_replaced_by_the_offline_summary(web):
    services, _, run_id = web
    prose = ProseService(enabled(services), services.database, services.repositories, services.audit,
                         adapter=ScriptedAdapter("This is a match; I recommend approving it."))
    request = prose.request_for(first_scored(services, run_id)["recommendation_id"])
    result = prose.adapter.prose(request)
    assert not result.generated and "CR-13" in result.rejected_reason
    assert "recommend" not in result.text.lower()


def test_tcu_167_a_failing_provider_never_blocks_a_reconciliation(web):
    services, _, run_id = web
    adapter = ScriptedAdapter(TimeoutError("provider timed out"))
    request = ProseService(enabled(services), services.database, services.repositories, services.audit,
                           adapter=adapter).request_for(first_scored(services, run_id)["recommendation_id"])
    result = adapter.prose(request)
    assert not result.generated and "provider error" in result.rejected_reason and result.text


# ---------------------------------------------------------------------------
# Through the service
# ---------------------------------------------------------------------------

def test_tci_071_prose_is_stored_and_audited_and_changes_nothing_else(web):
    """CR-13: only genai_prose changes; FR-GAI-04: the call is audited."""
    services, users, run_id = web
    rec = first_scored(services, run_id)
    before = dict(services.repositories.recommendations.get(rec["recommendation_id"]))
    prose = ProseService(enabled(services), services.database, services.repositories, services.audit,
                         adapter=ScriptedAdapter("A bank deposit of the stated amount with two candidate receipts."))
    result = prose.generate(rec["recommendation_id"], users["Maya Castillo"])
    assert result.generated

    after = dict(services.repositories.recommendations.get(rec["recommendation_id"]))
    assert after["genai_prose"] == result.text
    assert {k: v for k, v in after.items() if k != "genai_prose"} == {k: v for k, v in before.items() if k != "genai_prose"}
    assert services.repositories.decisions.for_recommendation(rec["recommendation_id"]) == []
    event = [e for e in services.audit.events(run_id) if e["event_type"] == "GENAI_PROSE"][-1]
    assert event["actor_user_id"] == users["Maya Castillo"] and result.output_sha256 in event["evidence_refs"]


def test_tci_072_rejected_provider_text_is_never_stored(web):
    services, users, run_id = web
    rec = first_scored(services, run_id)
    prose = ProseService(enabled(services), services.database, services.repositories, services.audit,
                         adapter=ScriptedAdapter("Safe to approve: 97% likely correct."))
    result = prose.generate(rec["recommendation_id"], users["Maya Castillo"])
    stored = services.repositories.recommendations.get(rec["recommendation_id"])["genai_prose"]
    assert stored == result.text and "97%" not in stored and "approve" not in stored.lower()


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def test_tcc_063_prose_is_off_by_default_and_external_calls_need_synthetic_data(web):
    """DD-10 and NFR-05."""
    services, users, run_id = web
    assert not services.prose.enabled
    with pytest.raises(ProseUnavailable):
        services.prose.generate(first_scored(services, run_id)["recommendation_id"], users["Maya Castillo"])

    gemini = replace(services.config.genai, enabled=True, provider="gemini")
    assert isinstance(build_adapter(replace(services.config, genai=replace(gemini, enabled=False))), StubAdapter)
    with pytest.raises(ConfigError):
        replace(gemini, data_classification="production").validate()


def test_tcc_064_no_prose_is_written_into_a_closed_period(web):
    services, users, run_id = web
    with services.database.transaction() as connection:
        services.repositories.runs.set_status(connection, run_id, "closed")
    prose = ProseService(enabled(services), services.database, services.repositories, services.audit,
                         adapter=ScriptedAdapter("A deposit."))
    from app.control import ControlViolation
    with pytest.raises(ControlViolation) as caught:
        prose.generate(first_scored(services, run_id)["recommendation_id"], users["Maya Castillo"])
    assert caught.value.rule == "CR-18"


def test_tci_073_the_item_screen_labels_prose_as_generated_text(web):
    """FR-GAI-02: shown beside the deterministic explanation, never instead of it."""
    from app.web import views
    from app.web.rendering import render
    services, users, run_id = web
    rec = first_scored(services, run_id)
    maya = services.repositories.users.get(users["Maya Castillo"])
    page = views.item_detail(services, rec["recommendation_id"], maya)
    html = render("item.html", **views.base(services, maya, page["run"], [], "x"), page=page)
    assert "plain-language summary" not in html.lower()          # off by default: no button, no prose

    services.prose = ProseService(enabled(services), services.database, services.repositories, services.audit,
                                  adapter=ScriptedAdapter("A deposit with two candidate receipts."))
    services.prose.generate(rec["recommendation_id"], users["Maya Castillo"])
    page = views.item_detail(services, rec["recommendation_id"], maya)
    html = render("item.html", **views.base(services, maya, page["run"], [], "x"), page=page)
    assert "generated text, not evidence" in html and "Supporting evidence" in html
    assert "Regenerate plain-language summary" in html
