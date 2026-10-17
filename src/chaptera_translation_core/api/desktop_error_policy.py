"""Verbatim desktop ErrorAnalyzer exception classification.

Extracted from src/full_cycle_app/translation/gemini/core/worker_helpers/
error_analyzer.py. Original classification order is significant: provider
unavailable precedes NetworkError and RequestBudget precedes RateLimit.
Do not change these rules independently of the desktop application.
The GUI/desktop queue actions are intentionally not copied here.
"""
from __future__ import annotations

from .errors import (
    ErrorType, ProviderUnavailableError, NetworkError, PartialGenerationError,
    ContentFilterError, LocationBlockedError, RequestBudgetExceededError,
    ApiKeyAuthenticationError, RateLimitExceededError, TemporaryRateLimitError,
    ModelNotFoundError, ProviderRequestError, ValidationFailedError,
    ProvenanceConflictError, OperationCancelledError,
)


class DesktopErrorClassifier:
    def _classify_exception(self, exc) -> ErrorType:
        # Классифицирует исключение по причине отказа, отделяя provider outage от transport и quota.
        if isinstance(exc, ProviderUnavailableError):
            return ErrorType.PROVIDER_UNAVAILABLE
        if isinstance(exc, NetworkError):
            return ErrorType.NETWORK
        if isinstance(exc, PartialGenerationError) and bool(getattr(exc, 'is_network_partial', False)):
            return ErrorType.NETWORK
        if isinstance(exc, ContentFilterError):
            return ErrorType.CONTENT_FILTER
        if isinstance(exc, LocationBlockedError):
            return ErrorType.GEOBLOCK
        if isinstance(exc, RequestBudgetExceededError):
            return ErrorType.REQUEST_BUDGET
        if isinstance(exc, ApiKeyAuthenticationError):
            return ErrorType.AUTHENTICATION
        if isinstance(exc, RateLimitExceededError):
            return ErrorType.QUOTA_EXCEEDED
        if isinstance(exc, PartialGenerationError):
            return ErrorType.PARTIAL_GENERATION
        if isinstance(exc, TemporaryRateLimitError):
            return ErrorType.TEMPORARY_LIMIT
        if isinstance(exc, ModelNotFoundError):
            return ErrorType.MODEL_NOT_FOUND
        if isinstance(exc, ProviderRequestError):
            return ErrorType.PROVIDER_REQUEST
        if isinstance(exc, ValidationFailedError):
            return ErrorType.VALIDATION
        if isinstance(exc, ProvenanceConflictError):
            return ErrorType.INTERNAL_INVARIANT
        if isinstance(exc, OperationCancelledError):
            return ErrorType.CANCEL
        # ----------------------------------------------
        return ErrorType.API_ERROR

    @classmethod
    def classify(cls, exc) -> ErrorType:
        return cls()._classify_exception(exc)
