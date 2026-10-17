"""Headless worker: durable claims, finite retry, fenced persistence and QA gates."""
from __future__ import annotations

import asyncio
import logging
import time

from chaptera_translation_core import (
    ChapterInput, TranslationCore, TranslationCancelled, TranslationRejected,
)

from .provider import ProviderFailure, TransientProviderFailure, DailyProviderQuotaFailure
from .gemini_error_bridge import (
    NON_COUNTABLE_TRANSIENT, FATAL_WORKER_CONFIG,
    desktop_error_type, desktop_transient_delay,
)
from chaptera_translation_core.api.errors import ErrorType
from .quota import DailyQuotaExhausted, pacific_window
from .atom_checkpoints import SQLiteAtomCheckpoint, AtomCheckpointConflict
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
        self._fatal_provider_error: str | None = None

    def _model_identity(self) -> str:
        # Resolve only the non-secret model name through the RPD/scheduler
        # decorators; the Gemini credential is never a cache key.
        provider = self.provider
        for _ in range(4):
            model = getattr(provider, "model", "")
            if model:
                return str(model)
            provider = getattr(provider, "provider", None)
            if provider is None:
                break
        return "unconfigured"

    def _uncheckpointed_completed_work(self, calls_before: int,
                                      checkpoint: SQLiteAtomCheckpoint | None,
                                      context_durable_calls: int) -> bool:
        """Only undurable successful responses count as expensive replay risk.

        Every packet saved by the existing desktop CoverageMap gate and every
        *fully persisted* context preflight remains reusable next day.
        Unvalidated 200 replies and incomplete context windows remain at risk.
        """
        completed = max(0, int(getattr(self.provider, "completed_calls", calls_before)) -
                        int(calls_before))
        durable_packets = checkpoint.saved_this_run if checkpoint is not None else 0
        return completed > durable_packets + context_durable_calls

    async def _heartbeat(self, job_id: str, token: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.heartbeat_seconds)
            except asyncio.TimeoutError:
                if not self.store.heartbeat(job_id, token, lease_seconds=self.lease_seconds):
                    return

    async def run_once(self) -> bool:
        # Desktop ErrorAnalyzer aborts the worker for a broken shared key/model
        # or request configuration. One project/key: do not burn other queued jobs.
        if self._fatal_provider_error is not None:
            return False
        claim = self.store.claim_next(lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        job, token = claim
        jid = job["id"]
        data = job["input"]
        calls_before = getattr(self.provider, "completed_calls", 0)
        context_durable_calls = 0
        stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat(jid, token, stop))
        checkpoint = (SQLiteAtomCheckpoint(self.store, jid, token,
                                           model_id=self._model_identity())
                      if data.get("transport") == "atoms_v1" else None)
        core = TranslationCore(
            self.provider,
            is_cancelled=lambda: self.store.cancellation_requested(jid, token),
            on_event=lambda event: self.store.record_event(jid, token, event.stage, event.detail),
            atom_checkpoint=checkpoint,
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
                    context_calls_before = getattr(self.provider, "completed_calls", 0)
                    snapshot = await generate_chapter_context(
                        self.provider, managed,
                        on_event=lambda stage, detail: self.store.record_event(jid, token, stage, detail),
                        is_cancelled=lambda: self.store.cancellation_requested(jid, token),
                    )
                    if self.store.cancellation_requested(jid, token):
                        raise TranslationCancelled('Translation cancelled during context preflight')
                    self.library.save_context(managed, snapshot)
                    context_durable_calls = max(
                        0, getattr(self.provider, "completed_calls", 0) - context_calls_before,
                    )
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
        except DailyQuotaExhausted as exc:
            # A quota pause is NOT one of the finite transient/provider retries.
            # If the job already sent multiple requests, avoid an unbounded
            # paid replay every day without durable sub-request checkpoints.
            partial_work = self._uncheckpointed_completed_work(
                calls_before, checkpoint, context_durable_calls,
            )
            self.store.defer_for_daily_quota(
                jid, token, reset_at=exc.reset_at, made_calls=partial_work,
            )
        except DailyProviderQuotaFailure:
            # Even without an opt-in local limit, Google's explicitly
            # classified PerDay response must not consume finite retries.
            # A configured QuotaControlledProvider additionally blocks other
            # attempts for the same project/model until reset.
            reset = pacific_window(time.time())[1]
            partial_work = self._uncheckpointed_completed_work(
                calls_before, checkpoint, context_durable_calls,
            )
            self.store.defer_for_daily_quota(
                jid, token, reset_at=reset, made_calls=partial_work,
            )
        except TransientProviderFailure as exc:
            kind = desktop_error_type(exc)
            if kind in NON_COUNTABLE_TRANSIENT:
                self.store.defer_desktop_transient(
                    jid, token, kind=kind.name,
                    initial_delay_seconds=desktop_transient_delay(exc, kind),
                )
            elif kind == ErrorType.API_ERROR and job["attempts"] >= min(job["max_attempts"], 2):
                # Desktop FAILURE_RULES.API_ERROR allows at most two countable
                # tries, regardless of a larger generic queue max_attempts.
                self.store.fail(jid, token, str(exc))
            else:
                # Unknown HTTP outcome or malformed successful response: finite
                # API_ERROR path as in desktop ErrorAnalyzer, never infinite retry.
                self.store.retry(jid, token, str(exc), delay_seconds=3.0)
        except ProviderFailure as exc:
            kind = desktop_error_type(exc)
            if kind in FATAL_WORKER_CONFIG:
                # Desktop ABORT_WORKER for shared credential/model/request config.
                self.store.record_event(jid, token, "provider_config_blocked",
                                        f"kind={kind.name}")
                self.store.fail(jid, token, str(exc))
                self._fatal_provider_error = kind.name
            elif kind == ErrorType.API_ERROR and job["attempts"] < min(job["max_attempts"], 2):
                self.store.retry(jid, token, str(exc), delay_seconds=3.0)
            else:
                self.store.fail(jid, token, str(exc))
        except ContextPrepassUnserviceable as exc:
            self.store.record_event(jid, token, 'context_unserviceable', str(exc))
            self.store.fail(jid, token, str(exc))
        except ContextPrepassRejected as exc:
            self.store.retry(jid, token, str(exc), delay_seconds=3.0)
        except (TranslationRejected, GlossaryVersionConflict,
                StaleBookJob, LibraryNotFound, AtomCheckpointConflict) as exc:
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
            if self._fatal_provider_error is not None:
                logger.error("Gemini worker halted: %s", self._fatal_provider_error)
                return
            did_work = await self.run_once()
            if not did_work:
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
