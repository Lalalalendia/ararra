"""The shutdown signal must stop new claims without losing an in-flight job."""
from __future__ import annotations

import asyncio
import signal

from chaptera_server.cli import _run_worker
from chaptera_server.config import Settings
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker


def test_shutdown_finishes_active_job_without_claiming_next(tmp_path):
    async def scenario():
        started = asyncio.Event()
        allow_completion = asyncio.Event()
        stop = asyncio.Event()

        class DelayedProvider:
            async def generate(self, request):
                started.set()
                await allow_completion.wait()
                return request.source_html_with_markers.replace(
                    "世界正在燃烧。", "Мир горит."
                )

        store = QueueStore(tmp_path / "jobs.sqlite3")
        first = store.create_job(
            "editor", JobSpec("chapter-1", "<body><p>世界正在燃烧。</p></body>")
        )
        second = store.create_job(
            "editor", JobSpec("chapter-2", "<body><p>世界正在燃烧。</p></body>")
        )
        worker = TranslationWorker(store, DelayedProvider())
        task = asyncio.create_task(worker.run_forever(poll_seconds=0.01, stop=stop))
        await asyncio.wait_for(started.wait(), timeout=2)
        stop.set()
        assert not task.done(), "SIGTERM should not abandon an active job"
        allow_completion.set()
        await asyncio.wait_for(task, timeout=2)
        assert store.get_job(first["id"])["status"] == "completed"
        assert store.get_job(second["id"])["status"] == "queued"

    asyncio.run(scenario())


def test_shutdown_while_idle_does_not_claim_a_job(tmp_path):
    async def scenario():
        store = QueueStore(tmp_path / "jobs.sqlite3")
        queued = store.create_job("editor", JobSpec("chapter-1", "<p>世界。</p>"))
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            TranslationWorker(store, object()).run_forever(
                poll_seconds=60, stop=stop
            ),
            timeout=1,
        )
        assert store.get_job(queued["id"])["status"] == "queued"

    asyncio.run(scenario())


def test_cli_registers_sigterm_and_sigint_for_cooperative_shutdown(tmp_path, monkeypatch):
    async def scenario():
        callbacks = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop, "add_signal_handler",
            lambda number, callback: callbacks.__setitem__(number, callback),
        )
        monkeypatch.setattr("chaptera_server.cli.QueueStore", lambda path: object())
        monkeypatch.setattr(
            "chaptera_server.cli.GeminiProvider",
            lambda api_key, model, **options: object(),
        )

        class ObservedWorker:
            def __init__(self, store, provider):
                pass

            async def run_forever(self, *, poll_seconds, stop):
                assert set(callbacks) == {signal.SIGTERM, signal.SIGINT}
                assert not stop.is_set()
                loop.call_soon(callbacks[signal.SIGTERM])
                await asyncio.wait_for(stop.wait(), timeout=1)

        monkeypatch.setattr("chaptera_server.cli.TranslationWorker", ObservedWorker)
        await _run_worker(
            Settings(db_path=tmp_path / "jobs.sqlite3", gemini_api_key="test-only")
        )

    asyncio.run(scenario())
