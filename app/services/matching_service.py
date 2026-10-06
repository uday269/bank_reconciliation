"""Matching pass: deterministic rules, then scored candidates (UC-03, P3, P4 and P5).

Order is fixed and matters. Rules run first and settle everything they can prove, so
scoring never reopens a question a rule already answered (ADR-05). Only the leftovers
reach the scoring layer, which generates candidates, scores them on five features, ranks
them, converts the leading score to a calibrated confidence and writes an explanation
that always includes conflicting evidence.

Nothing here decides anything. Every recommendation, scored or not, goes to a review
queue and waits for a person (CR-01). Confidence orders the work; it never replaces
approval.

Running with `use_scoring=False` reproduces the Stage 6 baseline exactly, which is what
makes the rules-only and hybrid comparison in DOC-07 a like-for-like measurement.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from app.config import Config
from app.domain import exceptions as exception_catalog
from app.domain.calibration import Calibrator
from app.domain.candidates import generate_all
from app.domain.explanation import explain_match
from app.domain.risk import assess_risk, assign_category
from app.domain.rules import (
    Item, RuleException, RuleMatch, apply_rules, classify_unmatched_bank_item,
    classify_unmatched_ledger_item, classify_period_end_item,
)
from app.domain.scoring import Ranking, Weights, rank_all
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database, insert
from app.infra.repositories import CandidateInput, Repositories


@dataclass
class MatchingSummary:
    run_id: int
    rule_matches: int = 0
    scored_matches: int = 0
    exceptions: int = 0
    unmatched_for_scoring: int = 0
    recommendations: int = 0
    candidates_generated: int = 0
    capped_searches: int = 0
    calibration_version: str = "uncalibrated"
    by_category: dict[str, int] = field(default_factory=dict)
    by_exception: dict[str, int] = field(default_factory=dict)
    by_risk: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    by_confidence_band: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"Run {self.run_id}: {self.rule_matches} rule matches, "
                f"{self.scored_matches} scored matches, {self.exceptions} exceptions, "
                f"{self.recommendations} recommendations")


class MatchingService:
    def __init__(self, database: Database, repositories: Repositories,
                 audit: AuditLog, config: Config, calibrator: Calibrator | None = None):
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.config = config
        # No artefact means the raw score is reported and the run is labelled
        # "uncalibrated", so no report can claim a calibrated figure that was never fitted.
        self.calibrator = calibrator or Calibrator.identity()

    # -- entry point --------------------------------------------------------
    def run_rules(self, run_id: int, performed_by: int | None = None,
                  use_scoring: bool = True) -> MatchingSummary:
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

        summary = MatchingSummary(run_id=run_id,
                                  calibration_version=self.calibrator.version_label)
        context = RunContext.load(self.database, run_id)
        now = utc_now()

        # Candidates are generated before the transaction opens: scoring is pure
        # computation and holding a write lock through it would serialize nothing useful.
        rankings: dict[tuple[str, int], Ranking] = {}
        if use_scoring:
            rankings = self._score_unmatched(outcome, summary)

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

            # 3. Scored matches, where the leading candidate is worth proposing.
            consumed_by_scoring: set[tuple[str, int]] = set()
            for key, ranking in rankings.items():
                if key in classified or ranking.best is None:
                    continue
                members = [(member.item_type, member.item_id) for member in ranking.best.members]
                if any(member in consumed_by_scoring for member in members):
                    continue                     # an earlier item already claimed this record
                self._store_scored_match(connection, run_id, ranking, known_counterparties,
                                         summary, now)
                consumed_by_scoring.add(key)
                consumed_by_scoring.update(members)

            # 4. Whatever is still unmatched becomes an exception for a reviewer. A capped
            #    search is reported as EXC-08 rather than as "nothing found" (DD-07).
            for item in outcome.unmatched_bank:
                key = (item.item_type, item.item_id)
                if key in classified or key in consumed_by_scoring:
                    continue
                ranking = rankings.get(key)
                if ranking is not None and ranking.search_capped:
                    exception = RuleException(
                        rule_name="BR-05", subject=item, exception_code="EXC-08",
                        explanation=f"The candidate search was limited: {ranking.cap_reason}.",
                        suggested_action="Investigate manually; the automated search was capped")
                else:
                    exception = classify_unmatched_bank_item(item, known_counterparties)
                self._store_exception(connection, run_id, exception, known_counterparties,
                                      summary, now)
                summary.unmatched_for_scoring += 1

            for item in outcome.unmatched_ledger:
                key = (item.item_type, item.item_id)
                if key in classified or key in consumed_by_scoring:
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
                    "scored_matches": summary.scored_matches,
                    "exceptions": summary.exceptions,
                    "recommendations": summary.recommendations,
                    "candidates_generated": summary.candidates_generated,
                    "capped_searches": summary.capped_searches,
                    "calibration_version": summary.calibration_version,
                    "scoring_enabled": use_scoring,
                    "by_category": summary.by_category,
                    "by_risk": summary.by_risk,
                    "by_confidence_band": summary.by_confidence_band,
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
                         now: str, previous_status: str = "validated") -> int:
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
            previous_status=previous_status, new_status="proposed"))

        summary.exceptions += 1
        summary.recommendations += 1
        summary.by_category[category] = summary.by_category.get(category, 0) + 1
        summary.by_exception[exception.exception_code] = \
            summary.by_exception.get(exception.exception_code, 0) + 1
        summary.by_risk[risk.level] = summary.by_risk.get(risk.level, 0) + 1
        summary.by_rule[exception.rule_name] = summary.by_rule.get(exception.rule_name, 0) + 1
        return recommendation_id

    # -- re-proposal after a reviewer decision -------------------------------
    def requeue_as_exceptions(self, connection, run_id: int, items: list[tuple[str, int]],
                              reason: str, now: str) -> list[int]:
        """Re-propose items a reviewer released, each as a new exception recommendation.

        Called when a proposed match is rejected, or when a reviewer selects a different
        candidate and the original counterparts are no longer part of it. Each item is
        classified with the same rules matching uses (BR-08..BR-12) and routed by BR-13,
        so a released item lands in the queue it would have reached had matching never
        paired it. The original recommendation is kept, superseded, as evidence.

        Parameters come from configuration, as they do for matching, so a re-proposal
        and the original run classify an item the same way.
        """
        if not items:
            return []
        parameters = self.config.parameters
        run = self.repositories.runs.get(run_id, connection)
        period_end = date.fromisoformat(run["period_end"])
        loaded = {item_type: [Item.from_row(item_type, row)
                              for row in self.repositories.items.list(run_id, item_type)
                              if row["status"] != "excluded"]
                  for item_type in ("bank", "ledger", "carry_in")}
        known_counterparties = self._known_counterparties(loaded)
        by_key = {(item.item_type, item.item_id): item
                  for group in loaded.values() for item in group}

        summary = MatchingSummary(run_id=run_id)
        created: list[int] = []
        for key in items:
            item = by_key[key]
            if item.item_type == "bank":
                exception = classify_unmatched_bank_item(item, known_counterparties)
            else:
                exception = (classify_period_end_item(
                    item, period_end, parameters.outstanding_check_stale_days,
                    parameters.deposit_in_transit_stale_days)
                    or classify_unmatched_ledger_item(item))
            exception = replace(exception, explanation=f"{reason} {exception.explanation}")
            previous = self.repositories.items.get(item.item_type, item.item_id, connection)["status"]
            created.append(self._store_exception(connection, run_id, exception,
                                                 known_counterparties, summary, now,
                                                 previous_status=previous))
        return created

    # -- scoring ------------------------------------------------------------
    def _score_unmatched(self, outcome, summary: MatchingSummary) -> dict[tuple[str, int], Ranking]:
        """Generate and rank candidates for every item the rules could not settle.

        Bank items are the subjects: each is offered the unmatched ledger entries, which
        covers one-to-one, one-to-many and, through the group search, many-to-one.
        """
        parameters = self.config.parameters
        weights = Weights.from_config(self.config.scoring)

        candidate_sets = generate_all(
            outcome.unmatched_bank, outcome.unmatched_ledger,
            window_business_days=parameters.timing_window_business_days,
            near_miss_tolerance_cents=self.config.matching.near_miss_tolerance_cents,
            candidate_cap=parameters.group_candidate_cap,
            member_cap=parameters.group_member_cap)

        rankings = rank_all(
            candidate_sets, weights,
            window_business_days=parameters.timing_window_business_days,
            one_to_one_score=self.config.scoring.relationship_score_one_to_one,
            group_score=self.config.scoring.relationship_score_group)

        summary.candidates_generated = sum(len(ranking.scored) for ranking in rankings)
        summary.capped_searches = sum(1 for ranking in rankings if ranking.search_capped)

        # Strongest first, so when two items compete for the same ledger entry the better
        # supported pairing takes it. Deterministic: ties fall back to the subject id.
        ordered = sorted(rankings,
                         key=lambda ranking: (-(ranking.best.score if ranking.best else 0.0),
                                              ranking.subject.item_id))
        return {(ranking.subject.item_type, ranking.subject.item_id): ranking
                for ranking in ordered}

    def _store_scored_match(self, connection, run_id: int, ranking: Ranking,
                            known_counterparties: set[str], summary: MatchingSummary,
                            now: str) -> None:
        """Store a scored recommendation with every candidate it considered."""
        subject = ranking.subject
        best = ranking.best
        parameters = self.config.parameters

        confidence = self.calibrator.confidence(best.score)
        runner_up_confidence = (self.calibrator.confidence(ranking.runner_up.score)
                                if ranking.runner_up else None)

        risk = assess_risk(
            amount_cents=subject.amount_cents,
            senior_approval_amount_cents=parameters.senior_approval_amount_cents,
            is_possible_duplicate=subject.is_possible_duplicate
            or any(member.is_possible_duplicate for member in best.members),
            is_group=best.is_group,
            counterparty=subject.payee, known_counterparties=known_counterparties)

        category, reason = assign_category(
            risk_level=risk.level, kind="match", source="ai",
            confidence=confidence, runner_up_confidence=runner_up_confidence,
            high_confidence_band=parameters.confidence_high_band,
            ambiguity_margin=parameters.ambiguity_margin)

        explanation = explain_match(ranking,
                                    window_business_days=parameters.timing_window_business_days,
                                    ambiguity_margin=parameters.ambiguity_margin)
        band = self.calibrator.band(confidence, parameters.confidence_high_band,
                                    parameters.confidence_low_band)
        text = (f"{explanation.as_text()} Confidence {confidence:.2f} ({band} band), "
                f"routed to {category}: {reason}.")
        if risk.triggered != ["RR-07"]:
            text += " Risk rules triggered: " + ", ".join(risk.reasons()) + "."

        model_version_id = self._model_version_id(connection)

        recommendation_id = self.repositories.recommendations.create(
            connection, run_id=run_id, subject_item_type=subject.item_type,
            subject_item_id=subject.item_id, kind="match", source="ai",
            category_code=category, risk_level=risk.level, explanation=text, created_at=now,
            relationship=best.relationship, model_version_id=model_version_id,
            risk_rules=risk.as_json(), confidence=confidence,
            candidates=[CandidateInput(
                rank_order=scored.rank, score=scored.score,
                confidence=self.calibrator.confidence(scored.score),
                feature_values=scored.features.as_json(),
                members=[(subject.item_type, subject.item_id)]
                + [(member.item_type, member.item_id) for member in scored.members])
                for scored in ranking.scored])

        for item in (subject, *best.members):
            self.repositories.items.set_status(connection, item.item_type, item.item_id,
                                               "proposed")

        self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
            event_type="AI_RECOMMENDATION", process_code="P4", entity_type="recommendation",
            entity_id=recommendation_id, model_version_id=model_version_id,
            item_refs=[subject.external_id] + [member.external_id for member in best.members],
            candidate_scores=ranking.as_candidate_scores(), confidence=confidence,
            risk_level=risk.level, explanation=explanation.as_text(),
            previous_status="validated", new_status="proposed"))

        summary.scored_matches += 1
        summary.recommendations += 1
        summary.by_category[category] = summary.by_category.get(category, 0) + 1
        summary.by_risk[risk.level] = summary.by_risk.get(risk.level, 0) + 1
        summary.by_rule["scored"] = summary.by_rule.get("scored", 0) + 1
        summary.by_confidence_band[band] = summary.by_confidence_band.get(band, 0) + 1

    def _model_version_id(self, connection) -> int:
        """Record which calibration artefact produced these confidences (FR-AI-07).

        The lookup runs on the connection the caller is writing through, not on the
        database object. The first scored match of a run inserts the row and the rest
        find it, which only works if the read happens inside the same transaction: a read
        on any other connection would not see an uncommitted insert, would insert a second
        row, and would fail the unique constraint on version_label, rolling back the run.

        Today `Database` holds a single connection, so reading either way behaves the same.
        Writing it this way removes the dependency on that remaining true.
        """
        label = self.calibrator.version_label
        row = connection.execute(
            "SELECT model_version_id FROM model_version WHERE version_label = ?",
            (label,)).fetchone()
        if row:
            return row["model_version_id"]
        return insert(connection, "model_version", {
            "version_label": label, "algorithm": self.calibrator.method,
            "calibration_dataset": self.calibrator.dataset or "none",
            "calibration_seed": self.config.model.calibration_seed,
            "trained_at": self.calibrator.fitted_at or utc_now(),
            "notes": self.calibrator.notes})

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
