from __future__ import annotations
import hashlib
import json
import re
import time
from typing import Any

# Adapted from the desktop chapter_context utilities (schema v3/prompt v5).
# Server policy is self-contained: no desktop configuration or GUI imports.
CHAPTER_CONTEXT_SCHEMA_VERSION = 3
CHAPTER_CONTEXT_PROMPT_VERSION = 5
PREDECESSOR_TRANSLATION_TAIL_CHARS = 2400
_MODEL_FIELDS = (
    'summary',
    'pov',
    'tone_and_register',
    'setting',
    'active_entities',
    'relationships',
    'important_events',
    'continuity_in',
    'continuity_out',
    'ambiguous_references',
    'reveal_constraints',
)
_LIST_FIELDS = {
    'setting',
    'active_entities',
    'relationships',
    'important_events',
    'continuity_in',
    'continuity_out',
    'ambiguous_references',
    'reveal_constraints',
}
_FEMALE_GENDER_RE = re.compile(
    r'(?:\bженщин(?:а|ы|е|у|ой)?\b|\bженск(?:ий|ая|ое|ие)\b|\bженский\s+пол\b|\bfemale\b|\bсамк(?:а|и|е|у|ой)?\b|她)',
    re.IGNORECASE,
)
_MALE_GENDER_RE = re.compile(
    r'(?:\bмужчин(?:а|ы|е|у|ой)?\b|\bмужск(?:ой|ая|ое|ие)\b|\bмужской\s+пол\b|\bmale\b|\bсам(?:ец|ца|цу|цом|це)\b|他)',
    re.IGNORECASE,
)
_ENTITY_NOTE_RE = re.compile(r'(?:персонаж|псевдоним|имя(?:\b|;)|божество|животное)', re.IGNORECASE)
def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def sha256_text(value: str) -> str:
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()
def glossary_fingerprint(glossary: Any) -> str:
    """Build a stable fingerprint from model-facing glossary decisions only."""
    if isinstance(glossary, list):
        rows = []
        for item in glossary:
            if not isinstance(item, dict):
                continue
            original = str(item.get('original') or '').strip()
            if not original:
                continue
            rows.append({
                'original': original,
                'rus': str(item.get('rus') or item.get('translation') or '').strip(),
                'note': str(item.get('note') or '').strip(),
                'authority': str(item.get('authority') or 'required'),
                'scope': str(item.get('scope') or 'project'),
            })
        rows.sort(key=lambda row: (row['original'].casefold(), row['original']))
        return sha256_text(_canonical_json(rows))
    if isinstance(glossary, dict):
        rows = []
        for original, data in glossary.items():
            if isinstance(data, dict):
                rows.append({
                    'original': str(original),
                    'rus': str(data.get('rus') or data.get('translation') or '').strip(),
                    'note': str(data.get('note') or '').strip(),
                    'authority': str(data.get('authority') or 'required'),
                    'scope': str(data.get('scope') or 'project'),
                })
            elif data is not None:
                rows.append({
                    'original': str(original),
                    'rus': str(data).strip(),
                    'note': '',
                    'authority': 'required',
                    'scope': 'project',
                })
        rows.sort(key=lambda row: (row['original'].casefold(), row['original']))
        return sha256_text(_canonical_json(rows))
    return sha256_text('[]')
def batch_chapter_paths_by_size(
    chapter_paths,
    sizes: dict[str, int],
    limit: int,
    *,
    ordered_paths=None,
) -> list[tuple[str, ...]]:
    """Pack only genuinely adjacent chapters into bounded context requests.
    ``chapter_paths`` can be a sparse stale/cache-miss subset. When ``ordered_paths``
    is supplied, a gap in the full book order forces a batch boundary instead of
    silently treating e.g. chapters 2 and 4 as neighbours because chapter 3 was a
    cache hit.
    """
    cap = max(2500, int(limit or 30000))
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    current_size = 0
    positions = {
        str(path or '').replace('\\', '/'): index
        for index, path in enumerate(ordered_paths or ())
        if str(path or '').strip()
    }
    previous_position: int | None = None
    for raw_path in chapter_paths or ():
        chapter_path = str(raw_path or '').replace('\\', '/')
        if not chapter_path:
            continue
        current_position = positions.get(chapter_path)
        if (
            current
            and positions
            and (
                current_position is None
                or previous_position is None
                or current_position != previous_position + 1
            )
        ):
            batches.append(tuple(current))
            current = []
            current_size = 0
        size = max(1, int((sizes or {}).get(raw_path, (sizes or {}).get(chapter_path, 0)) or 0))
        if current and current_size + size > cap:
            batches.append(tuple(current))
            current = []
            current_size = 0
        if size > cap:
            if current:
                batches.append(tuple(current))
                current = []
                current_size = 0
            batches.append((chapter_path,))
            previous_position = current_position
            continue
        current.append(chapter_path)
        current_size += size
        previous_position = current_position
    if current:
        batches.append(tuple(current))
    return batches
def bounded_translation_tail(text: str, limit: int = PREDECESSOR_TRANSLATION_TAIL_CHARS) -> str:
    """Return a bounded end-of-chapter Russian continuity excerpt.
    The tail is intentionally an excerpt, not full history: it preserves the latest
    dialogue/register/wording while keeping prompt cost bounded.
    """
    clean = str(text or '').strip()
    cap = max(200, int(limit or PREDECESSOR_TRANSLATION_TAIL_CHARS))
    if len(clean) <= cap:
        return clean
    tail = clean[-cap:]
    # Prefer a paragraph/sentence boundary without discarding too much context.
    for marker in ('\n\n', '\n', '. ', '! ', '? ', '… '):
        pos = tail.find(marker)
        if 0 <= pos <= cap // 3:
            return tail[pos + len(marker):].lstrip()
    return tail
def normalize_predecessor_dependency(raw: Any) -> dict:
    """Normalize immutable upstream data used while deriving one chapter context."""
    value = raw if isinstance(raw, dict) else {}
    return {
        'chapter_path': str(value.get('chapter_path') or '').replace('\\', '/'),
        'source_hash': str(value.get('source_hash') or ''),
        'translation_tail_hash': str(value.get('translation_tail_hash') or ''),
        'translation_tail': str(value.get('translation_tail') or '').strip(),
    }
def _snapshot_fingerprint_payload(snapshot: dict, normalized_context: dict | None = None) -> dict:
    context = normalized_context if normalized_context is not None else normalize_model_context(snapshot)
    return {
        'chapter_path': str(snapshot.get('chapter_path') or '').replace('\\', '/'),
        'source_hash': str(snapshot.get('source_hash') or ''),
        'glossary_fingerprint': str(snapshot.get('glossary_fingerprint') or ''),
        'schema_version': int(snapshot.get('schema_version') or 0),
        'prompt_version': int(snapshot.get('prompt_version') or 0),
        'predecessor': normalize_predecessor_dependency(snapshot.get('predecessor')),
        'context': context,
        'semantic_terms': list(snapshot.get('semantic_terms') or []),
    }
def context_policy_is_current(snapshot: Any, *, glossary_hash: str) -> bool:
    if not isinstance(snapshot, dict):
        return False
    try:
        normalized = normalize_model_context(snapshot)
    except ValueError:
        return False
    if (
        int(snapshot.get('schema_version') or 0) != CHAPTER_CONTEXT_SCHEMA_VERSION
        or int(snapshot.get('prompt_version') or 0) != CHAPTER_CONTEXT_PROMPT_VERSION
        or str(snapshot.get('glossary_fingerprint') or '') != str(glossary_hash or '')
    ):
        return False
    stored = str(snapshot.get('context_fingerprint') or '')
    expected = sha256_text(_canonical_json(_snapshot_fingerprint_payload(snapshot, normalized)))
    return bool(stored) and stored == expected
def context_is_current(
    snapshot: Any,
    *,
    source_hash: str,
    glossary_hash: str,
    predecessor: Any = None,
) -> bool:
    return (
        context_policy_is_current(snapshot, glossary_hash=glossary_hash)
        and str(snapshot.get('source_hash') or '') == str(source_hash or '')
        and normalize_predecessor_dependency(snapshot.get('predecessor'))
        == normalize_predecessor_dependency(predecessor)
    )
def normalize_model_context(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('chapter context entry must be an object')
    normalized: dict[str, Any] = {}
    for field in _MODEL_FIELDS:
        value = raw.get(field)
        if field in _LIST_FIELDS:
            if value is None or value == '':
                normalized[field] = []
            elif isinstance(value, list):
                normalized[field] = value
            elif isinstance(value, tuple):
                normalized[field] = list(value)
            else:
                # Gemini occasionally emits a single JSON scalar/object for a field that
                # semantically contains one item. Preserve the value and normalize only
                # the container shape; do not infer or rewrite its meaning.
                normalized[field] = [value]
        else:
            if value is None:
                normalized[field] = ''
            elif isinstance(value, str):
                normalized[field] = value.strip()
            elif isinstance(value, (dict, list)):
                normalized[field] = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            else:
                normalized[field] = str(value).strip()
    return normalized
def _normalize_glossary_map(glossary: Any) -> dict[str, dict]:
    """Normalize runtime glossary to source-keyed records for conflict checks."""
    if isinstance(glossary, list):
        result = {}
        for item in glossary:
            if not isinstance(item, dict):
                continue
            original = str(item.get('original') or item.get('source') or item.get('key') or '').strip()
            if original:
                result[original] = dict(item)
        return result
    if isinstance(glossary, dict):
        result = {}
        for original, value in glossary.items():
            source = str(original or '').strip()
            if not source:
                continue
            if isinstance(value, dict):
                result[source] = dict(value)
            elif value is not None:
                result[source] = {'original': source, 'rus': str(value), 'note': ''}
        return result
    return {}
def _explicit_gender_claim(value: Any) -> str | None:
    """Return only explicit male/female claims; ambiguous mixed text is ignored."""
    text = str(value or '').strip()
    if not text:
        return None
    female = bool(_FEMALE_GENDER_RE.search(text))
    male = bool(_MALE_GENDER_RE.search(text))
    if female == male:
        return None
    return 'female' if female else 'male'
def detect_glossary_context_conflicts(
    context: Any,
    glossary: Any,
    *,
    chapter_path: str = '',
) -> list[dict]:
    """Find high-confidence metadata disagreements without changing canonical glossary.
    v1 intentionally detects only explicit entity gender conflicts. Role/relationship
    contradictions are too semantic for deterministic promotion and stay out of this
    gate until we have stronger source evidence/provenance.
    """
    try:
        normalized = normalize_model_context(context)
    except ValueError:
        return []
    glossary_map = _normalize_glossary_map(glossary)
    if not glossary_map:
        return []
    conflicts: list[dict] = []
    for entity in normalized.get('active_entities') or []:
        if not isinstance(entity, dict):
            continue
        source_name = str(entity.get('source_name') or '').strip()
        context_text = str(entity.get('gender_or_grammar') or '').strip()
        context_gender = _explicit_gender_claim(context_text)
        if not source_name or context_gender not in {'male', 'female'}:
            continue
        candidate_terms = [source_name]
        aliases = entity.get('aliases')
        if isinstance(aliases, (list, tuple)):
            candidate_terms.extend(str(alias or '').strip() for alias in aliases)
        elif aliases:
            candidate_terms.append(str(aliases).strip())
        evidence = []
        seen_terms = set()
        for term in candidate_terms:
            if not term or term in seen_terms:
                continue
            seen_terms.add(term)
            entry = glossary_map.get(term)
            if not isinstance(entry, dict):
                continue
            note = str(entry.get('note') or '').strip()
            # Avoid treating grammatical gender notes on locations/terms as character identity.
            if not _ENTITY_NOTE_RE.search(note):
                continue
            glossary_gender = _explicit_gender_claim(note)
            if glossary_gender not in {'male', 'female'} or glossary_gender == context_gender:
                continue
            evidence.append({
                'source_term': term,
                'target': str(entry.get('rus') or entry.get('translation') or '').strip(),
                'glossary_value': glossary_gender,
                'glossary_note': note,
                'authority': str(entry.get('authority') or 'required'),
                'scope': str(entry.get('scope') or 'project'),
            })
        if not evidence:
            continue
        severity = 'high' if any(item.get('authority') != 'advisory' for item in evidence) else 'medium'
        conflicts.append({
            'type': 'gender_mismatch',
            'field': 'gender_or_grammar',
            'severity': severity,
            'status': 'needs_resolution',
            'chapter_path': str(chapter_path or '').replace('\\', '/'),
            'entity_source_name': source_name,
            'context_value': context_gender,
            'context_text': context_text,
            'glossary_evidence': evidence,
        })
    return conflicts
def context_conflict_fingerprint(conflicts: Any) -> str:
    """Stable identity for persisted unresolved conflict sets."""
    rows = conflicts if isinstance(conflicts, list) else []
    return sha256_text(_canonical_json(rows))
def make_snapshot(
    *,
    chapter_path: str,
    source_hash: str,
    glossary_hash: str,
    context: Any,
    predecessor: Any = None,
    semantic_terms: Any = None,
) -> dict:
    normalized = normalize_model_context(context)
    normalized_predecessor = normalize_predecessor_dependency(predecessor)
    normalized_terms = [dict(item) for item in (semantic_terms or []) if isinstance(item, dict)]
    fingerprint_payload = _snapshot_fingerprint_payload({
        'chapter_path': str(chapter_path or '').replace('\\', '/'),
        'source_hash': str(source_hash or ''),
        'glossary_fingerprint': str(glossary_hash or ''),
        'schema_version': CHAPTER_CONTEXT_SCHEMA_VERSION,
        'prompt_version': CHAPTER_CONTEXT_PROMPT_VERSION,
        'predecessor': normalized_predecessor,
        'semantic_terms': normalized_terms,
    }, normalized)
    return {
        'chapter_path': fingerprint_payload['chapter_path'],
        'source_hash': fingerprint_payload['source_hash'],
        'glossary_fingerprint': fingerprint_payload['glossary_fingerprint'],
        'schema_version': CHAPTER_CONTEXT_SCHEMA_VERSION,
        'prompt_version': CHAPTER_CONTEXT_PROMPT_VERSION,
        'generated_at': time.time(),
        'context_fingerprint': sha256_text(_canonical_json(fingerprint_payload)),
        'predecessor': normalized_predecessor,
        'semantic_terms': normalized_terms,
        **normalized,
    }
def model_facing_context(snapshot: Any) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    result = {'chapter_path': str(snapshot.get('chapter_path') or '')}
    for field in _MODEL_FIELDS:
        value = snapshot.get(field)
        if field in _LIST_FIELDS:
            result[field] = list(value) if isinstance(value, list) else []
        else:
            result[field] = str(value or '')
    result['context_fingerprint'] = str(snapshot.get('context_fingerprint') or '')
    predecessor = normalize_predecessor_dependency(snapshot.get('predecessor'))
    if predecessor['chapter_path']:
        public_predecessor = {'chapter_path': predecessor['chapter_path']}
        if predecessor['translation_tail']:
            public_predecessor['translation_tail'] = predecessor['translation_tail']
        result['predecessor'] = public_predecessor
    return result
def _response_context_object(value: Any) -> dict | None:
    """Return a model-context object without guessing chapter identity by position."""
    if not isinstance(value, dict):
        return None
    for nested_key in ('context', 'chapter_context', 'chapterContext'):
        nested = value.get(nested_key)
        if isinstance(nested, dict):
            result = dict(nested)
            if 'semantic_terms' in value and 'semantic_terms' not in result:
                result['semantic_terms'] = value.get('semantic_terms')
            return result
    if any(field in value for field in _MODEL_FIELDS):
        return value
    return None
def _response_identifier(value: Any, *, expected_ids: set[str], by_path: dict[str, str]) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ('id', 'document_id', 'documentId', 'chapter_id', 'chapterId'):
        candidate = str(value.get(key) or '').strip()
        if candidate in expected_ids:
            return candidate
    for key in ('chapter_path', 'chapterPath', 'document_path', 'documentPath', 'path'):
        candidate = str(value.get(key) or '').replace('\\', '/').strip()
        if candidate in by_path:
            return by_path[candidate]
    return None
def _coerce_context_response_map(payload: Any, documents: list[dict]) -> dict[str, dict]:
    """Normalize common Gemini JSON wrappers while preserving explicit chapter identity.
    Accepted examples include direct ``{"c0": {...}}`` maps, wrapper objects such as
    ``{"chapter_contexts": {"c0": {...}}}``, and arrays whose entries carry an
    explicit request id or exact chapter_path. Positional array mapping is deliberately
    forbidden because it could attach context to the wrong chapter.
    """
    expected_ids = {str(doc.get('id') or '') for doc in documents if str(doc.get('id') or '')}
    by_path = {
        str(doc.get('chapter_path') or '').replace('\\', '/'): str(doc.get('id') or '')
        for doc in documents
        if str(doc.get('chapter_path') or '') and str(doc.get('id') or '')
    }
    result: dict[str, dict] = {}
    visited: set[int] = set()
    def resolve_key(raw_key: Any) -> str | None:
        key = str(raw_key or '').strip()
        if key in expected_ids:
            return key
        normalized_path = key.replace('\\', '/')
        return by_path.get(normalized_path)
    def visit(node: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(node, (dict, list)):
            return
        node_id = id(node)
        if node_id in visited:
            return
        visited.add(node_id)
        if isinstance(node, list):
            for item in node:
                if not isinstance(item, dict):
                    continue
                identifier = _response_identifier(item, expected_ids=expected_ids, by_path=by_path)
                context_obj = _response_context_object(item)
                if identifier and context_obj is not None:
                    result.setdefault(identifier, context_obj)
                visit(item, depth + 1)
            return
        for raw_key, value in node.items():
            identifier = resolve_key(raw_key)
            context_obj = _response_context_object(value)
            if identifier and context_obj is not None:
                result.setdefault(identifier, context_obj)
        identifier = _response_identifier(node, expected_ids=expected_ids, by_path=by_path)
        context_obj = _response_context_object(node)
        if identifier and context_obj is not None:
            result.setdefault(identifier, context_obj)
        # Recurse so benign provider/model wrappers (contexts/results/data/etc.) do not
        # make a complete response unusable. Identity still has to be explicit.
        for value in node.values():
            if isinstance(value, (dict, list)):
                visit(value, depth + 1)
    visit(payload)
    return result
def describe_context_response_shape(payload: Any) -> str:
    """Compact non-content diagnostic for structural failures."""
    if isinstance(payload, dict):
        keys = [str(key) for key in list(payload.keys())[:12]]
        return f"object keys={keys}"
    if isinstance(payload, list):
        first_type = type(payload[0]).__name__ if payload else 'empty'
        return f"array len={len(payload)} first={first_type}"
    return type(payload).__name__
def validate_context_response(payload: Any, documents: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate model JSON using explicit request ids or exact chapter paths.
    Valid rows can be persisted before retrying an incomplete batch, so a retry only asks
    for the remaining chapters. Common harmless JSON wrappers are accepted, but positional
    mapping is intentionally rejected.
    """
    if not isinstance(payload, (dict, list)):
        raise ValueError('chapter context response must be a JSON object or array with explicit chapter ids')
    response_map = _coerce_context_response_map(payload, documents)
    valid: list[dict] = []
    invalid: list[str] = []
    by_id = {str(doc.get('id') or ''): doc for doc in documents if str(doc.get('id') or '')}
    for doc_id, document in by_id.items():
        raw = response_map.get(doc_id)
        if raw is None:
            invalid.append(doc_id)
            continue
        try:
            normalized = normalize_model_context(raw)
        except ValueError:
            invalid.append(doc_id)
            continue
        from .semantic_terms import normalize_semantic_terms
        semantic_terms = normalize_semantic_terms(
            raw.get('semantic_terms') if isinstance(raw, dict) else None,
            source_text=str(document.get('text') or ''),
        )
        valid.append({
            'id': doc_id,
            'chapter_path': str(document.get('chapter_path') or ''),
            'source_hash': str(document.get('source_hash') or ''),
            'predecessor': normalize_predecessor_dependency(document.get('predecessor')),
            'context': normalized,
            'semantic_terms': semantic_terms,
        })
    return valid, invalid
def build_chapter_context_prompt(
    documents: list[dict],
    glossary_context: str = '',
    predecessor_anchor: dict | None = None,
) -> str:
    template = (
        'You extract source-grounded narrative context for a literary translation system.\n'
        'Analyze the ordered chapter chain below. Return ONLY one JSON object keyed by the provided document ids.\n'
        'The ROOT keys must be the exact request ids (for example c0, c1). Do NOT wrap them in chapter_contexts, contexts, chapters, results, data, or an array.\n'
        'Before returning, verify that every input document id appears exactly once as a ROOT key and that no other root keys are present.\n'
        'For each document, describe only information knowable by the end of that document. Later documents may help detect ambiguity, '
        'but must never turn a future reveal into an earlier fact. Put such cases into reveal_constraints instead.\n'
        'Do not translate the chapter. Do not invent facts. Unknown values must be empty strings/lists.\n'
        'Write context values in concise Russian because the downstream target language is Russian.\n'
        'Each entry MUST contain these fields: summary, pov, tone_and_register, setting, active_entities, relationships, '
        'important_events, continuity_in, continuity_out, ambiguous_references, reveal_constraints, semantic_terms.\n'
        'semantic_terms is a compact list of reusable proper names/terms that need one Russian lexical decision across parallel chapter translation. '
        'Each item uses source, suggested_ru, kind, aliases, note, confidence. source/aliases MUST be exact source substrings. '
        'Do not include ordinary vocabulary or entries already fixed by the canonical glossary unless a source alias/conflict needs recording.\n'
        'active_entities is a list of objects using source_name, aliases, role, gender_or_grammar, state when supported by source evidence.\n'
        'source_name and every aliases item MUST be an exact source-language string copied verbatim from source/glossary. '
        'Never put Russian translations, parenthetical explanations, comments, or mixed source+Russian strings inside aliases.\n'
        'relationships is a list of compact objects describing participants, relationship state and address/register when relevant.\n'
        'reveal_constraints is a list of objects with fact and instruction; use it to preserve deliberate ambiguity or delayed reveals.\n'
        'PREDECESSOR_SOURCE_ANCHOR, when present, is the immediate chapter before the first requested document. Use it only to establish continuity into the first document and never produce an output object for the anchor.\n'
        'A predecessor_translation_tail is a bounded excerpt from an already accepted Russian translation. Use it only for names, register, address forms and stylistic continuity. It is not source authority and must never override the source or canonical glossary.\n'
        'Keep each chapter context compact; summary should normally be <= 900 characters.\n\n'
        'CANONICAL GLOSSARY CONTEXT (read-only):\n{glossary_context}\n\n'
        'PREDECESSOR_SOURCE_ANCHOR JSON:\n{predecessor_anchor_json}\n\n'
        'ORDERED SOURCE DOCUMENTS JSON:\n{documents_json}\n'
    )
    public_documents = [
        {
            'id': str(doc.get('id') or ''),
            'chapter_path': str(doc.get('chapter_path') or ''),
            'source': str(doc.get('text') or ''),
            **(
                {
                    'predecessor_chapter_path': normalize_predecessor_dependency(doc.get('predecessor'))['chapter_path'],
                    'predecessor_translation_tail': normalize_predecessor_dependency(doc.get('predecessor'))['translation_tail'],
                }
                if normalize_predecessor_dependency(doc.get('predecessor'))['chapter_path']
                else {}
            ),
        }
        for doc in documents
    ]
    anchor = predecessor_anchor if isinstance(predecessor_anchor, dict) else {}
    public_anchor = {
        'chapter_path': str(anchor.get('chapter_path') or '').replace('\\', '/'),
        'source': str(anchor.get('source') or ''),
        'translation_tail': str(anchor.get('translation_tail') or '').strip(),
    } if anchor else {}
    return (
        str(template)
        .replace('{glossary_context}', glossary_context or '')
        .replace('{predecessor_anchor_json}', json.dumps(public_anchor, ensure_ascii=False, indent=2))
        .replace('{documents_json}', json.dumps(public_documents, ensure_ascii=False, indent=2))
    )
def format_chapter_context_block(snapshots: list[dict]) -> str:
    model_rows = [row for snapshot in snapshots if (row := model_facing_context(snapshot))]
    if not model_rows:
        return ''
    return '\n'.join([
        '### SOURCE-DERIVED CHAPTER CONTEXT',
        'This context is read-only disambiguation support. SOURCE_ATOMS/source HTML remains authoritative. '
        'Never add a fact merely because it appears here, and never expose a reveal earlier than the source does. '
        'If predecessor.translation_tail is present, use it only as a bounded Russian style/wording continuity reference; '
        'it must not override source facts or canonical glossary bindings.',
        '```json',
        json.dumps({'CHAPTER_CONTEXTS': model_rows}, ensure_ascii=False, indent=2),
        '```',
    ])
