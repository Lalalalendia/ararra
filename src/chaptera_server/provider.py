"""Minimal native Gemini REST adapter; no browser session, Windows dependency or SDK globals."""
from __future__ import annotations

import email.utils
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from chaptera_translation_core.ports import ProviderRequest


class ProviderFailure(RuntimeError):
    pass


class TransientProviderFailure(ProviderFailure):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _retry_after(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw.strip()), 3600.0))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), 3600.0))
        except (TypeError, ValueError, OverflowError):
            return None


class GeminiProvider:
    def __init__(self, api_key: str, model: str, *, client: httpx.AsyncClient | None = None,
                 timeout_seconds: float = 180.0):
        if not api_key:
            raise ValueError("Gemini API key is required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
            raise ValueError("Invalid Gemini model ID")
        self.api_key = api_key
        self.model = model
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def generate(self, request: ProviderRequest) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, headers={"x-goog-api-key": self.api_key}, json=payload)
            else:
                response = await self.client.post(url, headers={"x-goog-api-key": self.api_key}, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TransientProviderFailure("Gemini network failure") from exc
        if response.status_code in (408, 429, 500, 502, 503, 504):
            raise TransientProviderFailure(
                f"Gemini temporarily unavailable (HTTP {response.status_code})",
                retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
            )
        if not (200 <= response.status_code < 300):
            # Do not leak response bodies, API keys, prompt text or paid chapters to logs.
            raise ProviderFailure(f"Gemini request failed (HTTP {response.status_code})")
        try:
            data: dict[str, Any] = response.json()
        except (ValueError, TypeError) as exc:
            raise TransientProviderFailure("Gemini returned malformed JSON") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderFailure("Gemini returned no candidates")
        first = candidates[0]
        reason = first.get("finishReason")
        if reason and reason not in {"STOP", "MAX_TOKENS"}:
            raise ProviderFailure(f"Gemini response stopped ({reason})")
        if reason == "MAX_TOKENS":
            raise TransientProviderFailure("Gemini response was truncated at token limit")
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(str(part["text"]) for part in parts if isinstance(part, dict) and part.get("text") and not part.get("thought"))
        if not text.strip():
            raise ProviderFailure("Gemini returned empty text")
        if request.on_usage is not None:
            counters = data.get('usageMetadata') or {}
            safe = {
                name: int(counters[name]) for name in
                ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount')
                if isinstance(counters.get(name), int) and not isinstance(counters[name], bool)
                and counters[name] >= 0
            }
            if safe:
                request.on_usage(safe)
        return text
