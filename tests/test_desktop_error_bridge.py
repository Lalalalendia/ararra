"""Offline parity gates for desktop full_cycle_app ErrorAnalyzer on the server.

No paid Gemini calls. The classification rules are copied, the bridge only
adapts native HTTP metadata; retries and aborts run against real SQLite jobs.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import chaptera_server.storage as storage
from chaptera_translation_core.api.desktop_error_policy import DesktopErrorClassifier
from chaptera_translation_core.api.errors import (
    ApiKeyAuthenticationError, ContentFilterError, ErrorType, LocationBlockedError,
    ModelNotFoundError, NetworkError, OperationCancelledError, PartialGenerationError,
    ProviderRequestError, ProviderUnavailableError, RateLimitExceededError,
    RequestBudgetExceededError, TemporaryRateLimitError, ValidationFailedError,
)
from chaptera_translation_core.ports import ProviderRequest
from chaptera_server.gemini_error_bridge import (
    NON_COUNTABLE_TRANSIENT, desktop_error_type, desktop_transient_delay,
)
from chaptera_server.provider import (
    DailyProviderQuotaFailure, GeminiProvider, ProviderFailure, TransientProviderFailure,
)
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker


@pytest.mark.parametrize(("failure", "expected"), [
    (ProviderUnavailableError("provider 503"), ErrorType.PROVIDER_UNAVAILABLE),
    (NetworkError("network"), ErrorType.NETWORK),
    (PartialGenerationError("partial network", "prefix", "CONNECTION_RESET"), ErrorType.NETWORK),
    (PartialGenerationError("short", "prefix", "MAX_TOKENS"), ErrorType.PARTIAL_GENERATION),
    (ContentFilterError("blocked"), ErrorType.CONTENT_FILTER),
    (LocationBlockedError("blocked region"), ErrorType.GEOBLOCK),
    (RequestBudgetExceededError("daily"), ErrorType.REQUEST_BUDGET),
    (ApiKeyAuthenticationError("credential"), ErrorType.AUTHENTICATION),
    (RateLimitExceededError("quota"), ErrorType.QUOTA_EXCEEDED),
    (TemporaryRateLimitError("minute"), ErrorType.TEMPORARY_LIMIT),
    (ModelNotFoundError("model"), ErrorType.MODEL_NOT_FOUND),
    (ProviderRequestError("bad prompt"), ErrorType.PROVIDER_REQUEST),
    (ValidationFailedError("bad html"), ErrorType.VALIDATION),
    (OperationCancelledError("cancel"), ErrorType.CANCEL),
    (RuntimeError("unknown"), ErrorType.API_ERROR),
])
def test_unmodified_desktop_error_type_priority(failure, expected):
    assert DesktopErrorClassifier.classify(failure) is expected


@pytest.mark.parametrize(("server_error", "kind"), [
    (TransientProviderFailure("HTTP 429", status_code=429), ErrorType.TEMPORARY_LIMIT),
    (TransientProviderFailure("HTTP 503", status_code=503), ErrorType.PROVIDER_UNAVAILABLE),
    (TransientProviderFailure("network", is_network_error=True), ErrorType.NETWORK),
    (TransientProviderFailure("HTTP 408", status_code=408), ErrorType.NETWORK),
    (TransientProviderFailure("malformed JSON"), ErrorType.API_ERROR),
    (DailyProviderQuotaFailure("RPD"), ErrorType.REQUEST_BUDGET),
    (ProviderFailure("HTTP 400", status_code=400), ErrorType.PROVIDER_REQUEST),
    (ProviderFailure("HTTP 401", status_code=401), ErrorType.AUTHENTICATION),
    (ProviderFailure("HTTP 403", status_code=403), ErrorType.PROVIDER_REQUEST),
    (ProviderFailure("HTTP 404", status_code=404), ErrorType.MODEL_NOT_FOUND),
    (ProviderFailure("SAFETY", finish_reason="SAFETY"), ErrorType.CONTENT_FILTER),
    (ProviderFailure("filtered", finish_reason="RECITATION"), ErrorType.CONTENT_FILTER),
    (ProviderFailure("no candidates"), ErrorType.API_ERROR),
])
def test_server_metadata_adapts_to_existing_desktop_categories(server_error, kind):
    assert desktop_error_type(server_error) is kind
    if kind in NON_COUNTABLE_TRANSIENT:
        assert desktop_transient_delay(server_error, kind) >= 0


@pytest.mark.parametrize(("status", "kind"), [
    (400, ErrorType.PROVIDER_REQUEST),
    (401, ErrorType.AUTHENTICATION),
    (403, ErrorType.PROVIDER_REQUEST),
    (404, ErrorType.MODEL_NOT_FOUND),
    (503, ErrorType.PROVIDER_UNAVAILABLE),
])
def test_http_adapter_preserves_safe_status_for_desktop_error_type(status, kind):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _req: httpx.Response(status, json={"error": {"message": "private chapter prompt"}})
        )) as client:
            provider = GeminiProvider("secret-key", "gemini-test", client=client)
            with pytest.raises(ProviderFailure) as error:
                await provider.generate(ProviderRequest("1", "private", "source", 1))
            assert "private" not in str(error.value)
            assert "secret-key" not in str(error.value)
            assert desktop_error_type(error.value) is kind
    asyncio.run(run())


def test_provider_safety_finish_reason_not_ordinary_api_error():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={
                "candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
            })
        )) as client:
            with pytest.raises(ProviderFailure) as error:
                await GeminiProvider("secret", "gemini-test", client=client).generate(
                    ProviderRequest("1", "source", "private", 1),
                )
            assert desktop_error_type(error.value) is ErrorType.CONTENT_FILTER
    asyncio.run(run())


class AlwaysTransient:
    def __init__(self, status_code=None, is_network_error=False):
        self.status_code = status_code
        self.is_network_error = is_network_error
        self.calls = 0
    async def generate(self, _request):
        self.calls += 1
        raise TransientProviderFailure(
            "sanitized provider unavailable",
            status_code=self.status_code,
            is_network_error=self.is_network_error,
        )


@pytest.mark.parametrize(("status", "network", "kind"), [
    (429, False, "TEMPORARY_LIMIT"),
    (503, False, "PROVIDER_UNAVAILABLE"),
    (None, True, "NETWORK"),
])
def test_repeated_desktop_transient_does_not_exhaust_finite_job_retries(
    tmp_path, monkeypatch, status, network, kind,
):
    clock = [1000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("moderator", JobSpec(
        "chapter-1", "<p>世界。</p>", max_attempts=1,
    ))
    provider = AlwaysTransient(status, network)
    worker = TranslationWorker(store, provider)
    for iteration in range(12):
        assert asyncio.run(worker.run_once()) is True
        state = store.get_job(job["id"])
        assert state["status"] == "retry_wait"
        assert state["attempts"] == 0
        assert store.claim_next() is None
        clock[0] += 400
    assert provider.calls == 12
    events = [row["detail"] for row in store.events(job["id"])
              if row["stage"] == "transient_wait"]
    assert len(events) == 12
    assert all("kind=" not in e or kind in e for e in events)
    assert all("private" not in e for e in events)
    with store._connect() as con:
        assert con.execute("SELECT transient_streak FROM jobs WHERE id=?", (job["id"],)
                           ).fetchone()[0] == 12


def test_safe_retry_after_is_honored_without_spending_attempt(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    class RateLimited:
        async def generate(self, _request):
            raise TransientProviderFailure("HTTP 429", status_code=429,
                                           retry_after_seconds=650)
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("moderator", JobSpec("c", "<p>世界。</p>", max_attempts=1))
    assert asyncio.run(TranslationWorker(store, RateLimited()).run_once())
    with store._connect() as con:
        row = con.execute("SELECT status,attempts,run_after FROM jobs WHERE id=?",
                          (job["id"],)).fetchone()
        assert row["status"] == "retry_wait"
        assert row["attempts"] == 0
        assert row["run_after"] >= 1650


def test_bad_credential_halts_shared_single_key_worker_without_queuing_others(tmp_path):
    class BadKey:
        calls = 0
        async def generate(self, _request):
            self.calls += 1
            raise ProviderFailure("Gemini request failed (HTTP 401)", status_code=401)
    store = QueueStore(tmp_path / "jobs.db")
    first = store.create_job("mod", JobSpec("first", "<p>世界。</p>"))
    second = store.create_job("mod", JobSpec("second", "<p>世界。</p>"))
    provider = BadKey()
    worker = TranslationWorker(store, provider)
    assert asyncio.run(worker.run_once())
    assert store.get_job(first["id"])["status"] == "failed"
    assert store.get_job(second["id"])["status"] == "queued"
    assert asyncio.run(worker.run_once()) is False
    assert provider.calls == 1
    assert any(e["stage"] == "provider_config_blocked" for e in store.events(first["id"]))


def test_generic_200_malformed_response_is_countable_and_finite(tmp_path):
    class BadJSON:
        calls = 0
        async def generate(self, _request):
            self.calls += 1
            raise TransientProviderFailure("Gemini returned malformed JSON")
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("mod", JobSpec("bad", "<p>世界。</p>", max_attempts=1))
    bad = BadJSON()
    assert asyncio.run(TranslationWorker(store, bad).run_once())
    assert store.get_job(job["id"])["status"] == "failed"
    assert bad.calls == 1


def test_noncountable_transient_cancellation_respects_fenced_cleanup(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("mod", JobSpec("a", "<p>世界。</p>"))
    _, token = store.claim_next()
    assert store.request_cancel(job["id"])["status"] == "running"
    assert store.defer_desktop_transient(job["id"], token,
                                         kind="NETWORK", initial_delay_seconds=20)
    assert store.get_job(job["id"])["status"] == "cancelled"
    assert store.claim_next() is None
