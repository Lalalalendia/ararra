"""Server-owned, source-complete chapter context preflight (schema v3 / prompt v6).

Long chapters are analyzed in contiguous windows. A snapshot is *never* emitted
unless every window has passed the same strict response/identity validation as a
short chapter; a missing/malformed window fails the whole operation closed.
"""
from __future__ import annotations

import json
import re
from html import escape
from collections import defaultdict
from typing import Callable

from bs4 import BeautifulSoup

from chaptera_translation_core.chapter_context import (
    build_chapter_context_prompt, make_snapshot, sha256_text, validate_context_response,
)
from chaptera_translation_core.prompt_policy import scoped_glossary, select_source_bindings
from chaptera_translation_core.ports import ProviderRequest, TranslationCancelled
from .library import ContextInputs


class ContextPrepassRejected(ValueError):
    """A source/window or model response cannot produce a complete trusted snapshot."""


class ContextPrepassUnserviceable(ContextPrepassRejected):
    """Hard source/context bounds prevent an expensive, impossible retry."""


# Bound every provider request, and reject chapters larger than the total cap.
# These limits apply to visible characters, not source HTML bytes or tokens.
CONTEXT_WINDOW_VISIBLE_CHARS = 12_000
MAX_CONTEXT_WINDOWS = 32
MAX_AGGREGATE_CONTEXT_CHARS = 24_000
_REQUIRED_SCALARS = ('summary', 'pov', 'tone_and_register')
_REQUIRED_LISTS = (
    'setting', 'active_entities', 'relationships', 'important_events',
    'continuity_in', 'continuity_out', 'ambiguous_references',
    'reveal_constraints', 'semantic_terms',
)
_LIST_CONTEXT_FIELDS = tuple(name for name in _REQUIRED_LISTS if name != 'semantic_terms')
ContextEvent = Callable[[str, str], None]


def _decode_context(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 100_000:
        raise ContextPrepassRejected('Context response must be nonempty bounded JSON')
    text = raw.strip()
    match = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text, flags=re.IGNORECASE)
    if match:
        text = match.group(1)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ContextPrepassRejected('Context response is not valid JSON') from exc
    if not isinstance(parsed, dict) or set(parsed) != {'c0'} or not isinstance(parsed['c0'], dict):
        raise ContextPrepassRejected('Context response must be a single object keyed by c0')
    row = parsed['c0']
    if any(not isinstance(row.get(name), str) for name in _REQUIRED_SCALARS):
        raise ContextPrepassRejected('Context response omitted a required string field')
    if any(not isinstance(row.get(name), list) for name in _REQUIRED_LISTS):
        raise ContextPrepassRejected('Context response omitted a required list field')
    if len(row['summary']) > 1_600 or any(len(row[field]) > 500 for field in ('pov', 'tone_and_register')):
        raise ContextPrepassRejected('Context scalar field exceeded its size limit')
    return parsed


def prepass_source_length(source_html: str) -> int:
    """Return visible text length for deciding and auditing context preflight."""
    return len(BeautifulSoup(source_html or '', 'html.parser').get_text(' ', strip=True))


def source_windows(source_text: str, *, cap: int = CONTEXT_WINDOW_VISIBLE_CHARS) -> list[tuple[int, int, str]]:
    """Split into complete, non-overlapping spans, preferring natural boundaries.

    The returned spans concatenate byte-for-byte (at Python string level) to
    the visible source. Window boundaries never introduce or remove glyphs.
    """
    if cap < 200:
        raise ValueError('Window cap must be >= 200 characters')
    if not source_text:
        raise ContextPrepassUnserviceable('Context source is empty')
    if len(source_text) > cap * MAX_CONTEXT_WINDOWS:
        raise ContextPrepassUnserviceable('Context source exceeds the complete-window safety limit')
    result: list[tuple[int, int, str]] = []
    start = 0
    while start < len(source_text):
        end = min(start + cap, len(source_text))
        if end < len(source_text):
            # Prefer a nearby sentence/paragraph boundary. Never take a cut
            # so early that a full-sized chapter creates excessive windows.
            earliest = start + (cap * 4 // 5)
            candidates = [source_text.rfind(mark, earliest, end) for mark in ('\n', '。', '！', '？', '.', '!', '?', ' ')]
            natural = max(candidates)
            if natural >= earliest:
                end = natural + 1
        if end <= start:
            raise ContextPrepassUnserviceable('Context window planner made no progress')
        result.append((start, end, source_text[start:end]))
        start = end
    if len(result) > MAX_CONTEXT_WINDOWS or ''.join(span for _, _, span in result) != source_text:
        raise ContextPrepassUnserviceable('Context windows did not cover the complete source')
    return result


def _ground_entities(context: dict, evidence: str) -> dict:
    """Drop ungrounded entity names and aliases, not necessarily the entire QA row."""
    clean = dict(context)
    entities: list[dict] = []
    for entity in clean.get('active_entities') or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get('source_name') or '').strip()
        if not name or name not in evidence:
            continue
        aliases = entity.get('aliases')
        safe_aliases = [a.strip() for a in aliases if isinstance(a, str) and a.strip() in evidence] \
            if isinstance(aliases, list) else []
        entities.append({**entity, 'source_name': name, 'aliases': safe_aliases})
    clean['active_entities'] = entities
    return clean


def _merge_windows(rows: list[dict], *, source_text: str) -> tuple[dict, list[dict], int]:
    """Merge only validated observations in source order; invent no global summary.

    Conflicting suggested Russian names remain suggestions. Conflicting values
    for one source term are excluded instead of silently picking a winner.
    """
    context: dict = {}
    for scalar in _REQUIRED_SCALARS:
        parts = [(i, str(row['context'].get(scalar) or '').strip())
                 for i, row in enumerate(rows, 1)]
        meaningful = [(i, value) for i, value in parts if value]
        context[scalar] = '\n'.join(f'[window {i}/{len(rows)}] {value}' for i, value in meaningful)
    for field in _LIST_CONTEXT_FIELDS:
        seen: set[str] = set()
        items: list = []
        for row in rows:
            for item in row['context'].get(field) or []:
                encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                if encoded not in seen:
                    seen.add(encoded)
                    items.append(item)
        context[field] = items

    terms_by_source: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        for item in row['semantic_terms']:
            key = str(item.get('source') or '')
            suggestion = str(item.get('suggested_ru') or '')
            if key and suggestion and key in source_text:
                terms_by_source[key].setdefault(suggestion, item)
    conflicting = sum(1 for suggestions in terms_by_source.values() if len(suggestions) > 1)
    semantic_terms = [next(iter(suggestions.values())) for _, suggestions in sorted(terms_by_source.items())
                      if len(suggestions) == 1]

    if len(json.dumps({'context': context, 'semantic_terms': semantic_terms}, ensure_ascii=False)) > MAX_AGGREGATE_CONTEXT_CHARS:
        raise ContextPrepassUnserviceable('Aggregated context exceeded bounded storage/prompt size')
    return context, semantic_terms, conflicting


async def generate_chapter_context(provider, inputs: ContextInputs, *, on_event: ContextEvent | None = None,
                                   is_cancelled: Callable[[], bool] | None = None) -> dict:
    source_text = BeautifulSoup(inputs.source_html, 'html.parser').get_text(' ', strip=True)
    windows = source_windows(source_text)
    glossary = scoped_glossary(inputs.glossary, inputs.glossary_versions,
                               inputs.chapter_path, inputs.glossary_authority)
    rows: list[dict] = []
    total = len(windows)
    audit_windows: list[dict] = []

    for index, (start, end, fragment) in enumerate(windows, start=1):
        if is_cancelled is not None and is_cancelled():
            raise TranslationCancelled('Cancelled before context window request')
        if on_event is not None:
            on_event('context_window', f'{index}/{total} visible_chars={len(fragment)}')
        doc = {
            'id': 'c0', 'chapter_path': inputs.chapter_path,
            'source_hash': sha256_text(fragment), 'text': fragment,
            'predecessor': inputs.predecessor if index == 1 else {},
        }
        anchor = inputs.predecessor_anchor if index == 1 else None
        # Select against this exact window only. Otherwise a glossary binding
        # appearing exclusively in a later window would leak future names.
        visible = select_source_bindings('<p>' + escape(fragment) + '</p>', glossary)
        prompt = build_chapter_context_prompt(
            [doc], glossary_context=json.dumps(visible, ensure_ascii=False),
            predecessor_anchor=anchor,
        )
        # Explicitly scope a partial document, so the model does not speculate
        # on an unseen earlier/later piece of the chapter.
        if total > 1:
            prompt = (
                f'This is CONTIGUOUS WINDOW {index} OF {total}, visible character offsets [{start},{end}). '
                'Extract only facts directly supported by the window shown. '
                'Do not infer a chapter-wide conclusion or unseen later reveals.\n'
                + prompt
            )
        def capture_usage(usage: dict[str, int], window: int = index) -> None:
            if on_event is not None:
                on_event('context_usage',
                         f'window={window}/{total} prompt_tokens={usage.get("promptTokenCount", 0)} '
                         f'output_tokens={usage.get("candidatesTokenCount", 0)} '
                         f'total_tokens={usage.get("totalTokenCount", 0)}')
        raw = await provider.generate(ProviderRequest(
            chapter_id=f'context:{inputs.book_id}:{inputs.chapter_id}:w{index}of{total}',
            prompt=prompt, source_html_with_markers='', unit_count=0,
            on_usage=capture_usage,
            request_role='glossary', is_cancelled=is_cancelled,
        ))
        if is_cancelled is not None and is_cancelled():
            raise TranslationCancelled('Cancelled during context window request')
        parsed = _decode_context(raw)
        valid, missing = validate_context_response(parsed, [doc])
        if missing or len(valid) != 1:
            raise ContextPrepassRejected(f'Context window {index}/{total} failed identity validation')
        entry = valid[0]
        # Only text in THIS window, or a genuine immediate predecessor for the
        # first window, can establish aliases and source-name identity.
        evidence = fragment
        if index == 1:
            evidence += ' ' + str(inputs.predecessor_anchor.get('source') or '')
        entry['context'] = _ground_entities(entry['context'], evidence)
        rows.append(entry)
        audit_windows.append({'index': index, 'start': start, 'end': end,
                              'source_hash': sha256_text(fragment)})
    context, semantic_terms, conflicting = _merge_windows(rows, source_text=source_text)
    if on_event is not None and conflicting:
        on_event('context_term_conflicts', f'conflicting_suggestions={conflicting}')
    if on_event is not None:
        on_event('context_windows_complete', f'count={total} visible_chars={len(source_text)}')
    return make_snapshot(
        chapter_path=inputs.chapter_path, source_hash=inputs.source_hash,
        glossary_hash=inputs.glossary_hash, predecessor=inputs.predecessor,
        context=context, semantic_terms=semantic_terms,
        window_coverage={'policy': 'windows_v1', 'visible_chars': len(source_text),
                         'windows': audit_windows},
    )
