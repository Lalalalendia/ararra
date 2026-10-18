"""Offline RE-06 tests: project-level daily RPD, DST and safe worker resume."""
from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import chaptera_server.quota as quota
import chaptera_server.storage as storage
from chaptera_server.provider import DailyProviderQuotaFailure
from chaptera_server.quota import (
    DailyQuotaExhausted, DailyQuotaLedger, QuotaControlledProvider,
    pacific_window,
)
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker

PACIFIC = ZoneInfo("America/Los_Angeles")


def ts(year, month, day, hour=0):
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


def test_pacific_midnight_rollover_and_daylight_saving():
    march = datetime(2026, 3, 8, 0, tzinfo=PACIFIC).timestamp()
    november = datetime(2026, 11, 1, 0, tzinfo=PACIFIC).timestamp()
    assert pacific_window(march)[0] == "2026-03-08"
    assert pacific_window(march)[1] - march == 23 * 3600
    assert pacific_window(november)[1] - november == 25 * 3600
    assert pacific_window(pacific_window(march)[1])[0] == "2026-03-09"


def test_project_and_model_scope_shared_after_restart(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    moment = ts(2026, 10, 17, 12)
    ledger = DailyQuotaLedger(QueueStore(path), "project-123", "gemini-x", 2)
    assert ledger.status(now=moment)["remaining"] == 2
    ledger.reserve(now=moment)
    DailyQuotaLedger(QueueStore(path), "project-123", "gemini-x", 2).reserve(now=moment)
    with pytest.raises(DailyQuotaExhausted) as error:
        ledger.reserve(now=moment)
    assert error.value.reset_at == pacific_window(moment)[1]
    assert ledger.status(now=moment)["used"] == 2
    assert DailyQuotaLedger(QueueStore(path), "project-123", "other-model", 2).status(now=moment)["remaining"] == 2
    assert DailyQuotaLedger(QueueStore(path), "other-project", "gemini-x", 2).status(now=moment)["remaining"] == 2
    assert ledger.status(now=error.value.reset_at)["remaining"] == 2


def test_atomic_21_call_cap_under_simultaneous_key_workers(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    one = DailyQuotaLedger(QueueStore(path), "same-project", "gemini-x", 21)
    two = DailyQuotaLedger(QueueStore(path), "same-project", "gemini-x", 21)
    moment = ts(2026, 10, 17, 12)

    def attempt(i):
        try:
            (one if i % 2 else two).reserve(now=moment)
            return True
        except DailyQuotaExhausted:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        outcome = list(executor.map(attempt, range(60)))
    assert sum(outcome) == 21
    assert one.status(now=moment)["used"] == 21


def test_external_quota_block_does_not_fake_local_usage(tmp_path):
    ledger = DailyQuotaLedger(QueueStore(tmp_path / "jobs.db"), "project-x", "model-1", 21)
    now = ts(2026, 10, 17, 12)
    assert ledger.block_day(now=now) == pacific_window(now)[1]
    state = ledger.status(now=now)
    assert state["used"] == 0 and state["remaining"] == 0 and state["externally_blocked"]
    with pytest.raises(DailyQuotaExhausted):
        ledger.reserve(now=now)
    assert ledger.status(now=state["reset_at"])["remaining"] == 21


def test_quota_requires_explicit_non_secret_project_and_positive_limit(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    for project, limit in [("", 21), ("unsafe secret with spaces", 21), ("valid", 0)]:
        with pytest.raises(ValueError):
            DailyQuotaLedger(store, project, "model", limit)


class CountingProvider:
    def __init__(self):
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return request.source_html_with_markers.replace("世界正在燃烧。", "Мир горит.")


def test_worker_pauses_at_quota_and_resumes_without_burning_retries(tmp_path, monkeypatch):
    clock = [ts(2026, 10, 17, 12)]
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    db = tmp_path / "jobs.sqlite3"
    store = QueueStore(db)
    provider = CountingProvider()
    ledger = DailyQuotaLedger(store, "project-pacific", "test-model", 1)
    guarded = QuotaControlledProvider(provider, ledger)
    worker = TranslationWorker(store, guarded)
    first = store.create_job("alice", JobSpec("a", "<p>世界正在燃烧。</p>", max_attempts=1))
    second = store.create_job("alice", JobSpec("b", "<p>世界正在燃烧。</p>", max_attempts=1))
    assert asyncio.run(worker.run_once())
    # RE-04 prioritizes smaller ready batches and breaks equal-time ties by
    # UUID. FIFO order between equal-priority jobs is not a quota invariant.
    states = {job["id"]:store.get_job(job["id"])["status"]
              for job in (first, second)}
    assert sorted(states.values()) == ["completed", "queued"]
    remaining_id = next(jid for jid, status in states.items() if status=="queued")
    assert asyncio.run(worker.run_once())
    state = store.get_job(remaining_id)
    assert state["status"] == "retry_wait"
    assert state["attempts"] == 0
    assert provider.calls == 1
    assert any(row["stage"] == "quota_wait" for row in store.events(remaining_id))
    assert store.claim_next() is None

    clock[0] = pacific_window(clock[0])[1] + 1
    assert DailyQuotaLedger(QueueStore(db), "project-pacific", "test-model", 1).status()["remaining"] == 1
    assert asyncio.run(worker.run_once())
    assert store.get_job(remaining_id)["status"] == "completed"
    assert provider.calls == 2


def test_remote_per_day_429_pauses_and_blocks_other_calls(tmp_path, monkeypatch):
    clock = [ts(2026, 10, 17, 12)]
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))

    class RemoteDaily:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise DailyProviderQuotaFailure("Daily quota")
            return request.source_html_with_markers.replace("世界正在燃烧。", "Мир горит.")

    store = QueueStore(tmp_path / "jobs.sqlite3")
    provider = RemoteDaily()
    ledger = DailyQuotaLedger(store, "project-a", "gemini-test", 21)
    worker = TranslationWorker(store, QuotaControlledProvider(provider, ledger))
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>"))
    assert asyncio.run(worker.run_once())
    assert store.get_job(job["id"])["status"] == "retry_wait"
    assert ledger.status()["used"] == 1
    assert ledger.status()["externally_blocked"]
    # Even before reaching 21, a provider-confirmed RPD exhausts the project.
    with pytest.raises(DailyQuotaExhausted):
        ledger.reserve()
    clock[0] = pacific_window(clock[0])[1] + 1
    assert asyncio.run(worker.run_once())
    assert store.get_job(job["id"])["status"] == "completed"


def test_repeated_partial_quota_restarts_eventually_fail_closed(tmp_path, monkeypatch):
    clock = [ts(2026, 10, 17, 12)]
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>"))
    for index in range(3):
        claimed, token = store.claim_next()
        assert store.defer_for_daily_quota(job["id"], token,
            reset_at=clock[0]+100, made_calls=True)
        state = store.get_job(job["id"])
        assert state["status"] == ("retry_wait" if index < 2 else "failed")
        clock[0] += 101
    assert store.claim_next() is None


def test_unconfigured_daily_429_suspends_instead_of_failing(tmp_path, monkeypatch):
    clock = [ts(2026, 10, 17, 12)]
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))
    # Worker uses its own clock for the remote RPD fallback.
    import chaptera_server.worker as worker_module
    monkeypatch.setattr(worker_module, "time", SimpleNamespace(time=lambda: clock[0]))

    class RemoteDaily:
        async def generate(self, _request):
            raise DailyProviderQuotaFailure("HTTP 429 daily")

    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>", max_attempts=1))
    assert asyncio.run(TranslationWorker(store, RemoteDaily()).run_once())
    state = store.get_job(job["id"])
    assert state["status"] == "retry_wait"
    assert state["attempts"] == 0
    assert store.claim_next() is None


def test_consecutive_external_daily_429s_do_not_consume_partial_job_budget(tmp_path, monkeypatch):
    clock = [ts(2026, 10, 17, 12)]
    monkeypatch.setattr(quota, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(storage, "time", SimpleNamespace(time=lambda: clock[0]))

    class AlwaysDaily:
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            raise DailyProviderQuotaFailure("Gemini 429 PerDay")

    store = QueueStore(tmp_path / "jobs.db")
    remote = AlwaysDaily()
    guarded = QuotaControlledProvider(
        remote, DailyQuotaLedger(store, "project-a", "gemini-test", 21),
    )
    worker = TranslationWorker(store, guarded)
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>", max_attempts=1))
    for day in range(4):
        assert asyncio.run(worker.run_once())
        state = store.get_job(job["id"])
        assert state["status"] == "retry_wait"
        assert state["attempts"] == 0
        assert guarded.completed_calls == 0
        assert remote.calls == day + 1
        clock[0] = pacific_window(clock[0])[1] + 1

