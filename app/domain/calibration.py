"""Turning a match score into a calibrated confidence (FR-AI-05, DD-11, ADR-06).

A score orders candidates. It is not a probability: a score of 0.80 does not mean the
pairing is right eight times in ten. Calibration learns that relationship from labelled
data, so a confidence of 0.80 shown to a reviewer means what it appears to mean.

Three constraints shape this module.

Fitted on separate data. The July dataset is used to fit; August is used to evaluate.
Fitting and evaluating on the same data makes a model look better than it is (ADR-06).

Machine learning is limited to this one step. Risk stays rule-based and inspectable, and
no model decides anything (DD-11, ADR-07).

The result is a stored artefact, not a live model. Fitting happens once, offline, and
writes a JSON file with a version label. A run loads that file and applies it; nothing
retrains from reviewer decisions (CR-14).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

ARTEFACT_VERSION = 1

# A finite sample cannot establish certainty. Isotonic regression will happily fit 1.0 to
# a bin where every example was correct, and an interface showing "100% confident" invites
# exactly the over-trust the control design exists to prevent (RSK-01). Probabilities are
# therefore clamped: confidence can approach certainty but never claim it.
MINIMUM_PROBABILITY = 0.02
MAXIMUM_PROBABILITY = 0.98


@dataclass(frozen=True)
class CalibrationPoint:
    """One labelled example: a score and whether that candidate was in fact correct."""

    score: float
    correct: bool


@dataclass
class Calibrator:
    """A monotonic mapping from score to probability, stored as breakpoints.

    Isotonic regression is used when scikit-learn is available: it fits a step function
    that never decreases, which is the right shape here because a higher score should
    never mean a lower probability. The fitted steps are saved as plain numbers, so the
    stored artefact is readable and applying it needs no library at all.
    """

    version_label: str
    thresholds: list[float] = field(default_factory=list)      # ascending score breakpoints
    probabilities: list[float] = field(default_factory=list)   # probability at each breakpoint
    method: str = "isotonic"
    fitted_at: str = ""
    dataset: str = ""
    sample_size: int = 0
    positive_rate: float = 0.0
    notes: str = ""

    # -- applying -----------------------------------------------------------
    def confidence(self, score: float) -> float:
        """Calibrated probability for one score, by linear interpolation between steps."""
        if not self.thresholds:
            return round(min(max(score, 0.0), 1.0), 4)       # identity until fitted

        if score <= self.thresholds[0]:
            return round(self.probabilities[0], 4)
        if score >= self.thresholds[-1]:
            return round(self.probabilities[-1], 4)

        for index in range(1, len(self.thresholds)):
            upper = self.thresholds[index]
            if score <= upper:
                lower = self.thresholds[index - 1]
                low_probability = self.probabilities[index - 1]
                high_probability = self.probabilities[index]
                if upper == lower:
                    return round(high_probability, 4)
                position = (score - lower) / (upper - lower)
                return round(low_probability + position * (high_probability - low_probability), 4)
        return round(self.probabilities[-1], 4)

    def band(self, confidence: float, high_band: float, low_band: float) -> str:
        if confidence >= high_band:
            return "High"
        if confidence >= low_band:
            return "Medium"
        return "Low"

    @property
    def is_fitted(self) -> bool:
        return bool(self.thresholds)

    # -- storage ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"artefact_version": ARTEFACT_VERSION, "version_label": self.version_label,
                "method": self.method, "fitted_at": self.fitted_at, "dataset": self.dataset,
                "sample_size": self.sample_size, "positive_rate": self.positive_rate,
                "thresholds": self.thresholds, "probabilities": self.probabilities,
                "notes": self.notes}

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path

    @staticmethod
    def load(path: Path) -> "Calibrator":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("artefact_version") != ARTEFACT_VERSION:
            raise ValueError(f"calibration artefact version {data.get('artefact_version')} "
                             f"is not supported (expected {ARTEFACT_VERSION})")
        return Calibrator(
            version_label=data["version_label"], thresholds=data["thresholds"],
            probabilities=data["probabilities"], method=data["method"],
            fitted_at=data["fitted_at"], dataset=data["dataset"],
            sample_size=data["sample_size"], positive_rate=data["positive_rate"],
            notes=data.get("notes", ""))

    @staticmethod
    def identity(version_label: str = "uncalibrated") -> "Calibrator":
        """Score used directly as confidence.

        The fallback when no artefact exists. Honest rather than convenient: an
        uncalibrated run reports its version label as uncalibrated, so no report can
        claim a calibrated figure that was never fitted.
        """
        return Calibrator(version_label=version_label, method="identity",
                          notes="No calibration artefact was loaded; the raw score is reported.")


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def fit(points: Sequence[CalibrationPoint], *, version_label: str, dataset: str,
        minimum_points: int = 200) -> Calibrator:
    """Fit a calibrator on labelled scores.

    Isotonic regression where scikit-learn is installed, and a binned frequency estimate
    otherwise. Both produce the same artefact shape, so a run behaves identically either
    way; only the smoothness of the curve differs.
    """
    if len(points) < minimum_points:
        raise ValueError(f"{len(points)} labelled points is too few to calibrate; "
                         f"at least {minimum_points} are needed")

    scores = [point.score for point in points]
    labels = [1.0 if point.correct else 0.0 for point in points]
    positive_rate = round(sum(labels) / len(labels), 4)
    fitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        from sklearn.isotonic import IsotonicRegression

        model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        model.fit(scores, labels)
        thresholds = [round(float(value), 6) for value in model.X_thresholds_]
        probabilities = [_clamp(float(value)) for value in model.y_thresholds_]
        method = "isotonic"
        notes = (f"Isotonic regression, monotonic by construction. Probabilities are clamped "
                 f"to [{MINIMUM_PROBABILITY}, {MAXIMUM_PROBABILITY}]: a finite sample cannot "
                 f"establish certainty.")
    except ImportError:
        thresholds, probabilities = _binned_fit(scores, labels)
        method = "binned"
        notes = ("scikit-learn was not available, so a binned frequency estimate was used. "
                 "The mapping is still monotonic but coarser.")

    return Calibrator(version_label=version_label, thresholds=thresholds,
                      probabilities=probabilities, method=method, fitted_at=fitted_at,
                      dataset=dataset, sample_size=len(points), positive_rate=positive_rate,
                      notes=notes)


def _clamp(probability: float) -> float:
    return round(min(max(probability, MINIMUM_PROBABILITY), MAXIMUM_PROBABILITY), 6)


def _binned_fit(scores: Sequence[float], labels: Sequence[float], bins: int = 20
                ) -> tuple[list[float], list[float]]:
    """Fallback: observed success rate per score bin, forced to be non-decreasing."""
    buckets: dict[int, list[float]] = {}
    for score, label in zip(scores, labels):
        index = min(int(score * bins), bins - 1)
        buckets.setdefault(index, []).append(label)

    thresholds: list[float] = []
    probabilities: list[float] = []
    running = 0.0
    for index in sorted(buckets):
        observed = sum(buckets[index]) / len(buckets[index])
        running = max(running, observed)                 # enforce monotonicity
        thresholds.append(round((index + 0.5) / bins, 6))
        probabilities.append(_clamp(running))
    return thresholds, probabilities


# ---------------------------------------------------------------------------
# Quality measures, reported when fitting
# ---------------------------------------------------------------------------

@dataclass
class CalibrationQuality:
    """How closely stated confidence matches observed correctness."""

    expected_calibration_error: float
    maximum_error: float
    brier_score: float
    bands: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (f"expected calibration error {self.expected_calibration_error:.4f}, "
                f"worst band {self.maximum_error:.4f}, Brier score {self.brier_score:.4f}")


def assess(calibrator: Calibrator, points: Sequence[CalibrationPoint],
           bins: int = 10) -> CalibrationQuality:
    """Compare stated confidence with observed frequency, band by band.

    This is the number that matters for a reviewer: when the interface says 0.90, is the
    candidate right about nine times in ten?
    """
    buckets: dict[int, list[tuple[float, float]]] = {}
    squared_error = 0.0
    for point in points:
        confidence = calibrator.confidence(point.score)
        label = 1.0 if point.correct else 0.0
        squared_error += (confidence - label) ** 2
        buckets.setdefault(min(int(confidence * bins), bins - 1), []).append((confidence, label))

    total = len(points)
    weighted_error = 0.0
    worst = 0.0
    bands = []
    for index in sorted(buckets):
        entries = buckets[index]
        stated = sum(confidence for confidence, _ in entries) / len(entries)
        observed = sum(label for _, label in entries) / len(entries)
        error = abs(stated - observed)
        worst = max(worst, error)
        weighted_error += error * len(entries) / total
        bands.append({"band": f"{index / bins:.1f}-{(index + 1) / bins:.1f}",
                      "n": len(entries), "stated": round(stated, 4),
                      "observed": round(observed, 4), "error": round(error, 4)})

    return CalibrationQuality(
        expected_calibration_error=round(weighted_error, 4),
        maximum_error=round(worst, 4),
        brier_score=round(squared_error / total, 4),
        bands=bands)
