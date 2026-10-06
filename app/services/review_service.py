"""Human review of recommendations (UC-04..UC-07, P6, FR-REV-01..11).

AI recommends; this service is where people authorize. Every decision is a new
`review_decision` row and a DECISION audit event, written in one transaction with the
status changes it causes (CR-08). Nothing here decides on a person's behalf.

Decisions
    approve     the leading candidate (or the exception disposition) is accepted
    modify      a different candidate of the same recommendation is accepted (FR-REV-07)
    reject      the proposed pairing is wrong; the items are re-proposed as exceptions
    escalate    the item goes to the senior queue (FR-REV-08)
    unresolved  the item stays open as a reconciling difference
    manual match  an exception is paired with records the reviewer selects; recorded
                as 'modify' with a reviewer-selected, unscored candidate

Reject and modify apply to matches only: an exception has no pairing to reject. The
original recommendation and every candidate are kept; a rejected recommendation is
marked superseded, never deleted.

Refusals
    Every check runs inside the transaction, on its connection, so it sees exactly the
    state the write would change. A `ControlViolation` rolls the transaction back, then
    a BLOCKED_ATTEMPT event is written in a transaction of its own and the violation is
    re-raised. A refused action therefore leaves evidence that the control fired and
    no other trace (ADR-12).

Batch approval (DD-02, pending confirmation)
    One reviewer action writes one `review_batch` row and, per item, one decision and
    one audit event. A different ruling changes `approve_batch`, `reject_batch` and the
    W4 screen only.

Timing (DD-04)
    `open_detail` writes a DETAIL_OPENED event. A decision takes its `opened_at` from
    that event for the same user, never from a value the browser sends, and stores it
    beside `decided_at` so time per item is measured, not estimated.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from app.control import ControlViolation
from app.control import duties, period_lock, permissions, states
from app.domain.manual_match import check_manual_match
from app.domain.rules import Item
from app.infra.audit import AuditEvent, AuditLog, RunContext, utc_now
from app.infra.db import Database
from app.infra.repositories import CandidateInput, Repositories, RepositoryError
from app.services.blocked import record_blocked
from app.services.matching_service import MatchingService

APPROVAL_STATUS = {"approve": "approved", "modify": "approved", "reject": "rejected",
                   "escalate": "escalated", "unresolved": "unresolved"}
ITEM_STATUS_AFTER = {"approve": "approved", "modify": "approved", "escalate": "escalated",
                     "unresolved": "unresolved", "reject": "proposed"}
RECOMMENDATION_STATUS_AFTER = {"approve": "decided", "modify": "decided", "unresolved": "decided",
                               "escalate": "open", "reject": "superseded"}
EXTERNAL_COLUMN = {"bank": "external_txn_id", "ledger": "external_entry_id",
                   "carry_in": "external_item_id"}
MEMBER_COLUMNS = (("bank", "bank_transaction_id"), ("ledger", "ledger_entry_id"),
                  ("carry_in", "carry_in_item_id"))
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}

Key = tuple[str, int]


class ManualMatchError(ValueError):
    """A hand-selected pairing does not reconcile. Every problem is listed; nothing is written."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class DecisionOutcome:
    recommendation_id: int
    review_decision_id: int
    decision: str
    decision_level: str
    previous_status: str
    new_status: str
    requeued: list[int] = field(default_factory=list)       # new exception recommendations
    superseded: list[int] = field(default_factory=list)     # recommendations replaced by a modify


@dataclass(frozen=True)
class BatchOutcome:
    review_batch_id: int
    decision: str
    outcomes: list[DecisionOutcome]


class ReviewService:
    def __init__(self, database: Database, repositories: Repositories, audit: AuditLog,
                 matching: MatchingService):
        self.database = database
        self.repositories = repositories
        self.audit = audit
        self.matching = matching          # re-proposes released items with the matching rules

    # -- queues -------------------------------------------------------------
    def queue(self, run_id: int, category_code: str) -> list[sqlite3.Row]:
        """First-level queue for one category, excluding items already escalated."""
        return [row for row in self.repositories.recommendations.queue(run_id, category_code)
                if category_code == "CAT-05" or self._subject_status(row) != "escalated"]

    def senior_queue(self, run_id: int) -> list[sqlite3.Row]:
        """CAT-05 plus every escalated item, ordered like any queue (FR-REV-02, FR-REV-08).

        Ordering only; nothing is filtered by confidence (CR-15).
        """
        rows = list(self.repositories.recommendations.queue(run_id, "CAT-05"))
        for category in ("CAT-01", "CAT-02", "CAT-03", "CAT-04"):
            rows += [row for row in self.repositories.recommendations.queue(run_id, category)
                     if self._subject_status(row) == "escalated"]
        return sorted(rows, key=lambda row: (RISK_ORDER[row["risk_level"]],
                                             1.0 if row["confidence"] is None else row["confidence"],
                                             row["recommendation_id"]))

    # -- detail view --------------------------------------------------------
    def open_detail(self, recommendation_id: int, user_id: int, at: str | None = None) -> str:
        """Record that a reviewer opened an item's detail view (FR-REV-10, FR-REV-11)."""
        at = at or utc_now()
        recommendation = self.repositories.recommendations.get(recommendation_id)
        with self.database.transaction() as connection:
            user = self.repositories.users.get(user_id, connection)
            permissions.require_role(user, permissions.VIEW)
            self.audit.write(connection, RunContext.load(self.database, recommendation["run_id"]),
                             AuditEvent(event_type="DETAIL_OPENED", process_code="P6",
                                        entity_type="recommendation", entity_id=recommendation_id,
                                        actor_type="human", actor_user_id=user_id, created_at=at))
        return at

    # -- single decisions ---------------------------------------------------
    def decide(self, recommendation_id: int, user_id: int, decision: str,
               comment: str | None = None, chosen_candidate_id: int | None = None,
               at: str | None = None) -> DecisionOutcome:
        """Record one reviewer decision (UC-04..UC-07)."""
        at = at or utc_now()
        recommendation = self.repositories.recommendations.get(recommendation_id)
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(recommendation["run_id"], connection)
                outcome = self._apply(connection, run, user, recommendation_id, decision,
                                      comment, chosen_candidate_id, at, batch_id=None)
                self._return_to_review(connection, run)
                return outcome
        except ControlViolation as violation:
            self._record_blocked(recommendation["run_id"], user_id, "recommendation",
                                 recommendation_id, decision, comment, violation)
            raise

    # -- batches (DD-02, pending) -------------------------------------------
    def approve_batch(self, run_id: int, user_id: int, recommendation_ids: list[int],
                      comment: str | None = None, at: str | None = None) -> BatchOutcome:
        return self._batch(run_id, user_id, recommendation_ids, "approve", comment, at)

    def reject_batch(self, run_id: int, user_id: int, recommendation_ids: list[int],
                     comment: str | None, at: str | None = None) -> BatchOutcome:
        return self._batch(run_id, user_id, recommendation_ids, "reject", comment, at)

    def _batch(self, run_id: int, user_id: int, recommendation_ids: list[int], decision: str,
               comment: str | None, at: str | None) -> BatchOutcome:
        at = at or utc_now()
        ids = list(dict.fromkeys(recommendation_ids))           # a repeated id counts once
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(run_id, connection)
                permissions.require_role(user, permissions.BATCH)
                period_lock.require_writable(run, "approve or reject a batch")
                permissions.require_comment(decision, "low", comment)

                rows, statuses = [], {}
                for rec_id in ids:
                    try:
                        row = self.repositories.recommendations.get(rec_id, connection)
                    except RepositoryError:
                        raise ControlViolation("CR-11", f"recommendation {rec_id} does not exist")
                    rows.append(row)
                    statuses[rec_id] = self._subject_status(row, connection)
                permissions.require_batch_eligible(run_id, rows, statuses)

                batch_id = self.repositories.decisions.create_batch(connection, run_id, user_id,
                                                                    at, len(rows))
                outcomes = [self._apply(connection, run, user, row["recommendation_id"], decision,
                                        comment, None, at, batch_id=batch_id)
                            for row in rows]
                self._return_to_review(connection, run)
                return BatchOutcome(batch_id, decision, outcomes)
        except ControlViolation as violation:
            self._record_blocked(run_id, user_id, "reconciliation_run", run_id,
                                 f"batch {decision}", comment, violation, recommendation_ids=ids)
            raise

    # -- manual pairing ----------------------------------------------------
    def manual_match_options(self, recommendation_id: int, limit: int = 50) -> list[sqlite3.Row]:
        """Records a reviewer could pair with an exception: other side, same direction of
        cash, and free (proposed by nothing but their own exception). Nearest date first.

        This is a picker, not a recommendation: it is ordered by date and amount only and
        carries no score, so it cannot be mistaken for the AI's view.
        """
        recommendation = self.repositories.recommendations.get(recommendation_id)
        subject_row = self.repositories.items.get(recommendation["subject_item_type"],
                                                  recommendation["subject_item_id"])
        subject = Item.from_row(recommendation["subject_item_type"], subject_row)
        other = ("ledger", "carry_in") if subject.item_type == "bank" else ("bank",)
        options = []
        for item_type in other:
            for row in self.repositories.items.list(recommendation["run_id"], item_type,
                                                    statuses=("proposed", "escalated")):
                item = Item.from_row(item_type, row)
                if (item.cash_effect > 0) != (subject.cash_effect > 0):
                    continue
                if self._is_free(recommendation["run_id"], (item_type, item.item_id)):
                    options.append(((abs((item.business_date - subject.business_date).days),
                                     abs(abs(item.cash_effect) - abs(subject.cash_effect)),
                                     item.external_id), row))
        options.sort(key=lambda option: option[0])
        return [row for _, row in options[:limit]]

    def _is_free(self, run_id: int, key: Key, connection: sqlite3.Connection | None = None) -> bool:
        """Proposed by nothing except its own exception recommendation."""
        return all(holder["kind"] == "exception"
                   and (holder["subject_item_type"], holder["subject_item_id"]) == key
                   for holder in self.repositories.recommendations.open_holding(run_id, *key, connection))

    def manual_match(self, recommendation_id: int, user_id: int, counterparts: list[Key],
                     comment: str | None, at: str | None = None) -> DecisionOutcome:
        """Pair an exception with records the reviewer selects (EXC-08 and missed groups).

        The pairing is added to the recommendation as a reviewer-selected candidate, with
        no score, beside anything the system proposed. It is then recorded as a 'modify'
        decision, so every rule that governs selecting a candidate applies: comment,
        detail view, level, separation of duties, and records that must be free. The
        arithmetic must agree to the cent or nothing is written.
        """
        at = at or utc_now()
        recommendation = self.repositories.recommendations.get(recommendation_id)
        try:
            with self.database.transaction() as connection:
                user = self.repositories.users.get(user_id, connection)
                run = self.repositories.runs.get(recommendation["run_id"], connection)
                recommendation = self.repositories.recommendations.get(recommendation_id, connection)
                period_lock.require_writable(run, "record a decision")
                if recommendation["status"] != "open":
                    raise ControlViolation("STATE", f"recommendation {recommendation_id} has already been decided")
                permissions.require_manual_match_permitted(
                    user, recommendation, self._subject_status(recommendation, connection))

                subject_key = (recommendation["subject_item_type"], recommendation["subject_item_id"])
                subject = Item.from_row(subject_key[0], self.repositories.items.get(*subject_key, connection))
                selected = []
                for item_type, item_id in counterparts:
                    try:
                        selected.append(Item.from_row(item_type, self.repositories.items.get(
                            item_type, item_id, connection)))
                    except (RepositoryError, KeyError):
                        raise ManualMatchError([f"{item_type} record {item_id} does not exist"])
                for counterpart in selected:
                    key = (counterpart.item_type, counterpart.item_id)
                    if not self._is_free(run["run_id"], key, connection):
                        raise ControlViolation(
                            "STATE", f"{counterpart.external_id} is proposed in another match; "
                                     "decide that recommendation first")
                problems = check_manual_match(subject, selected)
                if problems:
                    raise ManualMatchError(problems)

                existing = self.repositories.recommendations.candidates(recommendation_id, connection)
                dates = [subject.business_date] + [item.business_date for item in selected]
                candidate_id = self.repositories.recommendations.add_candidate(
                    connection, recommendation_id, CandidateInput(
                        rank_order=max((row["rank_order"] for row in existing), default=0) + 1,
                        score=0.0, confidence=None,
                        feature_values=json.dumps({
                            "origin": "reviewer", "scored": False,
                            "selected_by": user_id, "member_count": len(selected),
                            "day_span": (max(dates) - min(dates)).days}, sort_keys=True),
                        members=[subject_key] + [(item.item_type, item.item_id) for item in selected]))

                outcome = self._apply(connection, run, user, recommendation_id, "modify", comment,
                                      candidate_id, at, batch_id=None, manual=True)
                self._return_to_review(connection, run)
                return outcome
        except ControlViolation as violation:
            self._record_blocked(recommendation["run_id"], user_id, "recommendation",
                                 recommendation_id, "manual match", comment, violation)
            raise

    # -- the decision itself ------------------------------------------------
    def _apply(self, connection: sqlite3.Connection, run: sqlite3.Row, user: sqlite3.Row,
               recommendation_id: int, decision: str, comment: str | None,
               chosen_candidate_id: int | None, at: str, batch_id: int | None,
               manual: bool = False) -> DecisionOutcome:
        recs = self.repositories.recommendations
        recommendation = recs.get(recommendation_id, connection)
        run_id = run["run_id"]

        # Gates, in the order a reviewer would want them explained.
        period_lock.require_writable(run, "record a decision")
        if recommendation["status"] != "open":
            raise ControlViolation("STATE", f"recommendation {recommendation_id} has already been decided")
        subject_status = self._subject_status(recommendation, connection)
        if manual:
            level = permissions.require_manual_match_permitted(user, recommendation, subject_status)
        else:
            level = permissions.require_decision_permitted(user, recommendation, subject_status, decision)
        permissions.require_comment(decision, recommendation["risk_level"], comment)

        opened = self.audit.latest_event(run_id, "DETAIL_OPENED", "recommendation",
                                         recommendation_id, user["user_id"], connection)
        opened_at = opened["created_at"] if opened else None
        if batch_id is None:
            permissions.require_detail_opened(recommendation, decision, opened_at)

        first_level = [row["decided_by"] for row in
                       self.repositories.decisions.for_recommendation(recommendation_id, connection)
                       if row["decision_level"] == "first"]
        if level == "senior":
            duties.require_independent_senior(
                user["user_id"], self.repositories.decisions.escalated_by(recommendation_id, connection),
                first_level)

        leading_id, leading = self._leading(recommendation, connection)
        accepted: list[Key] = leading
        if decision == "modify":
            accepted = self._resolve_modify(connection, recommendation, leading_id, chosen_candidate_id)
        elif chosen_candidate_id is not None:
            raise ControlViolation("STATE", "a candidate is selected only when modifying a match")

        # Settling records settles every other open proposal for them. An exception raised
        # for the same record (a possible-duplicate flag, or a re-proposal) is superseded;
        # another open match is refused, so two decisions can never claim one record.
        superseded: list[int] = []
        if decision in ("approve", "modify", "unresolved"):
            superseded = self._displace(connection, recommendation, accepted)
            self._require_senior_for(connection, user, superseded)

        released = [key for key in leading if key not in accepted] if decision == "modify" else \
            (list(leading) if decision == "reject" else [])

        # Status changes, each checked against the DOC-03 state model.
        new_item_status = ITEM_STATUS_AFTER[decision]
        for item_type, item_id in (accepted if decision != "reject" else []):
            current = self.repositories.items.get(item_type, item_id, connection)["status"]
            states.require_item_transition(current, new_item_status)
            self.repositories.items.set_status(connection, item_type, item_id, new_item_status)
        for item_type, item_id in released:
            current = self.repositories.items.get(item_type, item_id, connection)["status"]
            states.require_item_transition(current, "proposed")
        for rec_id in superseded:
            recs.set_status(connection, rec_id, "superseded")
        recs.set_status(connection, recommendation_id, RECOMMENDATION_STATUS_AFTER[decision])

        # A released record that already has its own open exception keeps it; only the
        # others need a new one.
        released = [key for key in released if not self._own_open_exception(run_id, key, connection)]
        requeued: list[int] = []
        if released:
            reason = (f"Released from recommendation {recommendation_id}, "
                      f"{'rejected' if decision == 'reject' else 'modified'} by a reviewer.")
            requeued = self.matching.requeue_as_exceptions(connection, run_id, released, reason, at)

        subject_key = (recommendation["subject_item_type"], recommendation["subject_item_id"])
        subject_new = self.repositories.items.get(*subject_key, connection)["status"]
        decision_id = self.repositories.decisions.add(
            connection, run_id=run_id, recommendation_id=recommendation_id, decision=decision,
            decision_level=level, decided_by=user["user_id"], decided_at=at,
            previous_status=subject_status, new_status=subject_new, comment=comment,
            chosen_candidate_id=chosen_candidate_id, review_batch_id=batch_id, opened_at=opened_at)

        evidence = [f"recommendation:{recommendation_id}", f"review_decision:{decision_id}"]
        if leading_id:
            evidence.append(f"candidate:{leading_id}")
        if chosen_candidate_id:
            evidence.append(f"candidate:{chosen_candidate_id}")
        if batch_id:
            evidence.append(f"review_batch:{batch_id}")
        self.audit.write(connection, RunContext.load(self.database, run_id), AuditEvent(
            event_type="DECISION", process_code="P6", entity_type="recommendation",
            entity_id=recommendation_id, actor_type="human", actor_user_id=user["user_id"],
            item_refs=self._external_ids(sorted(set(leading) | set(accepted)), connection),
            rule_name=recommendation["rule_name"], model_version_id=recommendation["model_version_id"],
            confidence=recommendation["confidence"], risk_level=recommendation["risk_level"],
            explanation=recommendation["explanation"], decision=decision, comment=comment,
            approval_status=APPROVAL_STATUS[decision], evidence_refs=evidence,
            original_values={"recommendation_status": "open", "leading_candidate_id": leading_id},
            revised_values={"recommendation_status": RECOMMENDATION_STATUS_AFTER[decision],
                            "decision_level": level, "chosen_candidate_id": chosen_candidate_id,
                            "opened_at": opened_at, "review_batch_id": batch_id,
                            "requeued_recommendations": requeued,
                            "superseded_recommendations": superseded,
                            "manual_match": manual},
            previous_status=subject_status, new_status=subject_new, created_at=at))

        return DecisionOutcome(recommendation_id, decision_id, decision, level, subject_status,
                               subject_new, requeued, superseded)

    def _resolve_modify(self, connection: sqlite3.Connection, recommendation: sqlite3.Row,
                        leading_id: int | None, chosen_candidate_id: int | None) -> list[Key]:
        """The selected candidate must belong to this recommendation and differ from the proposal."""
        candidates = {row["candidate_id"] for row in
                      self.repositories.recommendations.candidates(recommendation["recommendation_id"], connection)}
        if chosen_candidate_id is None or chosen_candidate_id not in candidates:
            raise ControlViolation("STATE", "modify requires selecting one of this item's candidates")
        if chosen_candidate_id == leading_id:
            raise ControlViolation("STATE", "the selected candidate is already the proposal; approve it instead")
        return self._members(chosen_candidate_id, connection)

    def _displace(self, connection: sqlite3.Connection, recommendation: sqlite3.Row,
                  keys: list[Key]) -> list[int]:
        """Open exceptions for these records that the decision supersedes.

        Each record must still be awaiting a decision. Any other open match proposing it
        refuses the decision: that match has to be decided first.
        """
        superseded: set[int] = set()
        for key in keys:
            status = self.repositories.items.get(*key, connection)["status"]
            if status not in ("proposed", "escalated"):
                ref = self._external_ids([key], connection)[0]
                raise ControlViolation("STATE", f"{ref} is already {status}")
            for holder in self.repositories.recommendations.open_holding(
                    recommendation["run_id"], *key, connection):
                if holder["recommendation_id"] == recommendation["recommendation_id"]:
                    continue
                if holder["kind"] == "exception" and (holder["subject_item_type"],
                                                      holder["subject_item_id"]) == key:
                    superseded.add(holder["recommendation_id"])
                    continue
                ref = self._external_ids([key], connection)[0]
                raise ControlViolation(
                    "STATE", f"{ref} is proposed in recommendation {holder['recommendation_id']}; "
                             "decide that recommendation first")
        return sorted(superseded)

    def _require_senior_for(self, connection: sqlite3.Connection, user: sqlite3.Row,
                            superseded: list[int]) -> None:
        """Superseding an exception settles it, so its own approval rules apply (CR-02, CR-03)."""
        for holder_id in superseded:
            holder = self.repositories.recommendations.get(holder_id, connection)
            if permissions.decision_level(holder, self._subject_status(holder, connection)) != "senior":
                continue
            if not permissions.is_permitted(user["role_code"], permissions.DECIDE_SENIOR):
                raise ControlViolation(
                    "CR-02", f"this decision would settle recommendation {holder_id}, which "
                             "requires a Senior Accountant or Controller decision")
            duties.require_independent_senior(
                user["user_id"], self.repositories.decisions.escalated_by(holder_id, connection),
                [row["decided_by"] for row in self.repositories.decisions.for_recommendation(holder_id, connection)
                 if row["decision_level"] == "first"])

    def _own_open_exception(self, run_id: int, key: Key, connection: sqlite3.Connection) -> bool:
        return any(holder["kind"] == "exception" and (holder["subject_item_type"], holder["subject_item_id"]) == key
                   for holder in self.repositories.recommendations.open_holding(run_id, *key, connection))

    # -- helpers ------------------------------------------------------------
    def _leading(self, recommendation: sqlite3.Row,
                 connection: sqlite3.Connection) -> tuple[int | None, list[Key]]:
        """The leading candidate and its members; an exception holds only its subject."""
        candidates = self.repositories.recommendations.candidates(
            recommendation["recommendation_id"], connection)
        if candidates and recommendation["kind"] == "match":
            leading_id = candidates[0]["candidate_id"]
            return leading_id, self._members(leading_id, connection)
        return None, [(recommendation["subject_item_type"], recommendation["subject_item_id"])]

    def _members(self, candidate_id: int, connection: sqlite3.Connection) -> list[Key]:
        members = []
        for row in self.repositories.recommendations.candidate_members(candidate_id, connection):
            for item_type, column in MEMBER_COLUMNS:
                if row[column] is not None:
                    members.append((item_type, int(row[column])))
        return members

    def _subject_status(self, recommendation: sqlite3.Row,
                        connection: sqlite3.Connection | None = None) -> str:
        return self.repositories.items.get(recommendation["subject_item_type"],
                                           recommendation["subject_item_id"], connection)["status"]

    def _external_ids(self, keys: list[Key], connection: sqlite3.Connection) -> list[str]:
        return [self.repositories.items.get(item_type, item_id, connection)[EXTERNAL_COLUMN[item_type]]
                for item_type, item_id in keys]

    def _return_to_review(self, connection: sqlite3.Connection, run: sqlite3.Row) -> None:
        """A decision recorded while the run was ready to close sends it back to review."""
        if run["status"] == "ready_to_close":
            states.require_run_transition("ready_to_close", "in_review")
            self.repositories.runs.set_status(connection, run["run_id"], "in_review")

    def _record_blocked(self, run_id: int, user_id: int, entity_type: str, entity_id: int,
                        attempted: str, comment: str | None, violation: ControlViolation,
                        recommendation_ids: list[int] | None = None) -> None:
        extra = {"recommendation_ids": recommendation_ids} if recommendation_ids is not None else None
        record_blocked(self.database, self.audit, run_id=run_id, user_id=user_id,
                       process_code="P6", entity_type=entity_type, entity_id=entity_id,
                       attempted=attempted, violation=violation, comment=comment, extra=extra)
