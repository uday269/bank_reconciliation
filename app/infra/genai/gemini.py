"""Google Gemini provider, called over HTTPS with the standard library only.

Free-tier keys come from Google AI Studio (aistudio.google.com). The key is read from
the environment or a local .env file, never from configuration that is committed.

Which API this uses
    The classic `generateContent` endpoint. Google's Interactions API became the
    recommended interface in June 2026, but it exists for server-side conversation
    state, tool orchestration and agent workflows. This adapter sends one prompt and
    reads one paragraph back, with no history and no tools, so the simpler stateless
    endpoint is the right fit. `generateContent` remains fully supported.

Model names change
    Model identifiers are retired on a published schedule: gemini-2.0-flash was
    withdrawn on 1 June 2026, for example. The model is therefore configuration, not a
    constant, and `list_models` below reports what the key can actually reach.

This class does one thing: send a prompt, return text. Every guard lives in the base
adapter, so another provider added later inherits the same protections.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request

from app.infra.genai.base import GenAIAdapter, GenAIError, ProseRequest


class TransientProviderError(GenAIError):
    """A failure worth retrying: overload, rate limit or a server fault."""


# Codes the provider itself describes as temporary.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
RETRY_DELAY_SECONDS = 2.0

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GENERATE_ENDPOINT = BASE_URL + "/models/{model}:generateContent"
MODELS_ENDPOINT = BASE_URL + "/models"

# Current default. Model identifiers are retired and closed to new keys on a published
# schedule: gemini-2.0-flash was withdrawn on 1 June 2026, and gemini-2.5-flash is closed
# to keys created after it. Override in config/local.toml, and run
# `python -m app.infra.genai.gemini` to see what a given key can actually reach.
DEFAULT_MODEL = "gemini-3.8-flash"

# Preference order when the configured model is unavailable: a plain text flash model,
# newest first. Image, audio, transcription and agent identifiers are skipped because
# they do not answer a text prompt.
_TEXT_MODEL = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash$")


def _ssl_context() -> ssl.SSLContext:
    """TLS context with a usable certificate bundle.

    Some Python installations, Anaconda on macOS in particular, ship without a system
    certificate store, which makes every HTTPS call fail with CERTIFICATE_VERIFY_FAILED.
    The certifi bundle is used when it is importable, which is the case for Anaconda and
    for anything that has installed requests. Verification is never disabled.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class GeminiAdapter(GenAIAdapter):
    name = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, max_words: int = 80,
                 timeout_seconds: int = 12, transport=None):
        super().__init__(model=model or DEFAULT_MODEL, max_words=max_words,
                         timeout_seconds=timeout_seconds)
        if not api_key:
            raise GenAIError("no API key supplied")
        self._api_key = api_key
        self._transport = transport or self._http_post      # injected in tests

    # -- provider call ------------------------------------------------------
    def _generate(self, request: ProseRequest) -> str:
        """Send the prompt, retrying once when the provider reports a temporary fault.

        One retry, not a loop: a reviewer is waiting, and prose is optional. If the
        second attempt also fails the base adapter uses the offline summary.
        """
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request.as_prompt()}]}],
            "generationConfig": {
                "temperature": 0.2,                 # low, so prose stays close to the evidence
                # Gemini 3 models spend part of the output budget on internal reasoning
                # before the visible answer, so a budget sized to the wanted paragraph
                # returns a sentence cut in half. Ask for far more than the prose needs;
                # the base adapter trims the result to max_words anyway.
                "maxOutputTokens": max(self.max_words * 12, 1024),
                "candidateCount": 1,
            },
        }
        url = GENERATE_ENDPOINT.format(model=self.model)
        try:
            response = self._transport(url, payload)
        except TransientProviderError:
            time.sleep(RETRY_DELAY_SECONDS)
            response = self._transport(url, payload)
        return self._extract_text(response)

    def _http_post(self, url: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": self._api_key})       # key in a header, never in the URL
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds,
                                        context=_ssl_context()) as handle:
                return json.loads(handle.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            message = self._explain_http_error(error.code, detail)
            if error.code in RETRYABLE_STATUS:
                raise TransientProviderError(message) from error
            raise GenAIError(message) from error
        except urllib.error.URLError as error:
            raise GenAIError(self._explain_url_error(error)) from error

    @staticmethod
    def _explain_url_error(error: urllib.error.URLError) -> str:
        reason = str(error.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            return ("TLS certificate verification failed. This Python has no certificate "
                    "bundle. Install certifi (pip install certifi), or on a python.org "
                    "install run 'Install Certificates.command' in /Applications/Python 3.x/")
        return f"network error: {reason}"

    def _explain_http_error(self, code: int, detail: str) -> str:
        """Turn a status code into something a reader can act on."""
        hints = {
            400: "bad request; the model name may be wrong or retired",
            401: "unauthenticated; check GEMINI_API_KEY",
            403: "forbidden; the key may be restricted or the Generative Language API disabled",
            404: f"model {self.model!r} not found; it may have been retired",
            429: "rate limited; the free tier quota is exhausted for now",
            500: "provider error", 502: "provider gateway error",
            503: "the model is temporarily overloaded at the provider",
            504: "provider timeout",
        }
        return f"HTTP {code}: {hints.get(code, 'request failed')} — {detail}"

    @staticmethod
    def _extract_text(response: dict) -> str:
        candidates = response.get("candidates") or []
        if not candidates:
            blocked = response.get("promptFeedback", {}).get("blockReason")
            raise GenAIError(f"no candidates returned{f' ({blocked})' if blocked else ''}")

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()
        finish = candidate.get("finishReason", "")

        if not text:
            raise GenAIError(f"empty response{f' ({finish})' if finish else ''}")

        # The budget is far larger than a short paragraph needs, so hitting it means the
        # response did not finish. A half-sentence is worse than none: the reviewer would
        # read half a fact. Treat it as a failure and use the offline summary instead.
        if finish == "MAX_TOKENS":
            raise GenAIError("response did not finish within the token budget (MAX_TOKENS)")
        return text


def list_models(api_key: str, timeout_seconds: int = 12) -> list[str]:
    """Model names this key can use with generateContent.

    Useful when a model has been retired: run the module directly to see the list.
    """
    request = urllib.request.Request(MODELS_ENDPOINT, method="GET",
                                     headers={"x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds,
                                    context=_ssl_context()) as handle:
            payload = json.loads(handle.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise GenAIError(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as error:
        raise GenAIError(GeminiAdapter._explain_url_error(error))

    names = []
    for model in payload.get("models", []):
        if "generateContent" in (model.get("supportedGenerationMethods") or []):
            names.append(model["name"].removeprefix("models/"))
    return sorted(names)


def choose_model(available: list[str], preferred: str = DEFAULT_MODEL) -> str:
    """Pick a usable text model from what the key can reach.

    The preferred name wins when it is present. Otherwise the newest plain flash model
    is used, so a retired identifier degrades to a working one rather than to an image
    or agent endpoint that cannot answer a prompt.
    """
    if preferred in available:
        return preferred
    candidates = [(float(match.group(1)), name) for name in available
                  if (match := _TEXT_MODEL.match(name))]
    if candidates:
        return max(candidates)[1]
    return preferred


def _main() -> int:
    """Check the key, show which models it can reach, and send one test prompt.

        python -m app.infra.genai.gemini

    Uses the same configuration the application uses, so the model reported here is the
    model a run would actually call.
    """
    from app.config import ConfigError, load_config
    from app.infra.genai.base import ProseRequest, load_env_file

    load_env_file()

    configured_model = ""
    try:
        config = load_config()
        configured_model = (config.genai.model or "").strip()
        print(f"Configuration: genai enabled = {config.genai.enabled}, "
              f"provider = {config.genai.provider}, "
              f"model = {configured_model or '(not set, using the default)'}")
    except ConfigError as error:
        print(f"Configuration could not be read ({error}); using defaults.")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY is not set. Add it to .env in the repository root.")
        return 2

    print(f"Key found, {len(api_key)} characters. Checking available models…\n")
    preferred = configured_model or DEFAULT_MODEL
    try:
        models = list_models(api_key)
    except GenAIError as error:
        print(f"Could not list models: {error}")
        return 1

    for name in models:
        if name == preferred:
            marker = "  <- configured" if configured_model else "  <- default"
        else:
            marker = ""
        print(f"  {name}{marker}")
    if preferred not in models:
        print(f"\nNote: {preferred!r} is not available to this key. "
              f"Using {choose_model(models, preferred)!r} instead; set another in "
              f"config/local.toml under [genai] model = \"...\"")

    print("\nSending one test prompt…")
    request = ProseRequest(
        item_summary="ACH deposit from Canyon Ridge Grocery, reference 8842",
        amount_cents=482000, item_date="2026-08-14", category_code="CAT-03",
        risk_level="low", exception_code=None,
        supporting_evidence=["amount matches exactly", "payee text similarity 0.94"],
        conflicting_evidence=["a second ledger entry has the same amount and payee"])
    model = choose_model(models, preferred)
    result = GeminiAdapter(api_key, model=model).prose(request)
    print(f"  model:     {model}")
    print(f"  generated: {result.generated}")
    if result.rejected_reason:
        print(f"  fell back: {result.rejected_reason}")
    print(f"  text:      {result.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
