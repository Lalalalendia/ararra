from enum import Enum, auto
class ErrorType(Enum):
    GEOBLOCK = auto()
    QUOTA_EXCEEDED = auto()
    AUTHENTICATION = auto()
    TEMPORARY_LIMIT = auto()
    REQUEST_BUDGET = auto()
    MODEL_NOT_FOUND = auto()
    PROVIDER_UNAVAILABLE = auto()
    NETWORK = auto()
    VALIDATION = auto()
    PROVIDER_REQUEST = auto()
    INTERNAL_INVARIANT = auto()
    API_ERROR = auto()
    CANCEL = auto()
    CONTENT_FILTER = auto() 
    PARTIAL_GENERATION = auto()
class WorkerAction(Enum):
    RETRY_NON_COUNTABLE = auto()
    RETRY_COUNTABLE = auto()
    RETRY_FRESH_ONCE = auto()
    MODEL_NOT_FOUND = auto()
    FAIL_AND_ATTEMPT_CHUNK = auto()
    FAIL_PERMANENTLY = auto()
    ABORT_WORKER = auto()
class SuccessSignal(Exception):
    def __init__(self, message="Задача успешно завершена досрочно.", status_code="SUCCESS_SIGNAL"):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)
class ContentFilterError(Exception): pass
class OperationCancelledError(Exception): pass
class LocationBlockedError(Exception): pass
class ModelNotFoundError(Exception): pass
class ValidationFailedError(Exception): pass
class ProviderRequestError(Exception):
    """Provider rejected the request shape/configuration; rotating API keys cannot fix it."""
    def __init__(self, message, *, http_status=400, provider_status=None, provider_reason=None):
        super().__init__(message)
        self.http_status = int(http_status or 400)
        self.provider_status = provider_status
        self.provider_reason = provider_reason
class ProvenanceConflictError(ValueError): pass
class TranslationValidationRecoveryExhaustedError(ValidationFailedError): pass
class ApiKeyAuthenticationError(Exception):
    """Provider rejected this concrete credential; rotating keys may recover the session."""
    def __init__(self, message, *, http_status=None, provider_status=None, provider_reason=None):
        super().__init__(message)
        self.http_status = int(http_status) if http_status is not None else None
        self.provider_status = provider_status
        self.provider_reason = provider_reason
class RateLimitExceededError(Exception):
    def __init__(self, message):
        # Сохраняет terminal provider/quota error без дополнительной mutable state.
        super().__init__(message)
class RequestBudgetExceededError(RateLimitExceededError):
    def __init__(self, message, request_role='unknown', remaining=None, required=None):
        # Описывает локальный RPD preflight отказ до отправки provider-запроса.
        super().__init__(message)
        self.request_role = request_role
        self.remaining = remaining
        self.required = required
class TemporaryRateLimitError(Exception):
    def __init__(self, message, delay_seconds=60):
        super().__init__(message)
        self.delay_seconds = delay_seconds
class NetworkError(Exception):
    def __init__(self, message, delay_seconds=30):
        super().__init__(message)
        self.delay_seconds = delay_seconds
class TransportStalledError(NetworkError):
    def __init__(self, message, delay_seconds=10, transport_stage=None):
        # Represents an ambiguous no-progress timeout. It remains retryable, but does not prove local OFFLINE state.
        super().__init__(message, delay_seconds=delay_seconds)
        self.transport_stage = str(transport_stage or "UNKNOWN")
        self.availability_scope = "unknown"
class ProviderUnavailableError(NetworkError):
    def __init__(
        self,
        message,
        delay_seconds=20,
        *,
        http_status=None,
        provider_status=None,
        provider_reason=None,
        failure_stage=None,
        model_id=None,
    ):
        # Описывает временную недоступность provider/model, не связанную с квотой конкретного ключа.
        # Дополнительные поля сохраняют machine-readable первопричину для логов/failover.
        super().__init__(message, delay_seconds=delay_seconds)
        self.http_status = int(http_status) if http_status is not None else None
        self.provider_status = str(provider_status or '').strip().upper() or None
        self.provider_reason = str(provider_reason or '').strip().upper() or None
        self.failure_stage = str(failure_stage or '').strip().upper() or None
        self.model_id = str(model_id or '').strip() or None
class GracefulShutdownInterrupt(Exception):
    pass
class PartialGenerationError(Exception):
    def __init__(self, message, partial_text, reason, delay_seconds=10):
        # Хранит recoverable partial provider output и явную transport-причину без потери checkpoint metadata.
        super().__init__(message)
        self.partial_text = partial_text
        self.reason = reason
        self.delay_seconds = max(0.0, float(delay_seconds or 0.0))
    @property
    def is_network_partial(self) -> bool:
        # Отличает физический обрыв transport от содержательно неполного ответа модели.
        return str(self.reason or '').upper() in {
            'NETWORK_ERROR', 'TRANSPORT_STALLED', 'TRANSPORT_TRUNCATED', 'STREAM_TRUNCATED', 'CONNECTION_RESET'
        }
class TranslationDebtDeferredError(Exception):
    def __init__(self, message='Translation task deferred until durable debt is resolved.', *, leaf_state=None, split_tree=None):
        # Переносит leaf/split state внутри processor; durable parent defer выполняется один раз после обхода всех siblings.
        super().__init__(message)
        self.leaf_state = leaf_state
        self.split_tree = split_tree
