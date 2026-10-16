"""Headless worker: durable claims, finite retry, fenced persistence and QA gates."""
from __future__ import annotations

import asyncio
import logging

from chaptera_translation_core import (
    ChapterInput, TranslationCore, TranslationCancelled, TranslationRejected,
)

from .provider import ProviderFailure, TransientProviderFailure
from .storage import QueueStore

logger = logging.getLogger(__name__)


class TranslationWorker:
    def __init__(self, store: QueueStore, provider, *, lease_seconds: float = 300.0,
                 heartbeat_seconds: float = 20.0):
        self.store = store
        self.provider = provider
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    async def _heartbeat(self, job_id: str, token: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
            except asyncio.TimeoutError:
                if not self.store.heartbeat(job_id, token, lease_seconds=self.lease_seconds):
                    return

    async def run_once(self) -> bool:
        claim = self.store.claim_next(lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        job, token = claim
        jid = job["id"]
        data = job["input"]
        stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat(jid, token, stop))
        core = TranslationCore(
            self.provider,
            is_cancelled=lambda: self.store.cancellation_requested(jid, token),
            on_event=lambda event: self.store.record_event(jid, token, event.stage, event.detail),
        )
        try:
            result = await core.translate(ChapterInput(
                chapter_id=data["chapter_id"],
                source_html=data["source_html"],
                source_language=data["source_language"],
                target_language=data["target_language"],
                glossary=data["glossary"],
                context=data["context"],
                chapter_path=data["chapter_path"],
            ))
            self.store.complete(jid, token, result.translated_html, result.source_hash, result.unit_count)
        except TranslationCancelled:
            self.store.fail(jid, token, "Translation cancelled")
            # The user cancellation flag results in 'cancelled' in _finish.
        except TransientProviderFailure as exc:
            retry_in = max(min(300.0, 2 ** min(job["attempts"], 8)), exc.retry_after_seconds or 0.0)
            self.store.retry(jid, token, str(exc), delay_seconds=retry_in)
        except (ProviderFailure, TranslationRejected) as exc:
            self.store.fail(jid, token, str(exc))
        except Exception as exc:
            logger.error("Unexpected job error in %s: %s", jid, type(exc).__name__)
            self.store.retry(jid, token, "Unexpected worker error", delay_seconds=15.0)
        finally:
            stop.set()
            await heartbeat_task
        return True

    async def run_forever(self, *, poll_seconds: float = 1.0, stop: asyncio.Event | None = None) -> None:
        shutdown = stop or asyncio.Event()
        while not shutdown.is_set():
            did_work = await self.run_once()
            if not did_work:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
