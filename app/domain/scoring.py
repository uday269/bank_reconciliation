"""Scoring and ranking of candidates (DOC-05 section 6, FR-AI-04..05).

A score is a weighted sum of the five features. Nothing more elaborate is used, and
that is the point: a reviewer can be shown the five numbers and the five weights and
reproduce the total by hand. A model whose arithmetic cannot be checked has no place in
a control system, however much accuracy it might buy.

The score is not a probability. Turning it into one is calibration's job, fitted on a
separate dataset (DD-11). Until then the score only orders candidates.

Ranking is deterministic: ties are broken by fixed, explainable rules rather than by
whichever row the database happened to return first, so the same run always produces
the same first candidate (NFR-01).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.domain.candidates import Candidate, CandidateSet
from app.domain.features import Features, extract
from app.domain.rules import Item


@dataclass(frozen=True)
class Weights:
    """The five weights, which must sum to 1.0 so a score stays within 0..1."""

    date_distance: float = 0.20
    amount_similarity: float = 0.30
    text_similarity: float = 0.25
    reference_similarity: float = 0.20
    relationship_type: float = 0.05

    def validate(self) -> None:
        total = round(self.date_distance + self.amount_similarity + self.text_similarity
                      + self.reference_similarity + self.relationship_type, 6)
        if total != 1.0:
            raise ValueError(f"weights must sum to 1.0, found {total}")

    @staticmethod
    def from_config(scoring) -> "Weights":
        weights = Weights(
            date_distance=scoring.weight_date_distance,
            amount_similarity=scoring.weight_amount_similarity,
            text_similarity=scoring.weight_text_similarity,
            reference_similarity=scoring.weight_reference_similarity,
            relationship_type=scoring.weight_relationship_type)
        weights.validate()
        return weights

    def contributions(self, features: Features) -> dict[str, float]:
        """What each feature added to the total, for the explanation."""
        return {
            "date_distance": round(features.date_distance * self.date_distance, 4),
            "amount_similarity": round(features.amount_similarity * self.amount_similarity, 4),
            "text_similarity": round(features.text_similarity * self.text_similarity, 4),
            "reference_similarity": round(features.reference_similarity * self.reference_similarity, 4),
            "relationship_type": round(features.relationship_type * self.relationship_type, 4),
        }


@dataclass
class ScoredCandidate:
    """A candidate with its features, score and rank."""

    candidate: Candidate
    features: Features
    score: float
    rank: int = 0
    contributions: dict[str, float] = field(default_factory=dict)

    @property
    def members(self) -> tuple[Item, ...]:
        return self.candidate.members

    @property
    def is_group(self) -> bool:
        return self.candidate.is_group

    @property
    def relationship(self) -> str:
        return self.candidate.relationship

    def strongest_features(self, count: int = 3) -> list[tuple[str, float]]:
        """The features that carried this score, largest contribution first."""
        return sorted(self.contributions.items(), key=lambda pair: pair[1], reverse=True)[:count]


@dataclass
class Ranking:
    """The ordered answer for one subject item."""

    subject: Item
    scored: list[ScoredCandidate] = field(default_factory=list)
    search_capped: bool = False
    cap_reason: str | None = None

    @property
    def best(self) -> ScoredCandidate | None:
        return self.scored[0] if self.scored else None

    @property
    def runner_up(self) -> ScoredCandidate | None:
        return self.scored[1] if len(self.scored) > 1 else None

    @property
    def margin(self) -> float:
        """How far ahead the leader is. Small margins mean an ambiguous item (PRM-08)."""
        if not self.scored:
            return 0.0
        if len(self.scored) == 1:
            return round(self.scored[0].score, 4)
        return round(self.scored[0].score - self.scored[1].score, 4)

    def as_candidate_scores(self) -> list[dict]:
        """Every candidate and score, recorded in the audit event (FR-AI-07)."""
        return [{"rank": item.rank, "score": item.score,
                 "members": [member.external_id for member in item.members],
                 "relationship": item.relationship}
                for item in self.scored]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_features(features: Features, weights: Weights) -> float:
    """Weighted sum of the five features, 0.0 to 1.0."""
    return round(
        features.date_distance * weights.date_distance
        + features.amount_similarity * weights.amount_similarity
        + features.text_similarity * weights.text_similarity
        + features.reference_similarity * weights.reference_similarity
        + features.relationship_type * weights.relationship_type,
        4)


def _tie_break(scored: ScoredCandidate) -> tuple:
    """Deterministic order for candidates with the same score.

    Preference, in order: higher score, exact amount over near miss, single item over a
    group, smaller date gap, then the lowest identifier. The last is arbitrary but
    stable, which is what reproducibility requires.
    """
    return (
        -scored.score,
        0 if scored.features.amount_difference_cents == 0 else 1,
        len(scored.members),
        abs(scored.features.business_day_gap),
        tuple(sorted((member.item_type, member.item_id) for member in scored.members)),
    )


def rank_candidates(candidate_set: CandidateSet, weights: Weights, *,
                    window_business_days: int, one_to_one_score: float = 1.0,
                    group_score: float = 0.8, keep: int | None = None) -> Ranking:
    """Score and order every candidate for one subject.

    All candidates are kept and ranked, not only the winner: FR-REV-09 requires the
    reviewer to see the alternatives, and the margin between first and second is what
    decides whether an item is treated as high confidence or ambiguous (BR-13).
    """
    weights.validate()
    scored: list[ScoredCandidate] = []

    for candidate in candidate_set.candidates:
        features = extract(candidate_set.subject, list(candidate.members),
                           window_business_days=window_business_days,
                           one_to_one_score=one_to_one_score, group_score=group_score)
        total = score_features(features, weights)
        scored.append(ScoredCandidate(candidate=candidate, features=features, score=total,
                                      contributions=weights.contributions(features)))

    scored.sort(key=_tie_break)
    if keep is not None:
        scored = scored[:keep]
    for position, item in enumerate(scored, start=1):
        item.rank = position

    return Ranking(subject=candidate_set.subject, scored=scored,
                   search_capped=candidate_set.search_capped,
                   cap_reason=candidate_set.cap_reason)


def rank_all(candidate_sets: Sequence[CandidateSet], weights: Weights, *,
             window_business_days: int, one_to_one_score: float = 1.0,
             group_score: float = 0.8, keep: int | None = None) -> list[Ranking]:
    return [rank_candidates(candidate_set, weights, window_business_days=window_business_days,
                            one_to_one_score=one_to_one_score, group_score=group_score, keep=keep)
            for candidate_set in candidate_sets]
