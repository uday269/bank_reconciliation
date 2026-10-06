"""Plain-language explanations with conflicting evidence (FR-AI-06, CR-15, NFR-06).

Two rules shape this module.

Conflicting evidence is never omitted. A reviewer who is shown only the reasons a
recommendation looks right cannot judge it, and a system that hides doubt trains people
to approve without reading (RSK-01). Every detector below runs on every recommendation,
whatever the score.

Explanations state facts, not verdicts. "Amount matches exactly" is a fact. "This is
the correct match" is a decision, and decisions belong to the reviewer (CR-01).

Sentences are generated from feature values, so the text always matches the numbers the
scorer used. Nothing here is written by a model; the optional generative prose is a
separate, labelled field (DD-10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from app.domain.rules import Item
from app.domain.scoring import Ranking, ScoredCandidate


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@dataclass
class Explanation:
    """What the reviewer reads, and what the report stores."""

    supporting: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """One block of prose for storage in recommendation.explanation."""
        parts = []
        if self.supporting:
            parts.append("Supporting evidence: " + "; ".join(self.supporting) + ".")
        if self.conflicting:
            parts.append("Conflicting evidence: " + "; ".join(self.conflicting) + ".")
        if self.alternatives:
            parts.append("Other candidates: " + "; ".join(self.alternatives) + ".")
        if self.caveats:
            parts.append(" ".join(self.caveats))
        return " ".join(parts)

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicting)


# ---------------------------------------------------------------------------
# Supporting evidence
# ---------------------------------------------------------------------------

def _supporting(subject: Item, scored: ScoredCandidate) -> list[str]:
    features = scored.features
    members = scored.members
    reasons: list[str] = []

    if features.amount_difference_cents == 0:
        if scored.is_group:
            reasons.append(f"{len(members)} entries sum exactly to {money(abs(subject.cash_effect))}")
        else:
            reasons.append(f"amount matches exactly at {money(abs(subject.cash_effect))}")
    elif features.amount_similarity > 0:
        reasons.append(f"amounts differ by {money(features.amount_difference_cents)}")

    gap = abs(features.business_day_gap)
    if gap == 0:
        reasons.append("same business date")
    elif gap == 1:
        reasons.append("posted 1 business day apart")
    else:
        reasons.append(f"posted {gap} business days apart")

    if features.reference_state == "equal":
        reference = members[0].reference or subject.reference
        reasons.append(f"reference {reference} appears on both records")
    elif features.reference_state == "partial":
        reasons.append("references are similar but not identical")

    if features.text_similarity >= 0.90:
        words = ", ".join(features.matched_words[:4]) if features.matched_words else ""
        reasons.append(f"counterparty text similarity {features.text_similarity:.2f}"
                       + (f" on {words}" if words else ""))
    elif features.text_similarity >= 0.70:
        reasons.append(f"counterparty text is a partial match at {features.text_similarity:.2f}")

    return reasons


# ---------------------------------------------------------------------------
# Conflict detectors (DOC-05 section 6.3)
# ---------------------------------------------------------------------------

def _close_runner_up(ranking: Ranking, ambiguity_margin: float) -> str | None:
    runner_up = ranking.runner_up
    if runner_up is None or ranking.best is None:
        return None
    if ranking.margin < ambiguity_margin:
        names = ", ".join(member.external_id for member in runner_up.members)
        return (f"the next candidate ({names}) scores {runner_up.score:.2f}, only "
                f"{ranking.margin:.2f} behind, so the two are hard to separate")
    return None


def _repeated_amount(ranking: Ranking) -> str | None:
    """Several candidates share the subject's exact amount."""
    exact = [scored for scored in ranking.scored
             if scored.features.amount_difference_cents == 0 and not scored.is_group]
    if len(exact) > 1:
        return (f"{len(exact)} records on the other side have this exact amount within the "
                f"date window, so amount alone does not identify the counterpart")
    return None


def _reference_mismatch(scored: ScoredCandidate, subject: Item) -> str | None:
    if scored.features.reference_state == "different":
        other = scored.members[0].reference
        return (f"references do not agree: {subject.reference} against {other}")
    if scored.features.reference_state == "missing":
        side = "bank item" if subject.reference is None else "ledger entry"
        return f"the {side} carries no reference, so that evidence is unavailable"
    return None


def _weak_text(scored: ScoredCandidate) -> str | None:
    if scored.features.text_similarity < 0.50:
        return (f"counterparty text similarity is only {scored.features.text_similarity:.2f}, "
                f"so the names do not support this pairing")
    return None


def _edge_of_window(scored: ScoredCandidate, window_business_days: int) -> str | None:
    if abs(scored.features.business_day_gap) >= window_business_days:
        return (f"the gap of {abs(scored.features.business_day_gap)} business days sits at the "
                f"edge of the {window_business_days} day window")
    return None


def _group_spread(scored: ScoredCandidate) -> str | None:
    if not scored.is_group:
        return None
    dates = {member.business_date for member in scored.members}
    if len(dates) > 1:
        span = (max(dates) - min(dates)).days
        if span >= 2:
            return (f"the {len(scored.members)} entries in this group span {span} days, which is "
                    f"wider than a single deposit usually covers")
    return None


def _near_miss_amount(scored: ScoredCandidate) -> str | None:
    if 0 < scored.features.amount_difference_cents:
        return (f"the amounts are not equal: a difference of "
                f"{money(scored.features.amount_difference_cents)} remains unexplained")
    return None


def _duplicate_involved(scored: ScoredCandidate, subject: Item) -> str | None:
    flagged = [item.external_id for item in (subject,) + tuple(scored.members)
               if item.is_possible_duplicate]
    if flagged:
        return (f"{', '.join(flagged)} was flagged as a possible duplicate during validation, "
                f"so confirm which copy is genuine before deciding")
    return None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def explain_match(ranking: Ranking, *, window_business_days: int,
                  ambiguity_margin: float, alternatives_shown: int = 3) -> Explanation:
    """Build the explanation for the leading candidate of one subject."""
    explanation = Explanation()
    best = ranking.best
    if best is None:
        return explanation

    subject = ranking.subject
    explanation.supporting = _supporting(subject, best)

    for detector in (
        lambda: _close_runner_up(ranking, ambiguity_margin),
        lambda: _repeated_amount(ranking),
        lambda: _reference_mismatch(best, subject),
        lambda: _weak_text(best),
        lambda: _edge_of_window(best, window_business_days),
        lambda: _group_spread(best),
        lambda: _near_miss_amount(best),
        lambda: _duplicate_involved(best, subject),
    ):
        finding = detector()
        if finding:
            explanation.conflicting.append(finding)

    for scored in ranking.scored[1:1 + alternatives_shown]:
        names = ", ".join(member.external_id for member in scored.members)
        explanation.alternatives.append(
            f"rank {scored.rank}, {names}, score {scored.score:.2f}"
            + (f", {len(scored.members)} entries" if scored.is_group else ""))

    if ranking.search_capped and ranking.cap_reason:
        explanation.caveats.append(
            f"The candidate search was limited: {ranking.cap_reason}. "
            f"A better answer may exist outside the search.")

    if len(ranking.scored) == 1:
        explanation.caveats.append("Only one candidate was found within the date window.")

    return explanation


def explain_no_candidate(subject: Item, considered: int, window_business_days: int,
                         capped: bool = False, cap_reason: str | None = None) -> Explanation:
    """Explanation for an item with nothing to pair with."""
    explanation = Explanation()
    explanation.supporting.append(
        f"{money(abs(subject.cash_effect))} dated {subject.business_date.isoformat()}"
        + (f" for {subject.payee}" if subject.payee else ""))
    explanation.conflicting.append(
        f"no record on the other side matches this amount within {window_business_days} "
        f"business days; {considered} records were considered")
    if capped and cap_reason:
        explanation.caveats.append(f"The search was limited: {cap_reason}.")
    return explanation


def summarize_for_prose(ranking: Ranking, explanation: Explanation) -> dict:
    """The whitelist of fields the optional generative adapter may receive (DD-10)."""
    subject = ranking.subject
    return {
        "item_summary": (subject.payee or subject.description or subject.external_id),
        "amount_cents": abs(subject.cash_effect),
        "item_date": subject.business_date.isoformat(),
        "supporting_evidence": list(explanation.supporting),
        "conflicting_evidence": list(explanation.conflicting),
    }


# ---------------------------------------------------------------------------
# Reading a stored explanation back into its parts
# ---------------------------------------------------------------------------

STORED_SECTIONS = ("Supporting evidence", "Conflicting evidence", "Other candidates")


def split_stored(text: str | None) -> dict[str, list[str] | str]:
    """Separate a stored explanation into supporting, conflicting, alternatives and notes.

    The reviewer screen shows conflicting evidence in its own block, and the optional
    prose adapter receives the same parts, so both read one parser.
    """
    sections: dict[str, list[str]] = {name: [] for name in STORED_SECTIONS}
    remainder = text or ""
    for name in STORED_SECTIONS:
        match = re.search(re.escape(name) + r": (.*?)\.(?= [A-Z]|$)", remainder)
        if match:
            sections[name] = [part.strip() for part in match.group(1).split("; ") if part.strip()]
            remainder = (remainder[:match.start()] + remainder[match.end():]).strip()
    return {"supporting": sections["Supporting evidence"], "conflicting": sections["Conflicting evidence"],
            "alternatives": sections["Other candidates"], "notes": remainder.strip()}
