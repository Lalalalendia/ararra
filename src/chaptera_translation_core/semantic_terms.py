"""Source-evidenced semantic terms from the desktop Chaptera preflight.

Only parsing/normalization is used here; automatic promotion to durable
project glossary is deliberately NOT performed by the server worker.
"""
from __future__ import annotations

import math
import re
from typing import Any

_HANGUL_RE = re.compile(r'[\uac00-\ud7af]')
_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_ALLOWED_KINDS = {
    'person', 'alias', 'place', 'organization', 'event', 'technique', 'title',
    'item', 'rank', 'concept', 'other',
}
_KIND_ALIASES = {
    'character': 'person',
    'name': 'person',
    '人物': 'person',
    'location': 'place',
    'org': 'organization',
    'organisation': 'organization',
    'skill': 'technique',
    'ability': 'technique',
    'move': 'technique',
}

def _clean_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.75
    if math.isnan(number) or math.isinf(number):
        number = 0.75
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    return max(0.0, min(1.0, number))
def _raw_term_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ('semantic_terms', 'terms', 'glossary_candidates', 'candidates'):
            value = raw.get(key)
            if isinstance(value, list):
                return value
        # Compatibility with {"source": {"suggested_ru": ...}} maps.
        mapped = []
        for source, value in raw.items():
            if isinstance(value, dict):
                mapped.append({'source': source, **value})
            elif value is not None:
                mapped.append({'source': source, 'suggested_ru': str(value)})
        return mapped
    return []
def normalize_semantic_terms(raw: Any, *, source_text: str = '') -> list[dict]:
    """Normalize model observations without promoting them to permanent glossary state.
    Every accepted source key must be directly evidenced by the current chapter text when
    ``source_text`` is supplied. Suspicious Hangul/replacement-codepoint keys are preserved
    as observations for diagnostics but are explicitly excluded from session canonization.
    """
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    source_blob = str(source_text or '')
    for raw_item in _raw_term_list(raw):
        if not isinstance(raw_item, dict):
            continue
        source = str(
            raw_item.get('source')
            or raw_item.get('original')
            or raw_item.get('source_name')
            or raw_item.get('term')
            or ''
        ).strip()
        target = str(
            raw_item.get('suggested_ru')
            or raw_item.get('rus')
            or raw_item.get('translation')
            or raw_item.get('target')
            or ''
        ).strip()
        if not source or not target or len(source) > 120 or len(target) > 180:
            continue
        if _HANGUL_RE.search(target) or _CJK_RE.search(target) or '\ufffd' in target:
            # A proposed Russian binding must itself be clean target-language text.
            # Letting source-script codepoints leak into SESSION_CANONICAL would spread
            # one bad preflight observation across every parallel translation worker.
            continue
        if '\n' in source or '\r' in source or _CONTROL_RE.search(source):
            continue
        if source_blob and source not in source_blob:
            # Semantic preflight is evidence-backed. A model-invented source spelling must
            # never become a lexical contract for all parallel workers.
            continue
        kind = str(raw_item.get('kind') or raw_item.get('type') or 'other').strip().casefold()
        kind = _KIND_ALIASES.get(kind, kind)
        if kind not in _ALLOWED_KINDS:
            kind = 'other'
        aliases_raw = raw_item.get('aliases')
        if isinstance(aliases_raw, str):
            aliases_raw = [aliases_raw]
        aliases: list[str] = []
        for alias in aliases_raw if isinstance(aliases_raw, (list, tuple)) else ():
            clean = str(alias or '').strip()
            if not clean or clean == source or clean in aliases or len(clean) > 120:
                continue
            if source_blob and clean not in source_blob:
                continue
            aliases.append(clean)
        suspicious_source = bool(_HANGUL_RE.search(source) or '\ufffd' in source)
        note = str(raw_item.get('note') or raw_item.get('role') or '').strip()
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            'source': source,
            'suggested_ru': target,
            'kind': kind,
            'aliases': aliases,
            'note': note[:500],
            'confidence': _clean_confidence(raw_item.get('confidence')),
            'suspicious_source': suspicious_source,
        })
    return result
