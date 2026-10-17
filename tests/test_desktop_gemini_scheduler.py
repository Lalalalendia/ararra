"""Offline RE-06 parity gates against the supplied full_cycle_app desktop scheduler."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from chaptera_server.gemini_scheduler import ApiNetworkController, ApiRequestScheduler
from chaptera_server.provider import GeminiProvider, ProviderFailure, TransientProviderFailure
from chaptera_server.quota import DailyQuotaExhausted, DailyQuotaLedger, QuotaControlledProvider
from chaptera_server.scheduled_provider import ScheduledGeminiProvider
from chaptera_server.storage import QueueStore
from chaptera_server.config import Settings
from chaptera_translation_core.ports import ProviderRequest, TranslationCancelled


DESKTOP_SOURCE_SHA256 = "8fc1b4c2293b08a9911424ff0b0bdeda714cb3531445cbe7a041ff1e72cc24f5"


class ManualClock:
    def __init__(self, now=100.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    async def sleep(self, duration):
        self.now += float(duration)
        await asyncio.sleep(0)


def test_port_is_original_desktop_scheduler_not_a_reimplementation():
    """Strip only the header and adapted import; original algorithm must match."""
    path = Path(__file__).parents[1] / "src/chaptera_server/gemini_scheduler.py"
    source = path.read_text(encoding="utf-8")
    assert "Direct port from full_cycle_app" in source
    original = "# -*- coding: utf-8 -*-\n" + source.split("# -*- coding: utf-8 -*-\n", 1)[1]
    original = original.replace(
        "from chaptera_translation_core.api.errors import OperationCancelledError",
        "from .errors import OperationCancelledError",
    ).rstrip("\n")
    assert hashlib.sha256(original.encode("utf-8")).hexdigest() == DESKTOP_SOURCE_SHA256


def test_desktop_rpm_and_tpm_permits_with_refund_and_cooldown():
    clock = ManualClock()
    scheduler = ApiRequestScheduler(5, tpm_limit=100, clock=clock, sleep_func=clock.sleep,
                                    aging_seconds=1000)
    async def run():
        a = await scheduler.acquire("translation", token_cost=60)
        b = await scheduler.acquire("targeted_repair", token_cost=30)
        assert b.granted_at >= a.granted_at + 12
        assert scheduler.snapshot()["tpm_reserved"] == 90
        c = await scheduler.acquire("translation", token_cost=20)
        assert c.granted_at >= a.granted_at + 60
        assert scheduler.refund(c)
        scheduler.apply_cooldown(30)
        assert scheduler.snapshot()["cooldown_remaining"] >= 29.9
        scheduler.decrease_rpm(percentage=25)
        assert scheduler.snapshot()["rpm_limit"] == 4
    asyncio.run(run())


def test_desktop_network_circuit_opens_after_three_provider_failures():
    clock = ManualClock()
    net = ApiNetworkController(
        max_inflight=3, start_interval_seconds=0,
        clock=clock, sleep_func=clock.sleep, jitter_func=lambda n: n,
    )
    async def run():
        for _ in range(3):
            permit = await net.acquire("translation")
            net.finish(permit, "provider_error")
        state = net.snapshot()
        assert state["state"] == "open"
        assert state["failure_scope"] == "provider"
        clock.now += 65
        probe = await net.acquire("targeted_repair")
        assert probe.is_probe
        assert net.finish(probe, "success")["circuit_remaining"] == 0
    asyncio.run(run())


def request(*, role="translation", cancel=None, prompt="hello"):
    return ProviderRequest(
        "chapter-1", prompt, "<p>source</p>", 1, request_role=role, is_cancelled=cancel,
    )


class ReturningProvider:
    def __init__(self):
        self.calls = 0
        self.completed_calls = 0

    async def generate(self, req):
        self.calls += 1
        self.completed_calls += 1
        return "translation"


def test_daily_cap_runs_after_desktop_schedulers_and_is_not_double_consumed(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    daily = DailyQuotaLedger(store, "the-one-project", "model-one", 1)
    downstream = ReturningProvider()
    quota = QuotaControlledProvider(downstream, daily)
    scheduled = ScheduledGeminiProvider(
        quota, rpm_limit=0, tpm_limit=0, start_interval_seconds=0,
    )
    assert asyncio.run(scheduled.generate(request())) == "translation"
    with pytest.raises(DailyQuotaExhausted):
        asyncio.run(scheduled.generate(request(role="targeted_repair")))
    assert daily.status()["used"] == 1
    assert downstream.calls == 1
    assert scheduled.completed_calls == 1
    assert scheduled.snapshot()["network"]["inflight"] == 0
    assert scheduled.snapshot()["scheduler"]["queue_depth"] == 0


def test_temporary_429_decreases_desktop_rpm_and_does_not_trip_circuit():
    clock = ManualClock()
    scheduler = ApiRequestScheduler(5, clock=clock, sleep_func=clock.sleep)
    net = ApiNetworkController(1, 0, clock=clock, sleep_func=clock.sleep)

    class Throttled:
        calls = 0
        async def generate(self, req):
            self.calls += 1
            if self.calls == 1:
                raise TransientProviderFailure(
                    "Gemini temporarily unavailable (HTTP 429)",
                    status_code=429, retry_after_seconds=17,
                )
            return "ok"

    downstream = Throttled()
    scheduled = ScheduledGeminiProvider(
        downstream, scheduler=scheduler, network_controller=net,
    )
    with pytest.raises(TransientProviderFailure):
        asyncio.run(scheduled.generate(request()))
    assert scheduler.get_rpm() == 4
    assert scheduler.snapshot()["cooldown_remaining"] > 16
    assert scheduled.snapshot()["network"]["state"] == "healthy"
    assert asyncio.run(scheduled.generate(request())) == "ok"
    assert downstream.calls == 2


def test_request_larger_than_tpm_is_denied_before_provider_and_rpd(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    ledger = DailyQuotaLedger(store, "one-project", "some-model", 21)
    downstream = ReturningProvider()
    scheduled = ScheduledGeminiProvider(
        QuotaControlledProvider(downstream, ledger),
        rpm_limit=5, tpm_limit=200, reserved_output_tokens=10,
    )
    with pytest.raises(ProviderFailure, match="TPM envelope"):
        asyncio.run(scheduled.generate(request(prompt="large input " * 300)))
    assert downstream.calls == 0
    assert ledger.status()["used"] == 0


def test_cancelled_request_does_not_consume_provider_or_rpd(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    ledger = DailyQuotaLedger(store, "one-project", "some-model", 21)
    downstream = ReturningProvider()
    scheduled = ScheduledGeminiProvider(
        QuotaControlledProvider(downstream, ledger),
        rpm_limit=5, tpm_limit=250000,
    )
    with pytest.raises(TranslationCancelled):
        asyncio.run(scheduled.generate(request(cancel=lambda: True)))
    assert ledger.status()["used"] == 0
    assert downstream.calls == 0


def test_desktop_default_model_limits_and_http_output_cap():
    settings = Settings(db_path=Path("ignored.sqlite3"))
    assert (settings.gemini_rpm, settings.gemini_tpm,
            settings.gemini_reserved_output_tokens) == (5, 250000, 65536)
    seen = []
    def respond(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}],
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            provider = GeminiProvider("offline-test-key", "gemini-test", client=http,
                                      max_output_tokens=int(65536 * 0.98))
            return await provider.generate(request())
    assert asyncio.run(run()) == "ok"
    assert seen[0]["generationConfig"]["maxOutputTokens"] == 64225
