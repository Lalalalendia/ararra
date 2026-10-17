"""Server adapter for the verbatim full_cycle_app Gemini scheduler.

The desktop scheduler owns admission, aging, RPM/RPS/TPM and circuit state;
the existing server provider owns HTTP and the SQLite RPD ledger owns daily
accounting. Exactly one configured API key and one worker are supported here.
"""
from __future__ import annotations

from chaptera_translation_core.api.errors import OperationCancelledError, PartialGenerationError
from chaptera_translation_core.ports import TranslationCancelled

from .gemini_scheduler import ApiNetworkController, ApiRequestScheduler
from .provider import DailyProviderQuotaFailure, ProviderFailure, TransientProviderFailure
from .quota import DailyQuotaExhausted


class ScheduledGeminiProvider:
    """Admit requests before the RPD-reserving provider, with desktop behavior.

    One instance lives for the worker's lifetime so a chapter's context,
    translation and repair all share the same scheduler and network controller.
    The quota-controlled provider, when configured, must be wrapped INSIDE it.
    """

    def __init__(
        self, provider, *,
        rpm_limit: int = 5,
        tpm_limit: int = 250_000,
        reserved_output_tokens: int = 65_536,
        rps_limit: float = 0.0,
        max_inflight: int = 1,
        start_interval_seconds: float = 0.35,
        scheduler=None,
        network_controller=None,
    ):
        if rpm_limit < 0 or tpm_limit < 0 or reserved_output_tokens < 0 or rps_limit < 0:
            raise ValueError("Gemini limits must not be negative")
        if tpm_limit and reserved_output_tokens == 0:
            raise ValueError("TPM gating requires a positive output-token reservation")
        self.provider = provider
        self.scheduler = scheduler or ApiRequestScheduler(
            rpm_limit, tpm_limit=tpm_limit, rps_limit=rps_limit,
        )
        self.network = network_controller or ApiNetworkController(
            max_inflight=max_inflight,
            start_interval_seconds=start_interval_seconds,
        )
        self.reserved_output_tokens = reserved_output_tokens

    @property
    def completed_calls(self) -> int:
        # Existing worker's expensive-partial-work protection must survive wrapping.
        return int(getattr(self.provider, "completed_calls", 0))

    @property
    def sent_calls(self) -> int:
        return int(getattr(self.provider, "sent_calls", 0))

    def snapshot(self) -> dict:
        # No API keys, prompt texts or Google project identifiers.
        return {
            "scheduler": self.scheduler.snapshot(),
            "network": self.network.snapshot(),
        }

    async def generate(self, request) -> str:
        role = getattr(request, "request_role", "translation") or "translation"
        cancelled = getattr(request, "is_cancelled", None)
        if callable(cancelled) and cancelled():
            raise TranslationCancelled("Translation cancelled before API admission")
        token_cost = 0
        if self.scheduler.tpm_limit:
            # Exact conservative envelope from desktop BaseApiHandler:
            # max possible input tokens <= UTF-8 bytes + 512.
            token_cost = len(request.prompt.encode("utf-8")) + 512 + self.reserved_output_tokens
        try:
            permit = await self.scheduler.acquire(role, token_cost=token_cost, is_cancelled=cancelled)
        except ValueError as exc:
            # No network/RPD use and no pointless retries on a known
            # oversized request: splitting is a separate engine action.
            raise ProviderFailure("Gemini request exceeds configured TPM envelope") from exc
        except OperationCancelledError as exc:
            raise TranslationCancelled("Translation cancelled during RPM/TPM wait") from exc
        try:
            net_permit = await self.network.acquire(role, is_cancelled=cancelled)
        except OperationCancelledError as exc:
            self.scheduler.refund(permit)
            raise TranslationCancelled("Translation cancelled during network admission") from exc
        except BaseException:
            self.scheduler.refund(permit)
            raise
        try:
            if callable(cancelled) and cancelled():
                self.scheduler.refund(permit)
                raise TranslationCancelled("Translation cancelled before request start")
            # QuotaControlledProvider increments sent_calls only after its
            # SQLite reservation. A local RPD denial never starts HTTP.
            sent_before = getattr(self.provider, "sent_calls", None)
            result = await self.provider.generate(request)
        except DailyQuotaExhausted:
            if sent_before is not None and sent_before == getattr(self.provider, "sent_calls", None):
                self.scheduler.refund(permit)
            self.network.finish(net_permit, "neutral")
            raise
        except DailyProviderQuotaFailure:
            self.network.finish(net_permit, "neutral")
            raise
        except PartialGenerationError as exc:
            # Desktop exposes partial text to the core, where the canonical
            # source-coverage validator decides whether any part is reusable.
            self.network.finish(net_permit,
                                "network_error" if exc.is_network_partial else "neutral")
            raise
        except TransientProviderFailure as exc:
            status = getattr(exc, "status_code", None)
            if status == 429:
                self.scheduler.apply_cooldown(exc.retry_after_seconds or 60.0)
                if self.scheduler.get_rpm() > 1:
                    self.scheduler.decrease_rpm(percentage=25)
                self.network.finish(net_permit, "neutral")
            elif status in (500, 502, 503, 504):
                self.network.finish(net_permit, "provider_error",
                                    retry_after=exc.retry_after_seconds or 0.0)
            elif status == 408 or getattr(exc, "is_network_error", False):
                self.network.finish(net_permit, "network_error")
            else:
                self.network.finish(net_permit, "neutral")
            raise
        except BaseException:
            self.network.finish(net_permit, "neutral")
            raise
        else:
            self.network.finish(net_permit, "success")
            return result
