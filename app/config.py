"""Configuration loading and the per-run parameter snapshot.

Values come from config/default.toml, optionally overridden by config/local.toml.
When a run is created, the effective values are frozen into
reconciliation_run.parameter_snapshot. Reports and the evaluation read that snapshot,
never this module, so editing configuration cannot change what a closed period
reported (ADR-10, NFR-01).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("config/default.toml")
LOCAL_CONFIG = Path("config/local.toml")


class ConfigError(Exception):
    """Configuration is missing or invalid. Raised at startup, never mid-run."""


# ---------------------------------------------------------------------------
# Parameter groups
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Parameters:
    """PRM-01..PRM-10 from DOC-02 section 3.4."""

    senior_approval_amount_cents: int      # PRM-01
    timing_window_business_days: int       # PRM-02
    outstanding_check_stale_days: int      # PRM-03
    group_candidate_cap: int               # PRM-04
    group_member_cap: int                  # PRM-05
    confidence_high_band: float            # PRM-06
    confidence_low_band: float             # PRM-07
    ambiguity_margin: float                # PRM-08
    auto_accept_threshold: float           # PRM-09, hypothetical only (DD-01)
    deposit_in_transit_stale_days: int     # PRM-10

    def validate(self) -> None:
        if self.senior_approval_amount_cents <= 0:
            raise ConfigError("PRM-01 senior_approval_amount_cents must be positive")
        if self.timing_window_business_days < 0:
            raise ConfigError("PRM-02 timing_window_business_days cannot be negative")
        if not 0 < self.confidence_low_band < self.confidence_high_band <= 1:
            raise ConfigError("PRM-06 and PRM-07 must satisfy 0 < low < high <= 1")
        if not 0 <= self.ambiguity_margin <= 1:
            raise ConfigError("PRM-08 ambiguity_margin must be between 0 and 1")
        if not 0 < self.auto_accept_threshold <= 1:
            raise ConfigError("PRM-09 auto_accept_threshold must be between 0 and 1")
        if self.group_member_cap < 2:
            raise ConfigError("PRM-05 group_member_cap must be at least 2")
        if self.group_candidate_cap < self.group_member_cap:
            raise ConfigError("PRM-04 group_candidate_cap cannot be below PRM-05")


@dataclass(frozen=True)
class Matching:
    exact_amount_tolerance_cents: int
    near_miss_tolerance_cents: int
    duplicate_window_business_days: int
    group_sum_tolerance_cents: int

    def validate(self) -> None:
        if self.exact_amount_tolerance_cents != 0:
            raise ConfigError("BR-01 requires exact_amount_tolerance_cents to be 0")
        if self.group_sum_tolerance_cents != 0:
            raise ConfigError("BR-04 requires group_sum_tolerance_cents to be 0")
        if self.near_miss_tolerance_cents < 0:
            raise ConfigError("near_miss_tolerance_cents cannot be negative")


@dataclass(frozen=True)
class Scoring:
    weight_date_distance: float
    weight_amount_similarity: float
    weight_text_similarity: float
    weight_reference_similarity: float
    weight_relationship_type: float
    relationship_score_one_to_one: float
    relationship_score_group: float

    def validate(self) -> None:
        total = round(
            self.weight_date_distance
            + self.weight_amount_similarity
            + self.weight_text_similarity
            + self.weight_reference_similarity
            + self.weight_relationship_type,
            6,
        )
        if total != 1.0:
            raise ConfigError(f"scoring weights must sum to 1.0, found {total}")
        for name, value in asdict(self).items():
            if not 0 <= value <= 1:
                raise ConfigError(f"{name} must be between 0 and 1")


@dataclass(frozen=True)
class GenAI:
    """Optional prose adapter. Off by default; never produces a score (DD-10, CR-13)."""

    enabled: bool
    provider: str
    model: str
    data_classification: str
    max_prose_words: int

    def validate(self) -> None:
        if self.provider not in ("stub", "gemini"):
            raise ConfigError("genai.provider must be 'stub' or 'gemini'")
        if self.enabled and self.data_classification != "synthetic":
            raise ConfigError(
                "genai.enabled requires data_classification = 'synthetic'; "
                "the adapter refuses to send anything else (NFR-05)"
            )

    @property
    def may_call_external_provider(self) -> bool:
        return self.enabled and self.provider != "stub" and self.data_classification == "synthetic"


@dataclass(frozen=True)
class Organization:
    name: str
    bank_name: str
    account_label: str
    account_mask: str
    gl_account_code: str
    currency: str


@dataclass(frozen=True)
class Paths:
    database: Path
    schema: Path
    evaluation_dataset: Path
    report_output: Path


@dataclass(frozen=True)
class Model:
    version_label: str
    calibration_dataset: str
    calibration_seed: int


@dataclass(frozen=True)
class Config:
    organization: Organization
    parameters: Parameters
    matching: Matching
    scoring: Scoring
    model: Model
    paths: Paths
    genai: GenAI

    def validate(self) -> None:
        self.parameters.validate()
        self.matching.validate()
        self.scoring.validate()
        self.genai.validate()

    def snapshot(self) -> str:
        """Canonical JSON of everything that can change a result.

        Stored on the run at creation. Organization details and file paths are excluded:
        they describe where the work happened, not how an item was judged.
        """
        payload = {
            "parameters": asdict(self.parameters),
            "matching": asdict(self.matching),
            "scoring": asdict(self.scoring),
            "model": asdict(self.model),
            "genai": {"enabled": self.genai.enabled, "provider": self.genai.provider},
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_snapshot(snapshot: str) -> dict[str, Any]:
        """Read back a stored snapshot. Used by reports and the evaluation."""
        return json.loads(snapshot)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _merge(base: dict, override: dict) -> dict:
    """Two-level merge: [section] tables are merged key by key."""
    merged = {section: dict(values) for section, values in base.items()}
    for section, values in override.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update(values)
        else:
            merged[section] = values
    return merged


def _section(data: dict, name: str, cls, converters: dict | None = None):
    """Build a dataclass from a TOML section, naming any missing or unexpected key."""
    if name not in data:
        raise ConfigError(f"configuration section [{name}] is missing")
    values = dict(data[name])
    for key, convert in (converters or {}).items():
        if key in values:
            values[key] = convert(values[key])

    expected = set(cls.__dataclass_fields__)
    missing = expected - set(values)
    unexpected = set(values) - expected
    if missing:
        raise ConfigError(f"[{name}] is missing: {', '.join(sorted(missing))}")
    if unexpected:
        raise ConfigError(f"[{name}] has unknown keys: {', '.join(sorted(unexpected))}")
    return cls(**values)


def load_config(default_path: Path = DEFAULT_CONFIG, local_path: Path = LOCAL_CONFIG) -> Config:
    """Load, merge and validate configuration. Raises ConfigError with a clear reason."""
    if not default_path.exists():
        raise ConfigError(f"{default_path} not found; run from the repository root")

    with default_path.open("rb") as fh:
        data = tomllib.load(fh)
    if local_path.exists():
        with local_path.open("rb") as fh:
            data = _merge(data, tomllib.load(fh))

    config = Config(
        organization=_section(data, "organization", Organization),
        parameters=_section(data, "parameters", Parameters),
        matching=_section(data, "matching", Matching),
        scoring=_section(data, "scoring", Scoring),
        model=_section(data, "model", Model),
        paths=_section(
            data, "paths", Paths,
            converters={"database": Path, "schema": Path,
                        "evaluation_dataset": Path, "report_output": Path},
        ),
        genai=_section(data, "genai", GenAI),
    )
    config.validate()
    return config
