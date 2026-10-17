"""Minimal native Gemini REST adapter; no browser session, Windows dependency or SDK globals."""
from __future__ import annotations

import email.utils
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from chaptera_translation_core.ports import ProviderRequest
from chaptera_translation_core.api.errors import PartialGenerationError


class ProviderFailure(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None,
                 finish_reason: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.finish_reason = finish_reason


class TransientProviderFailure(ProviderFailure):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None,
                 status_code: int | None = None, is_network_error: bool = False):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        self.is_network_error = is_network_error


class DailyProviderQuotaFailure(TransientProviderFailure):
    """The server explicitly reported a per-day quota (not a transient RPM 429)."""


def _daily_quota_violation(response: httpx.Response) -> bool:
    """Use only structured quota IDs; never log provider error bodies.

    An unclassified 429 may be RPM, TPM or spend-related, and must keep the
    existing bounded exponential retry path rather than sleeping a whole day.
    """
    if len(response.content) > 65536:
        return False
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations") or []:
            if (isinstance(violation, dict) and
                    "perday" in str(violation.get("quotaId", "")).lower()):
                return True
    return False


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
                 timeout_seconds: float = 180.0, max_output_tokens: int | None = None,
                 stream_atoms: bool = False):
        if not api_key:
            raise ValueError("Gemini API key is required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
            raise ValueError("Invalid Gemini model ID")
        self.api_key = api_key
        self.model = model
        self.client = client
        if max_output_tokens is not None and max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.stream_atoms = bool(stream_atoms)
        self.completed_calls = 0

    async def generate(self, request: ProviderRequest) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        generation_config = {"temperature": 0.2}
        if self.max_output_tokens is not None:
            generation_config["maxOutputTokens"] = self.max_output_tokens
        payload = {
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": generation_config,
        }
        if self.stream_atoms and request.transport == "atoms_v1":
            return await self._generate_stream(url, payload, request)
        try:
            if self.client is None:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    response = await client.post(url, headers={"x-goog-api-key": self.api_key}, json=payload)
            else:
                response = await self.client.post(url, headers={"x-goog-api-key": self.api_key}, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise TransientProviderFailure("Gemini network failure", is_network_error=True) from exc
        if response.status_code == 429 and _daily_quota_violation(response):
            raise DailyProviderQuotaFailure("Gemini daily quota exhausted (HTTP 429)")
        if response.status_code in (408, 429, 500, 502, 503, 504):
            raise TransientProviderFailure(
                f"Gemini temporarily unavailable (HTTP {response.status_code})",
                retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
                status_code=response.status_code,
            )
        if not (200 <= response.status_code < 300):
            # Do not leak response bodies, API keys, prompt text or paid chapters to logs.
            raise ProviderFailure(
                f"Gemini request failed (HTTP {response.status_code})",
                status_code=response.status_code,
            )
        try:
            data: dict[str, Any] = response.json()
        except (ValueError, TypeError) as exc:
            raise TransientProviderFailure("Gemini returned malformed JSON") from exc
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderFailure("Gemini returned no candidates")
        first = candidates[0]
        reason = first.get("finishReason")
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(str(part["text"]) for part in parts if isinstance(part, dict) and part.get("text") and not part.get("thought"))
        if reason and reason not in {"STOP", "MAX_TOKENS"}:
            # A content/safety stop can contain candidate text but it must NOT
            # enter the source-proven partial recovery path.
            raise ProviderFailure(
                f"Gemini response stopped ({reason})" if reason in
                {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "SPII", "OTHER"}
                else "Gemini response stopped (unrecognized finish reason)",
                finish_reason=reason if reason in
                {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "SPII"} else None,
            )
        if reason == "MAX_TOKENS" and not text.strip():
            raise TransientProviderFailure("Gemini response truncated without answer text")
        if not text.strip():
            raise ProviderFailure("Gemini returned empty text")
        if request.on_usage is not None:
            counters = data.get('usageMetadata') or {}
            safe = {
                name: int(counters[name]) for name in
                ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount',
                 'thoughtsTokenCount', 'cachedContentTokenCount')
                if isinstance(counters.get(name), int) and not isinstance(counters[name], bool)
                and counters[name] >= 0
            }
            if safe:
                request.on_usage(safe)
        if reason == "MAX_TOKENS":
            # Preserve the ORIGINAL desktop PartialGenerationError contract:
            # callers validate source-owned coverage before storing any text.
            raise PartialGenerationError(
                "Gemini answer stopped at MAX_TOKENS", text, "MAX_TOKENS",
            )
        self.completed_calls += 1
        return text

    async def _generate_stream(self, url: str, payload: dict, request: ProviderRequest) -> str:
        """Read Gemini SSE frames like desktop, without persisting unproved text.

        Physical interruption retains only the accumulated answer in the
        desktop's PartialGenerationError. TranslationCore then performs the
        existing source-owned CoverageMap validation and targeted repair.
        """
        stream_url = url.replace(":generateContent", ":streamGenerateContent") + "?alt=sse"
        if self.client is None:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                return await self._read_stream(client, stream_url, payload, request)
        return await self._read_stream(self.client, stream_url, payload, request)

    async def _read_stream(self, client: httpx.AsyncClient, url: str,
                           payload: dict, request: ProviderRequest) -> str:
        answer: list[str] = []
        answer_chars = 0
        finish_reason: str | None = None
        last_usage: dict | None = None
        frame_count = 0

        def parse_frame(text: str) -> None:
            nonlocal answer_chars, finish_reason, last_usage, frame_count
            if not text or text == "[DONE]":
                return
            if len(text) > 1_000_000:
                raise ProviderFailure("Gemini SSE frame exceeds bounded size")
            try:
                frame = json.loads(text)
            except (ValueError, TypeError) as exc:
                raise TransientProviderFailure("Gemini SSE contained malformed JSON") from exc
            if not isinstance(frame, dict):
                raise TransientProviderFailure("Gemini SSE contained invalid frame type")
            frame_count += 1
            if frame_count > 4096:
                raise ProviderFailure("Gemini SSE exceeded frame limit")
            if frame.get("promptFeedback", {}).get("blockReason"):
                raise ProviderFailure("Gemini SSE content blocked", finish_reason="SAFETY")
            if "error" in frame:
                err = frame.get("error") if isinstance(frame.get("error"), dict) else {}
                code = err.get("code") if isinstance(err.get("code"), int) else None
                if code == 429:
                    if answer:
                        raise PartialGenerationError(
                            "Gemini SSE interrupted by provider quota", "".join(answer), "NETWORK_ERROR",
                        )
                    # Daily limits are recognized by structured quota IDs.
                    if "perday" in str(err.get("details", "")).lower():
                        raise DailyProviderQuotaFailure("Gemini streaming daily quota exhausted")
                    raise TransientProviderFailure(
                        "Gemini SSE temporarily rate limited", status_code=429,
                    )
                if answer and code in {408, 500, 502, 503, 504}:
                    raise PartialGenerationError(
                        "Gemini SSE interrupted by provider", "".join(answer), "NETWORK_ERROR",
                    )
                if code in {408, 500, 502, 503, 504}:
                    raise TransientProviderFailure(
                        "Gemini SSE provider unavailable", status_code=code,
                    )
                raise ProviderFailure("Gemini streaming provider reported a terminal error")
            usage = frame.get("usageMetadata")
            if isinstance(usage, dict):
                last_usage = usage
            candidates = frame.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                return
            first = candidates[0] if isinstance(candidates[0], dict) else {}
            if first.get("finishReason"):
                finish_reason = str(first["finishReason"])
            content = first.get("content")
            parts = content.get("parts") if isinstance(content, dict) else []
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict) or part.get("thought") is True:
                    continue
                value = part.get("text")
                if not isinstance(value, str) or not value:
                    continue
                answer_chars += len(value)
                if answer_chars > 2_000_000:
                    raise ProviderFailure("Gemini SSE answer exceeded bounded size")
                answer.append(value)

        try:
            async with client.stream(
                "POST", url, headers={"x-goog-api-key": self.api_key}, json=payload,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    if response.status_code == 429 and _daily_quota_violation(response):
                        raise DailyProviderQuotaFailure("Gemini streaming daily quota exhausted")
                    if response.status_code in (408, 429, 500, 502, 503, 504):
                        raise TransientProviderFailure(
                            f"Gemini streaming temporarily unavailable (HTTP {response.status_code})",
                            retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
                            status_code=response.status_code,
                        )
                    raise ProviderFailure(
                        f"Gemini streaming request failed (HTTP {response.status_code})",
                        status_code=response.status_code,
                    )
                lines: list[str] = []
                async for line in response.aiter_lines():
                    if len(line) > 1_000_000:
                        raise ProviderFailure("Gemini SSE data line exceeded bounded size")
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip())
                    elif not line and lines:
                        parse_frame("\n".join(lines))
                        lines.clear()
                    elif line and not line.startswith(":"):
                        # Ignore SSE event/id/retry metadata, never user content.
                        continue
                if lines:
                    parse_frame("\n".join(lines))
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            if answer:
                raise PartialGenerationError(
                    "Gemini stream interrupted after answer text",
                    "".join(answer), "STREAM_TRUNCATED",
                ) from exc
            raise TransientProviderFailure(
                "Gemini stream network failure before answer", is_network_error=True,
            ) from exc
        text = "".join(answer)
        # An explicit policy/safety stop always overrides accumulated text.
        if finish_reason and finish_reason not in {"STOP", "MAX_TOKENS"}:
            raise ProviderFailure(
                "Gemini SSE response stopped without successful completion",
                finish_reason=finish_reason if finish_reason in
                {"SAFETY", "PROHIBITED_CONTENT", "RECITATION", "SPII"} else None,
            )
        if finish_reason != "STOP":
            if text.strip():
                raise PartialGenerationError(
                    "Gemini SSE stopped before successful completion",
                    text, finish_reason or "STREAM_TRUNCATED",
                )
            raise TransientProviderFailure("Gemini SSE ended without a complete answer")
        if not text.strip():
            raise ProviderFailure("Gemini SSE returned empty answer")
        if request.on_usage is not None and isinstance(last_usage, dict):
            safe = {
                name: int(last_usage[name]) for name in
                ("promptTokenCount", "candidatesTokenCount", "totalTokenCount",
                 "thoughtsTokenCount", "cachedContentTokenCount")
                if isinstance(last_usage.get(name), int)
                and not isinstance(last_usage[name], bool)
                and last_usage[name] >= 0
            }
            if safe:
                request.on_usage(safe)
        self.completed_calls += 1
        return text
