from __future__ import annotations

import asyncio

from chaptera_server.provider import TransientProviderFailure
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.worker import TranslationWorker


class EchoProvider:
    async def generate(self, request):
        return request.source_html_with_markers.replace("世界正在燃烧。", "Мир горит.")


def test_worker_persists_validated_result_with_events(tmp_path):
    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<body><p>世界正在燃烧。</p></body>"))
    assert asyncio.run(TranslationWorker(store, EchoProvider()).run_once())
    saved = store.get_job(job["id"])
    assert saved["status"] == "completed"
    assert saved["translated_html"] == "<body><p>Мир горит.</p></body>"
    assert [e["stage"] for e in store.events(job["id"])][-1] == "completed"


def test_worker_rejects_bad_output_without_persisting_it(tmp_path):
    class BadProvider:
        async def generate(self, request):
            return "<p>Я перевёл что-то</p>"

    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>"))
    assert asyncio.run(TranslationWorker(store, BadProvider()).run_once())
    saved = store.get_job(job["id"])
    assert saved["status"] == "failed"
    assert saved["translated_html"] is None


def test_transient_provider_error_schedules_retry(tmp_path):
    class BusyProvider:
        async def generate(self, request):
            raise TransientProviderFailure("HTTP 429", retry_after_seconds=35)

    store = QueueStore(tmp_path / "jobs.db")
    job = store.create_job("alice", JobSpec("1", "<p>世界正在燃烧。</p>"))
    assert asyncio.run(TranslationWorker(store, BusyProvider()).run_once())
    saved = store.get_job(job["id"])
    assert saved["status"] == "retry_wait"
    assert store.claim_next() is None
