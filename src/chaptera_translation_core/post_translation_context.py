"""Server adapter for the proven desktop post-translation terminology gates.

The desktop matcher, ORM/Qt session and project history are NOT server
dependencies. Exact source spans, per-chapter scope and immutable glossary
authority are reconstructed from the existing prompt-policy inputs. This
adapter does not guess alignment from matching HTML node counts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .core.prepared_chapter import prepare_source_chapter
from .glossary_enforcement import infer_enforcement
from .prompt_policy import scoped_glossary, select_source_bindings


def _find_occurrences(text: str, source: str):
    """Return lexical occurrences, allowing CJK inside adjacent Han glyphs."""
    if not text or not source:
        return
    is_cjk = any('\u3400' <= ch <= '\u9fff' for ch in source)
    expression = (re.escape(source) if is_cjk
                  else r'(?<![\w])' + re.escape(source) + r'(?![\w])')
    for found in re.finditer(expression, text, flags=re.IGNORECASE):
        yield found.start(), found.end()


class ExactSourceSpanMatcher:
    """Longest non-overlapping source key wins, across required and advisory.

    A source phrase such as 灰燼教團 must not assert a separate 教團 term.
    The matcher returns the desktop's node/start/end/term tuple contract.
    """

    def __init__(self, glossary: Mapping[str, Any]):
        self.sources = tuple(sorted(
            (str(k) for k, row in glossary.items()
             if str(k).strip() and isinstance(row, dict) and row.get('rus')),
            key=lambda term: (-len(term), term.casefold(), term),
        ))

    def find_exact_matches_with_spans_in_nodes(self, nodes):
        matches = []
        for index, node in enumerate(nodes):
            candidates = []
            for source in self.sources:
                for begin, end in _find_occurrences(str(node), source):
                    candidates.append((begin, end, source))
            # Longest source wins even when the substring is located first.
            candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
            occupied = []
            selected = []
            for begin, end, source in candidates:
                if any(begin < occupied_end and end > occupied_start
                       for occupied_start, occupied_end in occupied):
                    continue
                occupied.append((begin, end))
                selected.append((index, begin, end, source))
            matches.extend(sorted(selected, key=lambda item: (item[0], item[1], item[2])))
        return matches


@dataclass
class ServerGateContext:
    """Chapter-local canonical rows, no API keys or desktop state."""

    glossary: Mapping[str, Any]
    regex_service: ExactSourceSpanMatcher = field(init=False)

    def __post_init__(self):
        self.regex_service = ExactSourceSpanMatcher(self.glossary)


def build_gate_context(chapter) -> ServerGateContext:
    """Use exactly the chapter's scoped glossary as during prompt creation."""
    return ServerGateContext(scoped_glossary(
        chapter.glossary, chapter.glossary_versions,
        chapter.chapter_path, chapter.glossary_authority,
    ))


def _to_binding(original: str, row: dict) -> dict:
    provenance = row.get('provenance') if isinstance(row.get('provenance'), dict) else {}
    target = str(row.get('rus') or '')
    note = str(row.get('note') or '')
    return {
        's': original, 't': target, 'i': note[:500],
        'authority': str(row.get('authority') or 'required'),
        'provenance': provenance,
        'enforcement': infer_enforcement(
            original, target, note, provenance,
            explicit=str(row.get('enforcement') or ''),
        ),
    }


def compile_translation_context(
    context_manager: ServerGateContext,
    source_html: str,
    current_chapters_list=None,
) -> dict:
    """Adapt one-chapter source bindings to desktop gate input.

    Catalog must include *other* required named people to detect explicit
    substitution. Only longest exact source matches produce obligations.
    Advisory terms never create hard findings. Session canonical terms are
    treated as chapter-local but cannot override a project-required decision.
    """
    glossary = context_manager.glossary
    source_nodes = prepare_source_chapter(source_html).visible_text_nodes
    exact = context_manager.regex_service.find_exact_matches_with_spans_in_nodes(source_nodes)
    matched = {term for _, _, _, term in exact}
    selected = select_source_bindings(source_html, glossary, advisory_limit=10_000)
    def normalize(groups):
        result = []
        for binding in groups:
            source = binding['s']
            if source in matched:
                result.append(_to_binding(source, glossary[source]))
        return result

    required = normalize(selected['required'] + selected['session'])
    advisory = normalize(selected['advisory'])[:24]
    catalog = []
    for source, item in glossary.items():
        if not isinstance(item, dict):
            continue
        binding = _to_binding(source, item)
        if binding['authority'] == 'advisory':
            continue
        note = binding['i'].casefold()
        kind = str(binding['provenance'].get('semantic_kind') or '').casefold()
        if (kind in {'person', 'alias'} or
                any(word in note for word in
                    ('персонаж', 'псевдоним', 'имя;', 'божество', 'type=person', 'type=alias'))):
            catalog.append(binding)
    return {
        'required': required,
        'advisory': advisory,
        'entity_catalog': catalog,
    }
