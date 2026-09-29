"""Provider-neutral adapter for optional explanation prose.

What this may do: rewrite the evidence the system already computed as a short
paragraph a reviewer can read quickly.

What it may never do: produce or change a score, confidence, risk level, category or
decision (CR-13). That is enforced twice. The request carries no decision to make, and
the response is checked for decision language before it is accepted; anything that
looks like advice is discarded and the deterministic stub is used instead.

Other guarantees:
  * off by default (DD-10)
  * refuses to call an external provider unless the configuration asserts the data is
    synthetic (NFR-05)
  * sends a whitelist of derived fields, never a file, a row or an account number
  * never raises: any failure falls back to offline prose, so a reconciliation cannot
    be blocked by a provider
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Language that would turn a description into a recommendation. Its presence means the
# model tried to decide something rather than restate the evidence, so the output is
# rejected (CR-13).
#
# Deliberately absent: "risk level" and similar phrases that the prompt itself supplies.
# Repeating a fact the system provided is description, not judgement, and banning it
# would reject faithful summaries. Only language that asserts a decision, a score or a
# probability is treated as an offence.
DECISION_WORDS = (
    "approve", "approved", "approving", "reject", "rejected", "rejecting",
    "confidence", "score", "probability", "certainty", "likelihood", "odds",
    "recommend", "suggest", "advise", "you should", "safe to", "no need to",
    "can be matched", "should be matched", "correct match", "valid match",
)

# Asserting that items match is a decision, however it is phrased. Written as a pattern
# because "is a match", "are a match" and "they match" all say the same thing, while
# "the amount matches exactly" is a statement about the evidence and stays allowed.
MATCH_ASSERTION = re.compile(r"\b(?:is|are|were|would be)\s+(?:a\s+|the\s+)?match(?:es)?\b"
                             r"|\bthey\s+match\b|\bthese\s+match\b")

# A percentage or a bare probability. Numbers already present in the evidence are
# allowed: echoing "similarity 0.94" is description, inventing "92% chance" is not.
NUMERIC_CLAIM = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%|\b0\.\d+\b")


class GenAIError(Exception):
    """Raised only by provider implementations; callers never see it."""


@dataclass(frozen=True)
class ProseRequest:
    """The whitelist of fields an adapter may send.

    Everything here is derived data the system produced. No file name, no account
    number, no raw statement row and no identity ever leaves the machine.
    """

    item_summary: str            # normalized description, already cleaned
    amount_cents: int
    item_date: str
    category_code: str
    risk_level: str
    exception_code: str | None
    supporting_evidence: list[str]
    conflicting_evidence: list[str]

    def as_prompt(self) -> str:
        parts = [
            "Rewrite the following reconciliation evidence as one short paragraph for an "
            "accountant. Describe only what the evidence says.",
            "Do not give an opinion, a recommendation, a score, a probability or a decision.",
            "Do not suggest approving, rejecting or matching anything.",
            "",
            f"Item: {self.item_summary}",
            f"Amount: {self.amount_cents / 100:.2f}",
            f"Date: {self.item_date}",
            f"Queue: {self.category_code}",
            f"Risk level assigned by rule: {self.risk_level}",
        ]
        if self.exception_code:
            parts.append(f"Exception category: {self.exception_code}")
        if self.supporting_evidence:
            parts.append("Supporting evidence: " + "; ".join(self.supporting_evidence))
        if self.conflicting_evidence:
            parts.append("Conflicting evidence: " + "; ".join(self.conflicting_evidence))
        return "\n".join(parts)

    def fingerprint(self) -> str:
        """Hash of the prompt, recorded in the audit event (FR-GAI-04)."""
        return hashlib.sha256(self.as_prompt().encode("utf-8")).hexdigest()


@dataclass
class ProseResult:
    """What the caller stores and audits."""

    text: str
    provider: str
    model: str
    generated: bool                       # False when the offline stub produced it
    rejected_reason: str | None = None
    prompt_sha256: str = ""
    output_sha256: str = ""
    elapsed_ms: int = 0

    @property
    def label(self) -> str:
        """Shown beside the prose in the interface (FR-GAI-02)."""
        return ("AI-generated summary of the evidence above"
                if self.generated else "Offline summary of the evidence above")

    def audit_values(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "generated": self.generated,
                "prompt_sha256": self.prompt_sha256, "output_sha256": self.output_sha256,
                "rejected_reason": self.rejected_reason, "elapsed_ms": self.elapsed_ms}


def contains_decision_language(text: str, source_text: str = "") -> str | None:
    """Return the offending phrase, or None when the text is purely descriptive.

    `source_text` is the evidence the model was given. Numbers that appear there may be
    repeated; numbers that do not are treated as the model asserting its own figure.
    """
    lowered = text.lower()
    for word in DECISION_WORDS:
        if word in lowered:
            return word

    assertion = MATCH_ASSERTION.search(lowered)
    if assertion:
        return f"match assertion {assertion.group(0)!r}"

    source = source_text.lower()
    for match in NUMERIC_CLAIM.finditer(lowered):
        figure = match.group(0).strip()
        if figure not in source and figure.rstrip("%").strip() not in source:
            return f"numeric claim {figure!r} that is not in the evidence"
    return None


class GenAIAdapter:
    """Base class. Subclasses implement `_generate` and nothing else."""

    name = "base"

    def __init__(self, model: str = "", max_words: int = 80, timeout_seconds: int = 12):
        self.model = model
        self.max_words = max_words
        self.timeout_seconds = timeout_seconds

    def prose(self, request: ProseRequest) -> ProseResult:
        """Produce prose, falling back to the offline summary on any problem."""
        import time
        started = time.perf_counter()
        fallback = offline_prose(request)
        try:
            text = self._generate(request)
        except Exception as error:                      # deliberately broad: never propagate
            return ProseResult(fallback, self.name, self.model, False,
                               f"provider error: {type(error).__name__}: {error}",
                               request.fingerprint(),
                               hashlib.sha256(fallback.encode()).hexdigest(),
                               int((time.perf_counter() - started) * 1000))

        text = " ".join(text.split())
        offence = contains_decision_language(text, request.as_prompt())
        if offence:
            return ProseResult(fallback, self.name, self.model, False,
                               f"output rejected: contains {offence} (CR-13)",
                               request.fingerprint(),
                               hashlib.sha256(fallback.encode()).hexdigest(),
                               int((time.perf_counter() - started) * 1000))

        words = text.split()
        if len(words) > self.max_words:
            text = " ".join(words[:self.max_words]).rstrip(",;:") + "…"

        return ProseResult(text, self.name, self.model, True, None, request.fingerprint(),
                           hashlib.sha256(text.encode()).hexdigest(),
                           int((time.perf_counter() - started) * 1000))

    def _generate(self, request: ProseRequest) -> str:
        raise NotImplementedError


def offline_prose(request: ProseRequest) -> str:
    """Deterministic summary built from the evidence, used as the default and the fallback.

    Same input, same sentence, which keeps a run reproducible whether or not a provider
    is configured (NFR-01).
    """
    amount = f"${request.amount_cents / 100:,.2f}"
    sentences = [f"{request.item_summary} for {amount} dated {request.item_date}."]
    if request.supporting_evidence:
        sentences.append("Supporting evidence: " + "; ".join(request.supporting_evidence) + ".")
    if request.conflicting_evidence:
        sentences.append("Conflicting evidence: " + "; ".join(request.conflicting_evidence) + ".")
    if request.exception_code:
        sentences.append(f"Classified as {request.exception_code}.")
    sentences.append(f"Rule-based risk level: {request.risk_level}.")
    return " ".join(sentences)


class StubAdapter(GenAIAdapter):
    """The default provider: offline, deterministic, no network."""

    name = "stub"

    def _generate(self, request: ProseRequest) -> str:
        return offline_prose(request)


def load_env_file(path: Path = Path(".env")) -> dict[str, str]:
    """Read KEY=value lines from .env without adding a dependency.

    Values already in the environment win, so a shell export overrides the file.
    """
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def build_adapter(config) -> GenAIAdapter:
    """Choose an adapter from configuration. Anything unavailable falls back to the stub.

    Three conditions must all hold before an external provider is used: the feature is
    enabled, the provider is not the stub, and the configuration asserts the data is
    synthetic (DD-10, NFR-05).
    """
    settings = config.genai
    if not settings.may_call_external_provider:
        return StubAdapter(max_words=settings.max_prose_words)

    if settings.provider == "gemini":
        from app.infra.genai.gemini import GeminiAdapter
        load_env_file()
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return StubAdapter(max_words=settings.max_prose_words)
        return GeminiAdapter(api_key=api_key,
                             model=settings.model or "gemini-2.0-flash",
                             max_words=settings.max_prose_words)

    return StubAdapter(max_words=settings.max_prose_words)
