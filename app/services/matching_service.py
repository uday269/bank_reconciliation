"""Deterministic matching pass (UC-03, P3 and P5).

Runs the rules in app/domain/rules.py over a validated run, assesses risk, routes each
result to a review queue and stores one recommendation per item. This is the rules-only
baseline: no scoring, no confidence, no model. Stage 7 adds candidate generation and
scoring for whatever the rules leave unmatched.

Every recommendation records the rule that produced it and an explanation a reviewer
can read, and the whole pass writes its audit events in the same transaction (CR-08).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import Config
from app.domain import exceptions as exception_catalog
from app.domain.risk import assess_risk, assign_category
from app.domain.rules import (
    Item, RuleException, RuleMatch, apply_rules, classify_unmatched_bank_item,
    classify_unmatched_ledger_item, classify_period_end_item,
)
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.repositories import CandidateInput, Repositories


@dataclass
class MatchingSummary:
    run_id: int
    rule_matches: int = 0
    exceptions: int = 0
    unmatched_for_scoring: int = 0
    recommendations: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_exception: dict[str, int] = field(default_factory=dict)
    by_risk: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"Run {self.run_id}: {self.rule_matches} rule matches, "
                f"{self.exceptions} exceptions, {self.unmatched_for_scoring} items left for "
                f"scoring, {self.recommendations} recommendations")


class MatchingService:
    def __init__(self, database: Database, repositories: Repositories,
                 audit: AuditLog, config: Config):
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.config = config

    # -- entry point --------------------------------------------------------
    def run_rules(self, run_id: int, performed_by: int | None = None) -> MatchingSummary:
        run = self.repositories.runs.get(run_id)
        if run["status"] != "processing":
            raise ValueError(
                f"run {run_id} is {run['status']}; matching runs after validation passes (CR-17)")

        parameters = self.config.parameters
        period_end = date.fromisoformat(run["period_end"])
        items = self._load_items(run_id)
        known_counterparties = self._known_counterparties(items)

        outcome = apply_rules(
            items["bank"], items["ledger"], items["carry_in"],
            period_end=period_end,
            timing_window_days=parameters.timing_window_business_days,
            stale_check_days=parameters.outstanding_check_stale_days,
            stale_deposit_days=parameters.deposit_in_transit_stale_days)

        summary = MatchingSummary(run_id=run_id)
        context = RunContext.load(self.database, run_id)
        now = utc_now()

        with self.database.transaction() as connection:
            # 1. Matches the rules are certain about.
            for match in outcome.matches:
                self._store_match(connection, run_id, match, known_counterparties, summary, now)

            # 2. Exceptions the rules classified: bank-originated, duplicates, period-end items.
            classified: set[tuple[str, int]] = set()
            for exception in outcome.exceptions:
                self._store_exception(connection, run_id, exception, known_counterparties,
                                      summary, now)
                classified.add((exception.subject.item_type, exception.subject.item_id))

            # 3. Whatever is left. In the baseline there is no scoring layer, so every
            #    remaining item is classified as an exception and sent to a reviewer.
            #    Stage 7 inserts candidate generation here and only the genuinely
            #    unmatchable items continue to this step.
            for item in outcome.unmatched_bank:
                if (item.item_type, item.item_id) in classified:
                    continue
                exception = classify_unmatched_bank_item(item, known_counterparties)
                self._store_exception(connection, run_id, exception, known_counterparties,
                                      summary, now)
                summary.unmatched_for_scoring += 1

            for item in outcome.unmatched_ledger:
                if (item.item_type, item.item_id) in classified:
                    continue
                period_exception = classify_period_end_item(
                    item, period_end, parameters.outstanding_check_stale_days,
                    parameters.deposit_in_transit_stale_days)
                exception = period_exception or classify_unmatched_ledger_item(item)
                self._store_exception(connection, run_id, exception, known_counterparties,
                                      summary, now)
                summary.unmatched_for_scoring += 1

            self.audit.write(connection, context, AuditEvent(
                event_type="ROUTING", process_code="P5", entity_type="reconciliation_run",
                entity_id=run_id,
                actor_type="human" if performed_by else "system", actor_user_id=performed_by,
                rule_name="BR-13",
                revised_values={
                    "rule_matches": summary.rule_matches,
                    "exceptions": summary.exceptions,
                    "recommendations": summary.recommendations,
                    "by_category": summary.by_category,
                    "by_risk": summary.by_risk,
                }))

            self.repositories.runs.set_status(connection, run_id, "in_review")

        return summary

    # -- storing results ----------------------------------------------------
    def _store_match(self, connection, run_id: int, match: RuleMatch,
                     known_counterparties: set[str], summary: MatchingSummary, now: str) -> None:
        subject, counterpart = match.subject, match.counterpart
        risk = assess_risk(
            amount_cents=subject.amount_cents,
            senior_approval_amount_cents=self.config.parameters.senior_approval_amount_cents,
            is_possible_duplicate=subject.is_possible_duplicate or counterpart.is_possible_duplicate,
            counterparty=subject.payee, known_counterparties=known_counterparties)
        category, reason = assign_category(risk_level=risk.level, kind="match", source="rule")

        explanation = f"{match.explanation} Routed to {category}: {reason}."
        if risk.triggered != ["RR-07"]:
            explanation += " Risk rules triggered: " + ", ".join(risk.reasons()) + "."

        recommendation_id = self.repositories.recommendations.create(
            connection, run_id=run_id, subject_item_type=subject.item_type,
            subject_item_id=subject.item_id, kind="match", source="rule",
            category_code=category, risk_level=risk.level, explanation=explanation,
            created_at=now, relationship=match.relationship, rule_name=match.rule_name,
            risk_rules=risk.as_json(),
            candidates=[CandidateInput(
                rank_order=1, score=1.0,
                feature_values='{"rule":"%s","amount_similarity":1.0,"date_distance":1.0}' % match.rule_name,
                members=[(subject.item_type, subject.item_id),
                         (counterpart.item_type, counterpart.item_id)])])

        for item in (subject, counterpart):
            self.repositories.items.set_status(connection, item.item_type, item.item_id, "proposed")

        self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
            event_type="RULE", process_code="P3", entity_type="recommendation",
            entity_id=recommendation_id, rule_name=match.rule_name,
            item_refs=[subject.external_id, counterpart.external_id],
            risk_level=risk.level, explanation=match.explanation,
            previous_status="validated", new_status="proposed"))

        summary.rule_matches += 1
        summary.recommendations += 1
        summary.by_category[category] = summary.by_category.get(category, 0) + 1
        summary.by_risk[risk.level] = summary.by_risk.get(risk.level, 0) + 1
        summary.by_rule[match.rule_name] = summary.by_rule.get(match.rule_name, 0) + 1

    def _store_exception(self, connection, run_id: int, exception: RuleException,
                         known_counterparties: set[str], summary: MatchingSummary,
                         now: str) -> None:
        item = exception.subject
        category_detail = exception_catalog.get(exception.exception_code)
        is_stale = exception.rule_name == "BR-10"

        risk = assess_risk(
            amount_cents=item.amount_cents,
            senior_approval_amount_cents=self.config.parameters.senior_approval_amount_cents,
            is_possible_duplicate=item.is_possible_duplicate,
            exception_code=exception.exception_code,
            counterparty=item.payee, known_counterparties=known_counterparties,
            is_stale=is_stale)
        category, reason = assign_category(risk_level=risk.level, kind="exception", source="rule")

        explanation = (f"{exception.explanation} Suggested action: {exception.suggested_action}. "
                       f"Routed to {category}: {reason}.")
        if risk.triggered != ["RR-07"]:
            explanation += " Risk rules triggered: " + ", ".join(risk.reasons()) + "."

        recommendation_id = self.repositories.recommendations.create(
            connection, run_id=run_id, subject_item_type=item.item_type,
            subject_item_id=item.item_id, kind="exception", source="rule",
            category_code=category, risk_level=risk.level, explanation=explanation,
            created_at=now, rule_name=exception.rule_name,
            exception_code=exception.exception_code, risk_rules=risk.as_json())

        self.repositories.items.set_status(connection, item.item_type, item.item_id, "proposed")

        self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
            event_type="RULE", process_code="P3", entity_type="recommendation",
            entity_id=recommendation_id, rule_name=exception.rule_name,
            item_refs=[item.external_id], risk_level=risk.level,
            explanation=exception.explanation,
            revised_values={"exception_code": exception.exception_code,
                            "next_action": category_detail.next_action},
            previous_status="validated", new_status="proposed"))

        summary.exceptions += 1
        summary.recommendations += 1
        summary.by_category[category] = summary.by_category.get(category, 0) + 1
        summary.by_exception[exception.exception_code] = \
            summary.by_exception.get(exception.exception_code, 0) + 1
        summary.by_risk[risk.level] = summary.by_risk.get(risk.level, 0) + 1
        summary.by_rule[exception.rule_name] = summary.by_rule.get(exception.rule_name, 0) + 1

    # -- loading ------------------------------------------------------------
    def _load_items(self, run_id: int) -> dict[str, list[Item]]:
        loaded: dict[str, list[Item]] = {}
        for item_type in ("bank", "ledger", "carry_in"):
            rows = self.repositories.items.list(run_id, item_type, statuses=("validated",))
            loaded[item_type] = [Item.from_row(item_type, row) for row in rows]
        return loaded

    @staticmethod
    def _known_counterparties(items: dict[str, list[Item]]) -> set[str]:
        """Counterparties seen more than once in the period.

        A name appearing once is novel by definition, so requiring at least two
        appearances is what makes RR-04 meaningful rather than flagging everything.
        """
        counts = Counter(item.payee for group in items.values() for item in group if item.payee)
        return {payee for payee, count in counts.items() if count > 1}
