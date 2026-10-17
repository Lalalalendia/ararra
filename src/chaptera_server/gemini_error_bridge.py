"""Server boundary for the desktop's canonical error classification.

The source classifier lives in chaptera_translation_core.api.desktop_error_policy.
This bridge only translates the server's sanitized HTTP/transport exceptions
into existing desktop exception classes; it does not change the desktop policy.
"""
from __future__ import annotations

from chaptera_translation_core.api.desktop_error_policy import DesktopErrorClassifier
from chaptera_translation_core.api.errors import (
    ApiKeyAuthenticationError, ContentFilterError, ErrorType,
    ModelNotFoundError, NetworkError, ProviderRequestError,
    ProviderUnavailableError, RequestBudgetExceededError,
    TemporaryRateLimitError, TransportStalledError,
)

from .provider import ProviderFailure, TransientProviderFailure, DailyProviderQuotaFailure


NON_COUNTABLE_TRANSIENT = frozenset({
    ErrorType.TEMPORARY_LIMIT, ErrorType.PROVIDER_UNAVAILABLE, ErrorType.NETWORK,
})
FATAL_WORKER_CONFIG = frozenset({
    ErrorType.AUTHENTICATION, ErrorType.MODEL_NOT_FOUND, ErrorType.PROVIDER_REQUEST,
    ErrorType.GEOBLOCK, ErrorType.QUOTA_EXCEEDED,
})


def _as_desktop_exception(exc: ProviderFailure) -> Exception:
    """Translate only known provider machine codes, never response text."""
    if isinstance(exc, DailyProviderQuotaFailure):
        return RequestBudgetExceededError("Gemini daily quota exceeded")
    if isinstance(exc, TransientProviderFailure):
        status = exc.status_code
        if status == 429:
            return TemporaryRateLimitError(
                "Gemini temporary request limit",
                delay_seconds=max(0.0, float(exc.retry_after_seconds or 60.0)),
            )
        if status in {500, 502, 503, 504}:
            return ProviderUnavailableError(
                "Gemini model temporarily unavailable",
                delay_seconds=max(0.0, float(exc.retry_after_seconds or 20.0)),
                http_status=status,
            )
        if status == 408:
            return TransportStalledError(
                "Gemini HTTP request stalled", delay_seconds=10, transport_stage="HTTP_408",
            )
        if exc.is_network_error:
            return NetworkError("Gemini transport unavailable", delay_seconds=30)
        # Malformed 200 OK or MAX_TOKENS without a durable streamed segment
        # stays a countable API failure. Do not label it a network outage.
        return exc
    if exc.status_code == 401:
        return ApiKeyAuthenticationError("Gemini credential rejected", http_status=401)
    if exc.status_code == 404:
        return ModelNotFoundError("Gemini model not found")
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        return ProviderRequestError(
            "Gemini request or access policy rejected", http_status=exc.status_code,
        )
    if exc.finish_reason in {"SAFETY", "PROHIBITED_CONTENT", "SPII", "RECITATION"}:
        return ContentFilterError("Gemini content filter stopped this response")
    return exc


def desktop_error_type(exc: ProviderFailure) -> ErrorType:
    """Return exact ErrorAnalyzer error category after transport adaptation."""
    return DesktopErrorClassifier.classify(_as_desktop_exception(exc))


def desktop_transient_delay(exc: TransientProviderFailure, kind: ErrorType) -> float:
    """Initial (pre-streak) delay from the desktop exception, RPD handled apart."""
    converted = _as_desktop_exception(exc)
    if kind not in NON_COUNTABLE_TRANSIENT:
        return max(0.0, float(exc.retry_after_seconds or 0.0))
    return max(0.0, float(getattr(converted, "delay_seconds", 0.0) or 0.0))
