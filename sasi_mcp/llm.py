"""Pluggable LLM backend for canonicalization + classification.

Two providers:

* `ollama` (default) — POST to 127.0.0.1:11434 with `format: "json"`. Local
  inference, zero data egress. Works against `llama3.1:8b` or any model
  pulled into Ollama.
* `anthropic` — Anthropic Messages API. Opt-in via config; the input is
  already redacted so sending it to the API is acceptable but not free.

Both expose a single `complete_json(provider, system, user, **kwargs)` that
returns a parsed dict. We force JSON mode in both and re-raise on parse
failure rather than silently returning a malformed dict.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sasi_mcp.logger import get_logger

_log = get_logger("sasi_mcp.llm")


class LLMError(RuntimeError):
    pass


def complete_json(
    provider: str,
    system: str,
    user: str,
    *,
    model: str | None = None,
    ollama_url: str = "http://127.0.0.1:11434",
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Return a parsed JSON object from the LLM."""
    if provider == "ollama":
        return _complete_ollama(system=system, user=user, model=model or "llama3.1:8b",
                                base_url=ollama_url, timeout=timeout)
    if provider == "anthropic":
        return _complete_anthropic(
            system=system, user=user, model=model or "claude-haiku-4-5", timeout=timeout
        )
    raise LLMError(f"unknown provider: {provider!r}")


def _complete_ollama(*, system: str, user: str, model: str, base_url: str,
                      timeout: float) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise LLMError("httpx is required for the ollama provider") from exc

    payload = {
        "model": model,
        "system": system,
        "prompt": user,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }
    try:
        resp = httpx.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        raise LLMError(f"ollama call failed: {exc}") from exc
    text = (resp.json() or {}).get("response", "")
    return _parse_json_loose(text)


def _complete_anthropic(*, system: str, user: str, model: str, timeout: float) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY not set")
    try:
        import httpx
    except ImportError as exc:
        raise LLMError("httpx is required for the anthropic provider") from exc

    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": system + "\nYou MUST respond with a single JSON object and nothing else.",
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            json=payload, headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise LLMError(f"anthropic call failed: {exc}") from exc
    body = resp.json()
    parts = body.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return _parse_json_loose(text)


def _parse_json_loose(text: str) -> dict[str, Any]:
    """Parse JSON, allowing a leading/trailing fence or whitespace."""
    if not text:
        raise LLMError("empty LLM response")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        # Best-effort: locate the first brace and try again.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1], strict=False)
            except json.JSONDecodeError:
                pass
        raise LLMError(f"non-JSON LLM response: {text[:200]}") from exc
