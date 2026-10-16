"""Framework-independent Chaptera translation orchestration.

The source-unit, repair, and publication algorithms are the same canonical
implementations used by the existing Windows application. Provider/queue/storage
are injected and belong to their environment, not the translation kernel.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Callable, Iterable

from .ports import (
    ChapterInput, ChapterResult, ProviderRequest, TranslationProvider,
    TranslationEvent, TranslationEventHandler, TranslationCancelled,
    TranslationRejected,
)
from .prompt_policy import compose_prompt
from .core.worker_helpers.translation_completeness import (
    build_translation_unit_source_bundle,
    finalize_publication_html,
    strip_source_boilerplate,
    validate_chunk_completeness,
    validate_translation_completeness,
)


def build_translation_prompt(item: ChapterInput, marked_source: str) -> str:
    """Public compatibility helper for the server's deterministic HTML protocol."""
    return compose_prompt(item, marked_source, marked_source)[0]


class TranslationCore:
    """Async, GUI-free, storage-free translation core.

    A failed completeness check raises TranslationRejected; the caller must not
    commit partial or failed output. Reuse the existing Chaptera QC rules.
    """

    def __init__(self, provider: TranslationProvider, *, on_event: TranslationEventHandler | None = None,
                 is_cancelled: Callable[[], bool] | None = None):
        self.provider = provider
        self.on_event = on_event
        self.is_cancelled = is_cancelled

    def _emit(self, chapter_id: str, stage: str, detail: str = '') -> None:
        if self.on_event is not None:
            self.on_event(TranslationEvent(chapter_id, stage, detail))

    def _cancel_check(self) -> None:
        if self.is_cancelled is not None and self.is_cancelled():
            raise TranslationCancelled('Translation cancelled')

    async def translate(self, chapter: ChapterInput) -> ChapterResult:
        if not str(chapter.chapter_id).strip():
            raise ValueError('chapter_id is required')
        if not chapter.source_html.strip():
            raise TranslationRejected(chapter.chapter_id, 'input', 'source HTML is empty')
        self._cancel_check()
        self._emit(chapter.chapter_id, 'preparing')
        source = strip_source_boilerplate(
            chapter.source_html, current_chapter_path=chapter.chapter_path or None,
        )
        marked_source, units = build_translation_unit_source_bundle(source)
        if not units:
            raise TranslationRejected(chapter.chapter_id, 'input', 'no translatable source units')
        self._cancel_check()
        prompt, diagnostics = compose_prompt(chapter, marked_source, source)
        if diagnostics.context_status == 'stale_ignored':
            self._emit(chapter.chapter_id, 'context_stale', 'ignored out-of-date chapter context snapshot')
        if diagnostics.context_conflicts:
            self._emit(chapter.chapter_id, 'context_conflict', f'count={diagnostics.context_conflicts}')
        self._emit(chapter.chapter_id, 'provider_request', f'units={len(units)}')
        raw = await self.provider.generate(ProviderRequest(chapter.chapter_id, prompt, marked_source, len(units)))
        self._cancel_check()
        if not isinstance(raw, str) or not raw.strip():
            raise TranslationRejected(chapter.chapter_id, 'provider', 'empty response')
        self._emit(chapter.chapter_id, 'validating')
        ok, reason = validate_translation_completeness(marked_source, raw)
        if not ok:
            raise TranslationRejected(chapter.chapter_id, 'unit_completeness', reason)
        ok, reason = validate_chunk_completeness(marked_source, raw)
        if not ok:
            raise TranslationRejected(chapter.chapter_id, 'structure', reason)
        cleaned = finalize_publication_html(marked_source, raw, chapter.chapter_path or None)
        if not cleaned or not cleaned.strip():
            raise TranslationRejected(chapter.chapter_id, 'finalize', 'empty final HTML')
        self._cancel_check()
        self._emit(chapter.chapter_id, 'completed')
        return ChapterResult(
            chapter_id=chapter.chapter_id,
            source_hash=hashlib.sha256(chapter.source_html.encode('utf-8')).hexdigest(),
            translated_html=cleaned,
            unit_count=len(units),
        )

    async def translate_batch(self, chapters: Iterable[ChapterInput], *, concurrency: int = 1) -> list[ChapterResult]:
        """Per-chapter independent work with bounded parallelism; caller handles durable queue/retries."""
        if concurrency < 1:
            raise ValueError('concurrency must be >= 1')
        items = list(chapters)
        ids = [str(item.chapter_id) for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate chapter_id in batch')
        semaphore = asyncio.Semaphore(concurrency)

        async def work(item: ChapterInput) -> ChapterResult:
            async with semaphore:
                return await self.translate(item)

        # Fail fast and cancel siblings; persistence/retry policy stays with the caller.
        tasks = [asyncio.create_task(work(item)) for item in items]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
