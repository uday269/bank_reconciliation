"""Risk rules RR-01 to RR-07 and category routing BR-13 (P5).

Risk is control policy, so it stays deterministic and inspectable: a rule, not a
model (DD-11, ADR-07). Every triggered rule is recorded with the recommendation and
shown to the reviewer, so "why is this high risk?" always has an answer.

Category routing decides which queue an item joins. High risk is tested first, so a
large amount or a suspected duplicate can never sit inside an exact-match batch,
which is what keeps batch approval safe (CR-11).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Sequence

LOW, MEDIUM, HIGH = "low", "medium", "high"
_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}

RISK_RULES = {
    "RR-01": ("Amount at or above the senior approval threshold", HIGH),
    "RR-02": ("Suspected duplicate", HIGH),
    "RR-03": ("Unexplained item", HIGH),
    "RR-04": ("Counterparty not seen earlier in the period", MEDIUM),
    "RR-05": ("Group match", MEDIUM),
    "RR-06": ("Stale outstanding item", MEDIUM),
    "RR-07": ("No risk rule triggered", LOW),
}


@dataclass(frozen=True)
class RiskAssessment:
    """The risk level and the rules that produced it."""

    level: str
    triggered: list[str] = field(default_factory=list)

    @property
    def requires_senior_approval(self) -> bool:
        """CR-02: high risk means a ROL-02 or ROL-03 decision."""
        return self.level == HIGH

    def reasons(self) -> list[str]:
        return [f"{code}: {RISK_RULES[code][0]}" for code in self.triggered]

    def as_json(self) -> str:
        """Stored in recommendation.risk_rules so the reviewer sees what fired."""
        return json.dumps(self.triggered, separators=(",", ":"))


def assess_risk(*, amount_cents: int, senior_approval_amount_cents: int,
                is_possible_duplicate: bool = False, exception_code: str | None = None,
                is_group: bool = False, counterparty: str | None = None,
                known_counterparties: Iterable[str] = (), is_stale: bool = False) -> RiskAssessment:
    """Apply every risk rule and return the highest level reached.

    Rules are cumulative: an item can trigger several, and all of them are recorded.
    The level is the highest triggered, never an average, because a control threshold
    cannot be diluted by unrelated low-risk signals.
    """
    triggered: list[str] = []

    if amount_cents >= senior_approval_amount_cents:
        triggered.append("RR-01")
    if is_possible_duplicate or exception_code == "EXC-05":
        triggered.append("RR-02")
    if exception_code == "EXC-07":
        triggered.append("RR-03")
    if counterparty and counterparty not in set(known_counterparties):
        triggered.append("RR-04")
    if is_group:
        triggered.append("RR-05")
    if is_stale:
        triggered.append("RR-06")

    if not triggered:
        return RiskAssessment(LOW, ["RR-07"])
    level = max((RISK_RULES[code][1] for code in triggered), key=lambda value: _ORDER[value])
    return RiskAssessment(level, triggered)


# ---------------------------------------------------------------------------
# BR-13 category routing
# ---------------------------------------------------------------------------

CATEGORIES = {
    "CAT-01": "Exact",
    "CAT-02": "High confidence",
    "CAT-03": "Ambiguous",
    "CAT-04": "Exception",
    "CAT-05": "High risk",
}


def assign_category(*, risk_level: str, kind: str, source: str,
                    confidence: float | None = None, runner_up_confidence: float | None = None,
                    high_confidence_band: float = 0.90,
                    ambiguity_margin: float = 0.15) -> tuple[str, str]:
    """BR-13, first match wins. Returns (category_code, why).

    Order: high risk, then exact, then exception, then confidence. Testing risk first
    is deliberate: a $12,000 exact match belongs in the senior queue, not in a batch.
    """
    if risk_level == HIGH:
        return "CAT-05", "risk level is high, so a senior decision is required (CR-02)"

    if kind == "match" and source == "rule":
        return "CAT-01", "settled by a deterministic rule, with no scoring involved"

    if kind == "exception":
        return "CAT-04", "no counterpart exists, so a disposition is needed rather than a match"

    if confidence is None:
        return "CAT-03", "no confidence value is available, so the item is treated as ambiguous"

    margin = confidence - (runner_up_confidence or 0.0)
    if confidence >= high_confidence_band and margin >= ambiguity_margin:
        return "CAT-02", (f"confidence {confidence:.2f} is at or above {high_confidence_band:.2f} "
                          f"and leads the next candidate by {margin:.2f}")
    if confidence < high_confidence_band:
        return "CAT-03", f"confidence {confidence:.2f} is below {high_confidence_band:.2f}"
    return "CAT-03", (f"confidence {confidence:.2f} is high, but the next candidate is only "
                      f"{margin:.2f} behind, which is under the {ambiguity_margin:.2f} margin")


def confidence_band(confidence: float | None, high_band: float, low_band: float) -> str:
    """BR-14: the band shown beside the numeric value."""
    if confidence is None:
        return "not scored"
    if confidence >= high_band:
        return "High"
    if confidence >= low_band:
        return "Medium"
    return "Low"
