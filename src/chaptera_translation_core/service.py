"""Framework-independent Chaptera translation orchestration.

The source-unit, repair, and publication algorithms are the same canonical
implementations used by the existing Windows application. Provider/queue/storage
are injected and belong to their environment, not the translation kernel.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Callable, Iterable

from .ports import (
    ChapterInput, ChapterResult, ProviderRequest, TranslationProvider,
    TranslationEvent, TranslationEventHandler, TranslationCancelled,
    TranslationRejected, AtomCheckpoint,
)
from .prompt_policy import compose_prompt
from .atom_transport import (
    MAX_REPAIR_ROUNDS, MAX_REPAIR_DOMAINS_PER_ROUND,
    prepare_source_atoms, packets_for_atoms, parse_packet_response,
    merge_proven_segments, repair_domains, apply_proven_repair, atom_provenance,
    atom_checkpoint_key, salvage_partial_atom_response,
)
from .api.errors import PartialGenerationError, TranslationDebtDeferredError
from .core.translation_units import build_render_plan, expected_render_domain_covers
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
                 is_cancelled: Callable[[], bool] | None = None,
                 atom_checkpoint: AtomCheckpoint | None = None):
        self.provider = provider
        self.on_event = on_event
        self.is_cancelled = is_cancelled
        self.atom_checkpoint = atom_checkpoint

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
        if chapter.transport not in {'html_v0', 'atoms_v1'}:
            raise ValueError('Unknown translation transport')
        self._cancel_check()
        self._emit(chapter.chapter_id, 'preparing')
        source = strip_source_boilerplate(
            chapter.source_html, current_chapter_path=chapter.chapter_path or None,
        )
        marked_source, units = build_translation_unit_source_bundle(source)
        if not units:
            raise TranslationRejected(chapter.chapter_id, 'input', 'no translatable source units')
        self._cancel_check()
        if chapter.transport == 'atoms_v1':
            return await self._translate_atoms(chapter, source, marked_source, units)
        prompt, diagnostics = compose_prompt(chapter, marked_source, source)
        if diagnostics.context_status == 'stale_ignored':
            self._emit(chapter.chapter_id, 'context_stale', 'ignored out-of-date chapter context snapshot')
        if diagnostics.context_conflicts:
            self._emit(chapter.chapter_id, 'context_conflict', f'count={diagnostics.context_conflicts}')
        self._emit(chapter.chapter_id, 'provider_request', f'units={len(units)}')
        raw = await self.provider.generate(ProviderRequest(
            chapter.chapter_id, prompt, marked_source, len(units), 'html_v0',
            lambda usage: self._emit(chapter.chapter_id, 'gemini_usage',
                f'prompt_tokens={usage.get("promptTokenCount", 0)} '
                f'output_tokens={usage.get("candidatesTokenCount", 0)} '
                f'total_tokens={usage.get("totalTokenCount", 0)}'),
            request_role='translation', is_cancelled=self.is_cancelled,
        ))
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

    async def _proven_atom_request(self, chapter_id: str, prompt: str,
                                  payload: str, atoms: tuple, role: str,
                                  on_usage, *, allow_provider: bool = True,
                                  defer_missing: bool = False):
        """Reuse exact source-owned atom requests after re-validating their reply.

        Desktop reconciliation and repair algorithms remain the authority;
        the server's SQLite port only caches a proved raw response.
        """
        key = atom_checkpoint_key(prompt, payload, role)
        cached = self.atom_checkpoint.load(key) if self.atom_checkpoint else None
        reused = cached is not None
        if reused:
            self._emit(chapter_id, 'atom_checkpoint_hit', f'role={role}')
            raw = cached
        else:
            if not allow_provider:
                if defer_missing:
                    raise TranslationDebtDeferredError(
                        "Bounded source-owned repair wave deferred; resume from durable SQLite proofs",
                        leaf_state={"mode": "debt_repair"},
                    )
                raise TranslationRejected(
                    chapter_id, 'finalize_only',
                    'Source-owned checkpoint missing or changed; Gemini generation forbidden',
                )
            try:
                raw = await self.provider.generate(ProviderRequest(
                    chapter_id, prompt, payload, len(atoms), 'atoms_v1',
                    on_usage, request_role=role, is_cancelled=self.is_cancelled,
                ))
            except PartialGenerationError as exc:
                if str(exc.reason).upper() not in {
                    'MAX_TOKENS', 'NETWORK_ERROR', 'TRANSPORT_STALLED',
                    'TRANSPORT_TRUNCATED', 'STREAM_TRUNCATED', 'CONNECTION_RESET',
                }:
                    raise
                try:
                    raw = salvage_partial_atom_response(exc.partial_text, atoms)
                except ValueError as invalid:
                    # Unknown/overlapping source identity is a deterministic
                    # QA violation, never a reason to publish or repair blindly.
                    raise TranslationRejected(chapter_id, 'atom_partial_identity',
                                              str(invalid)) from invalid
                if raw is None:
                    # No complete source-owned segments. Preserve the original
                    # transport failure for desktop ErrorAnalyzer retry policy.
                    raise
                self._emit(chapter_id, 'atom_partial_recovered',
                           f'role={role} reason={str(exc.reason).upper()}')
        self._cancel_check()
        try:
            result = parse_packet_response(raw, atoms)
        except ValueError as exc:
            raise TranslationRejected(
                chapter_id, 'atom_checkpoint' if reused else 'atom_identity', str(exc),
            ) from exc
        # Partial but proven source segments can be repaired by the existing
        # debt loop. Store only packets with at least one valid segment.
        if self.atom_checkpoint and not reused and result.segments:
            self.atom_checkpoint.save(key, raw)
            self._emit(chapter_id, 'atom_checkpoint_saved', f'role={role}')
        return result, not reused

    async def _translate_atoms(self, chapter: ChapterInput, source: str, marked_source: str,
                               units: list[dict]) -> ChapterResult:
        # Source owned identity and DOM are prepared ONCE; the provider only supplies text.
        # TSEG service-looking user text cannot be roundtripped by the publication stripper.
        if re.search(r'(?i)\b(?:TSEG_\d{1,12}|TSRC_[0-9a-f]{6,64})\b', source):
            raise TranslationRejected(chapter.chapter_id, 'input', 'reserved marker-like source content')
        atoms = prepare_source_atoms(units, chapter_id=chapter.chapter_id)
        batches = packets_for_atoms(atoms)
        if not batches:
            raise TranslationRejected(chapter.chapter_id, 'input', 'no source atoms')
        self._emit(chapter.chapter_id, 'atom_prepared', f'atoms={len(atoms)} batches={len(batches)}')
        source_digest = hashlib.sha256(chapter.source_html.encode('utf-8')).hexdigest()
        phase_reader = getattr(self.atom_checkpoint, 'phase', None)
        phase_writer = getattr(self.atom_checkpoint, 'mark_phase', None)
        durable_recovery = callable(phase_reader) and callable(phase_writer)
        finalize_only = bool(durable_recovery and phase_reader(source_digest) == 'finalize_only')
        if finalize_only:
            self._emit(chapter.chapter_id, 'atom_finalize_only_resume',
                       'local proof revalidation only; provider admission forbidden')
        accepted = []
        for index, batch in enumerate(batches, start=1):
            self._cancel_check()
            prompt, diagnostics = compose_prompt(chapter, marked_source, source, atom_payload=batch.payload)
            if index == 1:
                if diagnostics.context_status == 'stale_ignored':
                    self._emit(chapter.chapter_id, 'context_stale', 'ignored out-of-date chapter context snapshot')
                if diagnostics.context_conflicts:
                    self._emit(chapter.chapter_id, 'context_conflict', f'count={diagnostics.context_conflicts}')
            self._emit(chapter.chapter_id, 'atom_batch',
                       f'{index}/{len(batches)} atoms={len(batch.atoms)} prompt_chars={len(prompt)} payload_chars={len(batch.payload)}')
            part, _ = await self._proven_atom_request(
                chapter.chapter_id, prompt, batch.payload, batch.atoms,
                'translation',
                lambda usage, number=index: self._emit(chapter.chapter_id, 'gemini_usage',
                    f'batch={number} prompt_tokens={usage.get("promptTokenCount", 0)} '
                    f'output_tokens={usage.get("candidatesTokenCount", 0)} '
                    f'total_tokens={usage.get("totalTokenCount", 0)}'),
                allow_provider=not finalize_only,
            )
            accepted.append(part)
        try:
            coverage = merge_proven_segments(atoms, accepted)
        except ValueError as exc:
            raise TranslationRejected(chapter.chapter_id, 'atom_identity', str(exc)) from exc
        # The desktop debt-parent handoff limits new paid *repairs* per claim,
        # not the number of source-owned domains in the chapter. Already saved
        # repairs are replayed locally without consuming this wave budget.
        repair_calls_this_claim = 0
        for round_index in range(MAX_REPAIR_ROUNDS + 1):
            self._cancel_check()
            domains = repair_domains(coverage, marked_source)
            if not domains:
                break
            if finalize_only:
                raise TranslationRejected(
                    chapter.chapter_id, 'finalize_only',
                    'Previously complete proof is now missing source-owned domains',
                )
            if round_index == MAX_REPAIR_ROUNDS:
                break
            if not durable_recovery and len(domains) > MAX_REPAIR_DOMAINS_PER_ROUND:
                # No persistent job/lease evidence: preserve the original
                # bounded fail-closed behavior for standalone core consumers.
                raise TranslationRejected(chapter.chapter_id, 'atom_repair',
                                          f'too many damaged domains: {len(domains)}')
            if durable_recovery:
                phase_writer(source_digest, 'debt_repair', remaining_debts=len(domains))
            self._emit(chapter.chapter_id, 'atom_repair',
                       f'round={round_index + 1} domains={len(domains)}')
            for domain in domains:
                self._cancel_check()
                payload = packets_for_atoms(domain)[0].payload
                prompt, _ = compose_prompt(chapter, marked_source, source,
                                           atom_payload=payload, repair=True)
                part, fetched = await self._proven_atom_request(
                    chapter.chapter_id, prompt, payload, domain, 'targeted_repair',
                    lambda usage: self._emit(chapter.chapter_id, 'gemini_repair_usage',
                        f'prompt_tokens={usage.get("promptTokenCount", 0)} '
                        f'output_tokens={usage.get("candidatesTokenCount", 0)} '
                        f'total_tokens={usage.get("totalTokenCount", 0)}'),
                    allow_provider=(
                        not durable_recovery or
                        repair_calls_this_claim < MAX_REPAIR_DOMAINS_PER_ROUND
                    ),
                    defer_missing=durable_recovery,
                )
                if fetched:
                    repair_calls_this_claim += 1
                coverage = apply_proven_repair(coverage, part)
        self._emit(chapter.chapter_id, 'validating')
        plan = build_render_plan(marked_source, coverage)
        expected = expected_render_domain_covers(atoms)
        actual = [tuple(seg.covers) for seg in coverage.segments]
        if coverage.missing_atom_ids or plan.debts or plan.rendered_html is None or actual != expected:
            raise TranslationRejected(chapter.chapter_id, 'atom_coverage',
                                      f'missing={len(coverage.missing_atom_ids)} debts={len(plan.debts)}')
        # This is the desktop finalize_only boundary: the complete source
        # proof is already in durable checkpoints. A crash here must resume
        # with ZERO new Gemini requests, regardless of quota or error history.
        if durable_recovery:
            phase_writer(source_digest, 'finalize_only')
            self._emit(chapter.chapter_id, 'atom_finalize_only',
                       'complete source proof; local HTML and provenance checks only')
        cleaned = finalize_publication_html(marked_source, plan.rendered_html, chapter.chapter_path or None)
        ok, reason = validate_chunk_completeness(source, cleaned)
        if not ok:
            raise TranslationRejected(chapter.chapter_id, 'structure', reason)
        if not cleaned.strip():
            raise TranslationRejected(chapter.chapter_id, 'finalize', 'empty final HTML')
        proof = atom_provenance(coverage)
        if len(proof) != len(atoms):
            raise TranslationRejected(chapter.chapter_id, 'atom_provenance', 'incomplete proof')
        self._cancel_check()
        self._emit(chapter.chapter_id, 'completed')
        return ChapterResult(
            chapter_id=chapter.chapter_id,
            source_hash=hashlib.sha256(chapter.source_html.encode('utf-8')).hexdigest(),
            translated_html=cleaned,
            unit_count=len(atoms),
            atom_provenance=tuple(proof),
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
