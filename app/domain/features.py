"""The five scoring features (DOC-05 section 6.1, FR-AI-04).

Each feature answers one question about a proposed pairing and returns 0.0 to 1.0.
They are computed separately, stored separately and shown separately, so a reviewer can
see which evidence carried a recommendation rather than a single opaque number.

Pure functions over `Item` values: no database, no configuration file, no clock. The
same pairing always produces the same features (NFR-01).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Sequence

from app.domain.normalize import business_days_between
from app.domain.rules import Item
from app.domain.text import best_similarity, shared_words


@dataclass(frozen=True)
class Features:
    """One pairing, five measurements, plus the facts the explanation needs."""

    date_distance: float
    amount_similarity: float
    text_similarity: float
    reference_similarity: float
    relationship_type: float

    business_day_gap: int = 0
    amount_difference_cents: int = 0
    matched_words: tuple[str, ...] = ()
    reference_state: str = "none"          # equal, partial, different, missing, none

    def as_json(self) -> str:
        """Stored on the candidate row and shown in the AI Recommendation Report."""
        payload = {key: value for key, value in asdict(self).items()}
        payload["matched_words"] = list(self.matched_words)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def as_dict(self) -> dict:
        return {"date_distance": self.date_distance,
                "amount_similarity": self.amount_similarity,
                "text_similarity": self.text_similarity,
                "reference_similarity": self.reference_similarity,
                "relationship_type": self.relationship_type}


# ---------------------------------------------------------------------------
# Individual features
# ---------------------------------------------------------------------------

def date_distance(gap_business_days: int, window_business_days: int) -> float:
    """1.0 on the same day, decaying with the business-day gap.

    Exponential decay rather than a straight line: the difference between same-day and
    one day apart matters more than between four days and five. At the configured
    window the score is about 0.37, so a pairing at the edge is still possible but
    clearly weaker than one inside it.
    """
    if window_business_days <= 0:
        return 1.0 if gap_business_days == 0 else 0.0
    return round(math.exp(-abs(gap_business_days) / window_business_days), 4)


def amount_similarity(left_cents: int, right_cents: int) -> float:
    """1.0 when the amounts are equal to the cent, falling away sharply.

    Money is the strongest signal in reconciliation, so near misses are punished hard:
    a dollar out on a five thousand dollar payment is usually a different transaction,
    not a rounding difference.
    """
    if left_cents == right_cents:
        return 1.0
    larger = max(abs(left_cents), abs(right_cents))
    if larger == 0:
        return 0.0
    relative_difference = abs(left_cents - right_cents) / larger
    score = round(max(0.0, 1.0 - relative_difference * 20), 4)
    # Never report 1.0 for amounts that are not equal. A one-cent difference on a large
    # amount rounds to 1.0 otherwise, and a group that sums one cent short would look
    # perfect when BR-04 requires an exact sum.
    return min(score, 0.9999) if score >= 1.0 else score


def text_similarity(left: str | None, right: str | None) -> float:
    """Counterparty similarity, tolerant of abbreviation and word order."""
    if not left or not right:
        return 0.0
    return best_similarity(left, right)


def reference_similarity(left: str | None, right: str | None) -> tuple[float, str]:
    """Reference agreement, and the state the explanation reports.

    A shared reference is close to proof, so an exact match scores 1.0. A missing
    reference scores 0.0 but is not evidence against the pairing; two different
    references are, which is why the two cases are reported differently.
    """
    if not left or not right:
        return 0.0, "missing" if (left or right) else "none"
    if left == right:
        return 1.0, "equal"
    if left in right or right in left:
        return 0.6, "partial"
    if len(left) == len(right):
        differences = sum(1 for a, b in zip(left, right) if a != b)
        if differences == 1:
            return 0.4, "partial"          # one digit apart: a transposition or typo
    return 0.0, "different"


def relationship_type(is_group: bool, one_to_one_score: float, group_score: float) -> float:
    """A flat preference for the simpler explanation.

    One bank item matching one ledger entry is more likely than a group that happens to
    sum correctly, so groups start slightly behind and must win on the other features.
    """
    return group_score if is_group else one_to_one_score


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def extract(subject: Item, counterparts: Sequence[Item], *, window_business_days: int,
            one_to_one_score: float = 1.0, group_score: float = 0.8) -> Features:
    """Compute every feature for one proposed pairing.

    `counterparts` is a list because a candidate may be a group. For a group the date
    used is the member furthest from the subject, and the amount compared is the sum,
    so a group is judged on its weakest member rather than its best.
    """
    is_group = len(counterparts) > 1
    total_cents = sum(item.amount_cents for item in counterparts)

    gaps = [business_days_between(subject.business_date, item.business_date)
            for item in counterparts]
    gap = max(gaps, key=abs) if gaps else 0

    subject_text = subject.payee or subject.description
    member_texts = [(item.payee or item.description or "") for item in counterparts]
    if is_group:
        # Averaged across members, not measured against the members joined together.
        # Joining lets one matching member carry the whole group: the subject's words all
        # appear in the combined text, which scores 1.0 however unrelated the others are.
        scores = [text_similarity(subject_text, text) for text in member_texts]
        text = round(sum(scores) / len(scores), 4) if scores else 0.0
    else:
        text = text_similarity(subject_text, member_texts[0] if member_texts else "")
    words = tuple(shared_words(subject_text or "", " ".join(member_texts)))

    if is_group:
        reference, reference_state = 0.0, "none"
        for item in counterparts:                    # any member agreeing is evidence
            score, state = reference_similarity(subject.reference, item.reference)
            if score > reference:
                reference, reference_state = score, state
    else:
        reference, reference_state = reference_similarity(subject.reference,
                                                          counterparts[0].reference)

    return Features(
        date_distance=date_distance(gap, window_business_days),
        amount_similarity=amount_similarity(abs(subject.cash_effect), total_cents),
        text_similarity=text,
        reference_similarity=reference,
        relationship_type=relationship_type(is_group, one_to_one_score, group_score),
        business_day_gap=gap,
        amount_difference_cents=abs(abs(subject.cash_effect) - total_cents),
        matched_words=words,
        reference_state=reference_state,
    )
