"""Headless worker: durable claims, finite retry, fenced persistence and QA gates."""
from __future__ import annotations

import asyncio
import logging

from chaptera_translation_core import (
    ChapterInput, TranslationCore, TranslationCancelled, TranslationRejected,
)

from .provider import ProviderFailure, TransientProviderFailure
from chaptera_translation_core.prompt_policy import GlossaryVersionConflict
from .storage import QueueStore
from .library import BookLibrary, LibraryNotFound, StaleBookJob
from .context_prepass import ContextPrepassRejected, ContextPrepassUnserviceable, generate_chapter_context

logger = logging.getLogger(__name__)


class TranslationWorker:
    def __init__(self, store: QueueStore, provider, *, lease_seconds: float = 300.0,
                 heartbeat_seconds: float = 20.0):
        self.store = store
        self.library = BookLibrary(store)
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
            managed = None
            snapshot = data.get('chapter_context_snapshot')
            predecessor = data.get('predecessor')
            if data.get('book_id'):
                managed = self.library.execution_inputs(data)
                snapshot = self.library.current_context(managed)
                predecessor = managed.predecessor
                if snapshot is None:
                    self.store.record_event(jid, token, 'context_preparing', 'server-owned windowed preflight')
                    snapshot = await generate_chapter_context(
                        self.provider, managed,
                        on_event=lambda stage, detail: self.store.record_event(jid, token, stage, detail),
                        is_cancelled=lambda: self.store.cancellation_requested(jid, token),
                    )
                    if self.store.cancellation_requested(jid, token):
                        raise TranslationCancelled('Translation cancelled during context preflight')
                    self.library.save_context(managed, snapshot)
                    self.store.record_event(jid, token, 'context_generated', 'complete snapshot persisted')
                else:
                    self.store.record_event(jid, token, 'context_cache_hit', 'fresh snapshot reused')
                # A concurrent book edit cannot slip between prepass and translation.
                if self.library.execution_inputs(data).guard() != managed.guard():
                    raise StaleBookJob('Book changed during context preflight')
            result = await core.translate(ChapterInput(
                chapter_id=data["chapter_id"],
                source_html=data["source_html"],
                source_language=data["source_language"],
                target_language=data["target_language"],
                glossary=data["glossary"],
                context=data["context"],
                chapter_path=data["chapter_path"],
                glossary_versions=data.get("glossary_versions") or {},
                glossary_authority=data.get("glossary_authority", "READY"),
                chapter_context_snapshot=snapshot,
                predecessor=predecessor,
                style_instructions=data.get("style_instructions", ""),
                transport=data.get("transport", "html_v0"),
            ))
            if managed is not None:
                self.library.complete_book_job(
                    jid, token, result.translated_html, result.source_hash, result.unit_count, managed,
                    result.atom_provenance,
                )
            else:
                self.store.complete(jid, token, result.translated_html, result.source_hash,
                                    result.unit_count, result.atom_provenance)
        except TranslationCancelled:
            self.store.fail(jid, token, "Translation cancelled")
            # The user cancellation flag results in 'cancelled' in _finish.
        except TransientProviderFailure as exc:
            retry_in = max(min(300.0, 2 ** min(job["attempts"], 8)), exc.retry_after_seconds or 0.0)
            self.store.retry(jid, token, str(exc), delay_seconds=retry_in)
        except ContextPrepassUnserviceable as exc:
            self.store.record_event(jid, token, 'context_unserviceable', str(exc))
            self.store.fail(jid, token, str(exc))
        except ContextPrepassRejected as exc:
            self.store.retry(jid, token, str(exc), delay_seconds=3.0)
        except (ProviderFailure, TranslationRejected, GlossaryVersionConflict, StaleBookJob, LibraryNotFound) as exc:
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
