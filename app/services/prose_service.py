"""Optional plain-language prose for a recommendation (FR-GAI-01..04, DD-10, CR-13).

Off by default. When `genai.enabled` is true, a reviewer can ask for a short paragraph
restating the evidence the system already computed. The adapter decides nothing:

  * it receives only derived fields (the ProseRequest whitelist), never a file or an
    account number, and an external provider is used only for data declared synthetic
  * output containing decision language, a score or an invented figure is discarded
    and the deterministic offline summary is stored instead
  * the prose is written to `recommendation.genai_prose` and nowhere else; confidence,
    risk, category, status and every decision are untouched
  * every call is audited as GENAI_PROSE with provider, model, prompt and output hashes,
    whether it was generated or fell back, and why (FR-GAI-04)
"""

from __future__ import annotations

from app.config import Config
from app.control import ControlViolation, period_lock, permissions
from app.domain.explanation import split_stored
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.genai.base import GenAIAdapter, ProseRequest, ProseResult, build_adapter
from app.infra.repositories import Repositories
from app.services.blocked import record_blocked

DATE = {"bank": "transaction_date", "ledger": "posting_date", "carry_in": "original_date"}


class ProseUnavailable(ValueError):
    """Prose is switched off in configuration. Not a control refusal; nothing is written."""

    def __init__(self) -> None:
        self.problems = ["plain-language summaries are switched off (genai.enabled = false in configuration)"]
        super().__init__(self.problems[0])


class ProseService:
    def __init__(self, config: Config, database: Database, repositories: Repositories, audit: AuditLog,
                 adapter: GenAIAdapter | None = None):
        self.config = config
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.adapter = adapter or build_adapter(config)

    @property
    def enabled(self) -> bool:
        return self.config.genai.enabled

    def request_for(self, recommendation_id: int) -> ProseRequest:
        """The whitelist sent to the adapter, built from what the system already stored."""
        rec = self.repositories.recommendations.get(recommendation_id)
        item = self.repositories.items.get(rec["subject_item_type"], rec["subject_item_id"])
        parts = split_stored(rec["explanation"])
        return ProseRequest(
            item_summary=item["description_normalized"] or item["description_original"],
            amount_cents=item["amount_cents"], item_date=item[DATE[rec["subject_item_type"]]],
            category_code=rec["category_code"], risk_level=rec["risk_level"],
            exception_code=rec["exception_code"],
            supporting_evidence=list(parts["supporting"]), conflicting_evidence=list(parts["conflicting"]))

    def generate(self, recommendation_id: int, user_id: int, at: str | None = None) -> ProseResult:
        if not self.enabled:
            raise ProseUnavailable()
        at = at or utc_now()
        rec = self.repositories.recommendations.get(recommendation_id)
        run_id = rec["run_id"]
        try:
            user = self.repositories.users.get(user_id)
            permissions.require_role(user, permissions.VIEW)
            period_lock.require_writable(self.repositories.runs.get(run_id), "generate a summary")
        except ControlViolation as violation:
            record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id, process_code="P4",
                           entity_type="recommendation", entity_id=recommendation_id,
                           attempted="generate prose", violation=violation)
            raise

        # The provider call happens outside the transaction: a slow provider must not
        # hold the database. Only the result is written, with its audit event.
        result = self.adapter.prose(self.request_for(recommendation_id))
        with self.database.transaction() as connection:
            self.repositories.recommendations.set_prose(connection, recommendation_id, result.text)
            self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
                event_type="GENAI_PROSE", process_code="P4", entity_type="recommendation",
                entity_id=recommendation_id, actor_type="human", actor_user_id=user_id,
                rule_name="CR-13", explanation=result.label,
                evidence_refs=[f"prompt_sha256:{result.prompt_sha256}", f"output_sha256:{result.output_sha256}"],
                revised_values={**result.audit_values(), "text": result.text}, created_at=at))
        return result
