"""Server-owned asynchronous chapter context preflight (schema v3/prompt v5).

Context is produced by a separate provider request *before* translation, cached
by immutable dependencies, and never promoted to the authoritative glossary.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from chaptera_translation_core.chapter_context import (
    build_chapter_context_prompt, make_snapshot, validate_context_response,
)
from chaptera_translation_core.prompt_policy import scoped_glossary, select_source_bindings
from chaptera_translation_core.ports import ProviderRequest
from .library import ContextInputs


class ContextPrepassRejected(ValueError):
    """Model context response was malformed or missing mandatory information."""


_REQUIRED_SCALARS = ('summary', 'pov', 'tone_and_register')
_REQUIRED_LISTS = (
    'setting', 'active_entities', 'relationships', 'important_events',
    'continuity_in', 'continuity_out', 'ambiguous_references',
    'reveal_constraints', 'semantic_terms',
)


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
    return parsed


async def generate_chapter_context(provider, inputs: ContextInputs) -> dict:
    source_text = BeautifulSoup(inputs.source_html, 'html.parser').get_text(' ', strip=True)
    if not source_text or len(source_text) > 50_000:
        raise ContextPrepassRejected('Context source is empty or exceeds 50000 visible characters')
    glossary = scoped_glossary(inputs.glossary, inputs.glossary_versions,
                               inputs.chapter_path, inputs.glossary_authority)
    # Send only visible source-matched glossary terms: no future-book disclosures.
    visible = select_source_bindings(inputs.source_html, glossary)
    doc = {
        'id': 'c0', 'chapter_path': inputs.chapter_path,
        'source_hash': inputs.source_hash, 'text': source_text,
        'predecessor': inputs.predecessor,
    }
    prompt = build_chapter_context_prompt(
        [doc], glossary_context=json.dumps(visible, ensure_ascii=False),
        predecessor_anchor=inputs.predecessor_anchor or None,
    )
    raw = await provider.generate(ProviderRequest(
        chapter_id=f'context:{inputs.book_id}:{inputs.chapter_id}',
        prompt=prompt, source_html_with_markers='', unit_count=0,
    ))
    payload = _decode_context(raw)
    valid, missing = validate_context_response(payload, [doc])
    if missing or len(valid) != 1:
        raise ContextPrepassRejected('Context response did not match the requested chapter')
    item = valid[0]
    # Entity names/aliases must be literal source-script evidence from the current
    # chapter or immediate predecessor, never a generated Russian explanation.
    # Unknown names are dropped, not elevated into a trusted continuity record.
    visible_evidence = source_text + ' ' + str(inputs.predecessor_anchor.get('source') or '')
    clean_context = dict(item['context'])
    entities = []
    for entity in clean_context.get('active_entities') or []:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get('source_name') or '').strip()
        if not name or name not in visible_evidence:
            continue
        aliases = entity.get('aliases')
        if isinstance(aliases, list):
            safe_aliases = [a for a in aliases if isinstance(a, str) and a.strip() and a.strip() in visible_evidence]
        else:
            safe_aliases = []
        entities.append({**entity, 'source_name': name, 'aliases': safe_aliases})
    clean_context['active_entities'] = entities
    return make_snapshot(
        chapter_path=inputs.chapter_path, source_hash=inputs.source_hash,
        glossary_hash=inputs.glossary_hash, predecessor=inputs.predecessor,
        context=clean_context, semantic_terms=item['semantic_terms'],
    )
