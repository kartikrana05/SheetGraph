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

_client: Groq | None = None


def client() -> Groq:
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set")
        _client = Groq(api_key=key)
    return _client


def complete(
    system: str,
    user: str,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> str:
    response = client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


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
