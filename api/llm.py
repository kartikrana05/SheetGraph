"""
Thin Groq wrapper with robust JSON extraction.

Every LLM call in this app expects structured JSON back, so the parsing
concerns live here rather than being repeated in each caller.
"""

from __future__ import annotations

import json
import os
import re

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

_resolved_model: str | None = None

_client: Groq | None = None


# The default client retries rate limits with backoff and waits indefinitely,
# so a throttled request can hang for minutes. Behind a load balancer that
# surfaces to the browser as a dropped connection with no explanation, which
# is worse than a clear failure.
REQUEST_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
# Zero, not one: a retry doubles the worst case to ~90s, and the thing most
# likely to be retried is a rate limit, whose backoff is exactly what stalls
# the request. Better to fail at 45s with an explanation the user can act on.
MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "0"))


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


def complete(
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> str:
    try:
        response = client().chat.completions.create(
            model=resolve_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        # Translate the provider's failures into something a user can act on.
        # Left raw, a rate limit and an outage read the same.
        name = type(exc).__name__
        text = str(exc)
        if "rate" in text.lower() or "429" in text or name == "RateLimitError":
            raise RuntimeError(
                "The language model is rate limited right now. Wait a few seconds "
                "and try again, or upload fewer sheets at once — a larger prompt "
                "consumes more of the per-minute allowance."
            ) from exc
        if "model" in text.lower() and ("not found" in text.lower() or "decommission" in text.lower()):
            raise RuntimeError(
                f"The model '{resolve_model()}' is not available on this API key. "
                f"Set LLM_MODEL to one of: {', '.join(available_models()[:8])}"
            ) from exc
        if name in {"APITimeoutError", "APIConnectionError"} or "timeout" in text.lower():
            raise RuntimeError(
                f"The language model did not respond within {REQUEST_TIMEOUT:.0f}s. "
                "This usually means the prompt was large — try fewer sheets."
            ) from exc
        raise RuntimeError(f"Language model call failed ({name}): {text[:300]}") from exc

    choice = response.choices[0]
    text = (choice.message.content or "").strip()

    # A response cut off at the token ceiling is invalid JSON in a way that is
    # indistinguishable from the model simply misbehaving. Say which it was.
    if getattr(choice, "finish_reason", None) == "length":  # noqa: E501
        raise RuntimeError(
            f"The model hit the {max_tokens}-token output limit before finishing. "
            "The schema was too large to express in one response — try fewer "
            "sheets, or fewer columns per sheet."
        )

    return text


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
