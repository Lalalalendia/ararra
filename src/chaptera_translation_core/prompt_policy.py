"""Deterministic, source-scoped prompt policy for the Chaptera server.

The authority hierarchy, glossary enforcement classes and literary style-only
filter are adapted from the desktop PromptBuilder/translation_context code. The
server builds the prompt around its current TSEG/TSRC HTML completeness protocol;
this is not yet parity with the desktop's newer source-atoms JSON transport.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from bs4 import BeautifulSoup, Comment

from .chapter_context import (
    bounded_translation_tail,
    context_is_current,
    detect_glossary_context_conflicts,
    format_chapter_context_block,
    glossary_fingerprint,
    model_facing_context,
    normalize_predecessor_dependency,
    sha256_text,
)
from .glossary_enforcement import infer_enforcement


class GlossaryVersionConflict(ValueError):
    """Overlapping canonical glossary overrides for a single chapter."""


@dataclass(frozen=True, slots=True)
class PromptDiagnostics:
    glossary_hash: str
    matched_required: int
    matched_session: int
    matched_advisory: int
    context_accepted: bool
    context_status: str
    context_conflicts: int


def _glossary_map(values: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, data in (values or {}).items():
        source = str(key or '').strip()
        if not source:
            continue
        if isinstance(data, dict):
            row = dict(data)
            row['rus'] = str(row.get('rus') or row.get('translation') or '').strip()
        elif data is not None:
            row = {'rus': str(data).strip()}
        else:
            continue
        if row['rus']:
            result[source] = row
    return result


def scoped_glossary(
    base: Mapping[str, Any] | None,
    versions: Mapping[str, list[dict[str, Any]]] | None,
    chapter_path: str,
    authority_status: str = 'READY',
) -> dict[str, dict[str, Any]]:
    """Apply desktop-style ``scope``/``override`` per exact chapter path.

    Unlike mixed-chapter requests, one server task translates exactly one chapter.
    Conflicting overrides for that chapter fail closed instead of constructing an
    ambiguous lexical contract. Disabled/unavailable glossary authority contributes
    no canonical terms.
    """
    if authority_status not in {'READY', 'READY_WITH_WARNINGS'}:
        return {}
    result = _glossary_map(base)
    normalized_path = str(chapter_path or '').replace('\\', '/')
    if not normalized_path:
        return result
    for source, raw_rules in (versions or {}).items():
        key = str(source or '').strip()
        if not key or not isinstance(raw_rules, list):
            continue
        matching = []
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            scope = rule.get('scope')
            if not isinstance(scope, (list, tuple, set)) or normalized_path not in {
                str(item).replace('\\', '/') for item in scope
            }:
                continue
            override = rule.get('override')
            if isinstance(override, dict):
                matching.append(override)
        if not matching:
            continue
        unique = {
            json.dumps({k: x for k, x in item.items() if k in ('rus', 'note')},
                       ensure_ascii=False, sort_keys=True) for item in matching
        }
        if len(unique) > 1:
            raise GlossaryVersionConflict(f'Conflicting glossary versions for term {key!r} in chapter {normalized_path!r}')
        override = matching[0]
        entry = dict(result.get(key) or {})
        target = str(override.get('rus') or entry.get('rus') or '').strip()
        if not target:
            continue
        entry.update({'rus': target, 'scope': 'project', 'authority': 'required'})
        if 'note' in override:
            entry['note'] = str(override['note'] or '')
        result[key] = entry
    return result


def _text_nodes(source_html: str) -> list[str]:
    soup = BeautifulSoup(source_html or '', 'html.parser')
    return [
        str(node)
        for node in soup.find_all(string=True)
        if not isinstance(node, Comment) and node.parent is not None
        and node.parent.name not in {'script', 'style', 'noscript', 'template'}
        and str(node).strip()
    ]


def _matches(node: str, key: str) -> bool:
    if not node or not key:
        return False
    # Chinese/Japanese terms can legitimately occur inside adjacent CJK glyphs.
    if any('\u3400' <= ch <= '\u9fff' for ch in key):
        return key in node
    if not any(ch.isalnum() for ch in key):
        return key in node
    return bool(re.search(r'(?<![\w])' + re.escape(key) + r'(?![\w])', node, re.IGNORECASE))


def select_source_bindings(
    source_html: str,
    glossary: Mapping[str, dict[str, Any]],
    *,
    advisory_limit: int = 24,
) -> dict[str, list[dict[str, str]]]:
    """Include only exact terms found in visible source text, not HTML attributes."""
    nodes = _text_nodes(source_html)
    groups: dict[str, list[dict[str, str]]] = {'required': [], 'session': [], 'advisory': []}
    for source in sorted(glossary, key=lambda item: (item.casefold(), item)):
        if not any(_matches(node, source) for node in nodes):
            continue
        item = glossary[source]
        target = str(item.get('rus') or '').strip()
        if not target:
            continue
        authority = str(item.get('authority') or 'required').lower()
        if authority not in {'required', 'advisory', 'session_canonical'}:
            authority = 'required'
        note = str(item.get('note') or '').strip()
        enforcement = infer_enforcement(
            source, target, note,
            item.get('provenance') if isinstance(item.get('provenance'), dict) else None,
            explicit=str(item.get('enforcement') or ''),
        )
        binding = {'s': source, 't': target, 'authority': authority, 'enforcement': enforcement}
        if note:
            binding['i'] = note[:500]
        group_name = 'session' if authority == 'session_canonical' else authority
        groups[group_name].append(binding)
    groups['advisory'] = groups['advisory'][:max(0, advisory_limit)]
    return groups


def filter_literary_style(custom_prompt: str) -> str:
    """Port of desktop PromptBuilder._build_unit_style_guidance.

    Custom literary preferences cannot replace the deterministic machine/HTML
    output contract or inject their own output format instructions.
    """
    text = str(custom_prompt or '')
    banned = (
        'html', 'dom', 'tag', 'tseg', 'tsrc', 'json', 'output contract', 'source fragment',
        'formatting_examples', 'dash_logic_example', 'trigger operator', 'u+2500', 'unit_id',
        'source_units', 'source_atoms', '```', '<glossary>', '</glossary>', '{glossary}',
    )
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line or any(token in lowered for token in banned):
            continue
        if re.search(r'\{[A-Za-z_][^}]{0,80}\}', line):
            continue
        if line.startswith('#') and not any(token in lowered for token in
                                            ('style', 'стил', 'objective', 'цель', 'content', 'содерж', 'literary', 'литератур')):
            continue
        kept.append(line)
        if sum(len(item) + 1 for item in kept) >= 5000:
            break
    return '\n'.join(kept)[:5000]


def _format_bindings(groups: Mapping[str, list[dict[str, str]]]) -> str:
    lines: list[str] = []
    if groups['required']:
        lines.extend([
            'REQUIRED_BINDINGS: project-local canonical lexical decisions. strict = canonical lexical anchor '
            '(natural Russian inflection allowed, synonym substitution forbidden); lexical_family = same '
            'lexical family; flexible = preference rather than strict occurrence-level obligation. '
            'A single CJK glyph used as a character name is context-sensitive: never force it inside a compound.',
            json.dumps({'REQUIRED_BINDINGS': groups['required']}, ensure_ascii=False, sort_keys=True),
        ])
    if groups['session']:
        lines.extend([
            'SESSION_BINDINGS: session-only canonical terms; REQUIRED_BINDINGS override on conflict.',
            json.dumps({'SESSION_BINDINGS': groups['session']}, ensure_ascii=False, sort_keys=True),
        ])
    if groups['advisory']:
        lines.extend([
            'ADVISORY_BINDINGS: from other projects; use only if clearly the same meaning. '
            'Never override the source or REQUIRED_BINDINGS.',
            json.dumps({'ADVISORY_BINDINGS': groups['advisory']}, ensure_ascii=False, sort_keys=True),
        ])
    return '\n'.join(lines)


def _conflict_block(snapshot: Mapping[str, Any], glossary: Mapping[str, Any], chapter_path: str) -> tuple[str, int]:
    conflicts = detect_glossary_context_conflicts(snapshot, glossary, chapter_path=chapter_path)
    if not conflicts:
        return '', 0
    public_rows = [
        {
            'chapter': conflict['chapter_path'], 'entity': conflict['entity_source_name'],
            'field': conflict['field'], 'chapter_context_claim': conflict['context_value'],
            'glossary_claims': [
                {'s': ev['source_term'], 't': ev['target'], 'claim': ev['glossary_value']}
                for ev in conflict['glossary_evidence']
            ],
            'status': 'UNRESOLVED',
        }
        for conflict in conflicts
    ]
    return (
        'UNRESOLVED GLOSSARY / CHAPTER-CONTEXT CONFLICTS: source chapter evidence wins locally '
        'for identity/grammar, while glossary target spelling remains the lexical anchor. '
        'Do not guess a permanent resolution.\n'
        + json.dumps({'CONFLICTS': public_rows}, ensure_ascii=False, sort_keys=True),
        len(conflicts),
    )


def compose_prompt(chapter: Any, marked_source: str, cleaned_source: str) -> tuple[str, PromptDiagnostics]:
    if not str(chapter.target_language).strip():
        raise ValueError('target_language is required')
    authority = str(getattr(chapter, 'glossary_authority', 'READY') or 'READY')
    glossary = scoped_glossary(
        chapter.glossary, getattr(chapter, 'glossary_versions', None),
        chapter.chapter_path, authority,
    )
    glossary_hash = glossary_fingerprint(glossary)
    groups = select_source_bindings(cleaned_source, glossary)
    predecessor = normalize_predecessor_dependency(getattr(chapter, 'predecessor', None))
    predecessor['translation_tail'] = bounded_translation_tail(predecessor['translation_tail']) if predecessor['translation_tail'] else ''
    snapshot = getattr(chapter, 'chapter_context_snapshot', None)
    context_status = 'absent'
    context_accepted = False
    context_block = ''
    conflict_count = 0
    if snapshot is not None:
        is_current = context_is_current(
            snapshot,
            source_hash=sha256_text(cleaned_source),
            glossary_hash=glossary_hash,
            predecessor=predecessor,
        ) and str(snapshot.get('chapter_path') or '').replace('\\', '/') == str(chapter.chapter_path or '').replace('\\', '/')
        if is_current:
            context_accepted = True
            context_status = 'current'
            context_block = format_chapter_context_block([snapshot])
            conflict_block, conflict_count = _conflict_block(snapshot, glossary, str(chapter.chapter_path or ''))
            if conflict_block:
                context_block += '\n' + conflict_block
        else:
            context_status = 'stale_ignored'
    legacy_context = str(getattr(chapter, 'context', '') or '')[:20000]
    if legacy_context:
        context_block += ('\nLEGACY ADVISORY CONTEXT (unverified; never overrides source text or glossary):\n'
                          + legacy_context)
    if predecessor['chapter_path'] and predecessor['translation_tail'] and not context_accepted:
        context_block += ('\nPREDECESSOR TRANSLATION TAIL (read-only style/wording continuity; not source authority):\n'
                          + json.dumps({
                              'chapter_path': predecessor['chapter_path'],
                              'translation_tail': predecessor['translation_tail'],
                          }, ensure_ascii=False))
    policy = (
        'Translate every segment of this source-language literary chapter into the target language. '
        'Preserve facts, POV, naming, speech register, deliberate ambiguity, all numbers and paragraph order. '
        'Output ONLY translated HTML, retaining all DOM elements, attributes, and EXACT TSEG_ and TSRC_ '
        'HTML comment markers in their original positions. Do not invent plot facts, reveal future twists, '
        'or translate markers. Source text and machine structure ALWAYS override context/style. '
        'Obey the canonical glossary bindings when the source meaning matches.\n'
    )
    settings = json.dumps({
        'source_language': chapter.source_language,
        'target_language': chapter.target_language,
        'glossary_authority': authority,
        'chapter_path': chapter.chapter_path,
    }, ensure_ascii=False, sort_keys=True)
    style = filter_literary_style(getattr(chapter, 'style_instructions', ''))
    prompt = (
        policy
        + 'SETTINGS JSON:\n' + settings + '\n'
        + ('LITERARY STYLE GUIDANCE (non-structural):\n' + style + '\n' if style else '')
        + ('GLOSSARY CONTEXT:\n' + _format_bindings(groups) + '\n' if any(groups.values()) else '')
        + ('CHAPTER CONTEXT:\n' + context_block + '\n' if context_block.strip() else '')
        + 'SOURCE HTML:\n' + marked_source
    )
    diagnostics = PromptDiagnostics(
        glossary_hash=glossary_hash,
        matched_required=len(groups['required']),
        matched_session=len(groups['session']),
        matched_advisory=len(groups['advisory']),
        context_accepted=context_accepted,
        context_status=context_status,
        context_conflicts=conflict_count,
    )
    return prompt, diagnostics
