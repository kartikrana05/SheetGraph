"""
Thin Groq wrapper with robust JSON extraction.

Every LLM call in this app expects structured JSON back, so the parsing
concerns live here rather than being repeated in each caller.
"""

from __future__ import annotations

import json
import os
import re
import time

from groq import Groq

MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")

# Providers retire models without warning, and a retired name fails fast with a
# 404 that looks like any other error. These are tried in order if the
# configured model is not in the account's list.
FALLBACK_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "llama3-70b-8192",
    "llama3-8b-8192",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]

# Cypher and schema generation need the capable model. Summarising rows and
# writing starter questions do not — and using a smaller model for those halves
# consumption of a per-minute token allowance that is shared across both.
FAST_MODEL = os.getenv("LLM_FAST_MODEL", "llama-3.1-8b-instant")
FAST_FALLBACKS = ["llama-3.1-8b-instant", "llama3-8b-8192", "gemma2-9b-it"]

_resolved_model: str | None = None
_resolved_fast_model: str | None = None

_client: Groq | None = None


# The default client retries rate limits with backoff and waits indefinitely,
# so a throttled request can hang for minutes. Behind a load balancer that
# surfaces to the browser as a dropped connection with no explanation, which
# is worse than a clear failure.
REQUEST_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
# The SDK's own retry is disabled — its exponential backoff is unbounded and
# was stalling requests behind the load balancer. Rate limits are retried here
# instead, with short fixed waits and a hard ceiling, because Groq's free-tier
# allowance resets per minute and a few seconds usually clears it.
MAX_RETRIES = 0
RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "2"))
RATE_LIMIT_BACKOFF = [3, 7]  # seconds; total added wait is bounded at 10s


def client() -> Groq:
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set")
        _client = Groq(api_key=key, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES)
    return _client


def available_models() -> list[str]:
    """Model ids this API key can actually use, newest listing first."""
    try:
        listing = client().models.list()
    except Exception as exc:
        raise RuntimeError(f"Could not list models ({type(exc).__name__}): {exc}") from exc

    ids = []
    for item in getattr(listing, "data", []) or []:
        model_id = getattr(item, "id", None)
        if model_id:
            ids.append(model_id)
    return ids


def resolve_model() -> str:
    """
    The model to actually call.

    Checks the configured name against what the account can serve, because a
    decommissioned model produces a fast 404 that is indistinguishable from a
    dozen other failures. Resolved once and cached.
    """
    global _resolved_model
    if _resolved_model:
        return _resolved_model

    try:
        ids = available_models()
    except Exception:
        # If even the listing fails, use the configured name and let the call
        # itself produce the real error.
        _resolved_model = MODEL
        return _resolved_model

    if MODEL in ids:
        _resolved_model = MODEL
        return _resolved_model

    for candidate in FALLBACK_MODELS:
        if candidate in ids:
            print(f"[llm] configured model {MODEL!r} unavailable; using {candidate!r}")
            _resolved_model = candidate
            return _resolved_model

    # Any chat-capable model beats none. Whisper and guard models are not.
    for model_id in ids:
        low = model_id.lower()
        if not any(skip in low for skip in ("whisper", "guard", "tts", "embed")):
            print(f"[llm] falling back to first usable model {model_id!r}")
            _resolved_model = model_id
            return _resolved_model

    _resolved_model = MODEL
    return _resolved_model


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ == "RateLimitError" or "rate" in text or "429" in text


def resolve_fast_model() -> str:
    """A cheaper model for summarisation, falling back to the main one."""
    global _resolved_fast_model
    if _resolved_fast_model:
        return _resolved_fast_model
    try:
        ids = available_models()
    except Exception:
        _resolved_fast_model = resolve_model()
        return _resolved_fast_model

    for candidate in [FAST_MODEL, *FAST_FALLBACKS]:
        if candidate in ids:
            _resolved_fast_model = candidate
            return _resolved_fast_model

    _resolved_fast_model = resolve_model()
    return _resolved_fast_model


def complete(
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
    model: str | None = None,
) -> str:
    target = model or resolve_model()
    attempt = 0
    downgraded = False

    while True:
        try:
            response = client().chat.completions.create(
                model=target,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            break
        except Exception as exc:
            # A rate limit is transient by definition — the allowance resets on
            # a fixed window — so waiting briefly beats failing the request.
            if _is_rate_limit(exc) and attempt < RATE_LIMIT_RETRIES:
                wait = RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]
                print(f"[llm] rate limited, retrying in {wait}s "
                      f"(attempt {attempt + 1}/{RATE_LIMIT_RETRIES})")
                time.sleep(wait)
                attempt += 1
                continue

            # Allowances are per model. When the capable one is exhausted the
            # smaller one usually is not, and a slightly weaker answer beats no
            # answer — particularly mid-demo.
            fast = resolve_fast_model()
            if _is_rate_limit(exc) and not downgraded and fast != target:
                print(f"[llm] {target} still rate limited; falling back to {fast}")
                target = fast
                downgraded = True
                attempt = 0
                continue

            return _raise_readable(exc, target)

    choice = response.choices[0]
    text = (choice.message.content or "").strip()

    if getattr(choice, "finish_reason", None) == "length":
        raise RuntimeError(
            f"The model hit the {max_tokens}-token output limit before finishing. "
            "The schema was too large to express in one response — try fewer "
            "sheets, or fewer columns per sheet."
        )

    return text


def _raise_readable(exc: Exception, target: str) -> str:
    """Translate a provider failure into something a user can act on."""
    name = type(exc).__name__
    text = str(exc)

    if _is_rate_limit(exc):
        raise RuntimeError(
            "Both the main and fallback models are rate limited. Groq's free tier "
            "allows a fixed number of tokens per minute and the window has not "
            "reset yet — wait about a minute. If this keeps happening, set "
            "LLM_MODEL=llama-3.1-8b-instant, which has a much larger allowance."
        ) from exc

    if "model" in text.lower() and ("not found" in text.lower() or "decommission" in text.lower()):
        raise RuntimeError(
            f"The model '{target}' is not available on this API key. "
            f"Set LLM_MODEL to one of: {', '.join(available_models()[:8])}"
        ) from exc

    if name in {"APITimeoutError", "APIConnectionError"} or "timeout" in text.lower():
        raise RuntimeError(
            f"The language model did not respond within {REQUEST_TIMEOUT:.0f}s."
        ) from exc

    raise RuntimeError(f"Language model call failed ({name}): {text[:300]}") from exc


def _balanced_json(text: str) -> str | None:
    """
    Find the first complete, brace-balanced JSON object in a blob of text.

    A naive greedy regex breaks whenever the model appends prose after the
    JSON, so we walk the string instead and respect string literals.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        char = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None


def extract_json(text: str) -> dict:
    """Parse JSON out of an LLM response, tolerating fences and stray prose."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    candidate = _balanced_json(cleaned)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Model did not return valid JSON. Got: {text[:300]}")


def complete_json(
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict:
    """Call the model and insist on a JSON object, with one repair attempt."""
    raw = complete(system, user, temperature, max_tokens)
    try:
        return extract_json(raw)
    except ValueError:
        repair = complete(
            "You fix malformed JSON. Return ONLY the corrected JSON object, nothing else.",
            f"This was supposed to be a single JSON object but could not be parsed:\n\n{raw}",
            temperature=0.0,
            max_tokens=max_tokens,
        )
        return extract_json(repair)
