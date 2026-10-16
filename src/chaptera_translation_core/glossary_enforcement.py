from __future__ import annotations
import re
from typing import Any
ENFORCEMENT_STRICT = 'strict'
ENFORCEMENT_LEXICAL_FAMILY = 'lexical_family'
ENFORCEMENT_FLEXIBLE = 'flexible'
_VALID = {ENFORCEMENT_STRICT, ENFORCEMENT_LEXICAL_FAMILY, ENFORCEMENT_FLEXIBLE}
_TYPE_RE = re.compile(r'(?:^|[;\s])type\s*=\s*([a-z_]+)', re.IGNORECASE)
_STRICT_KINDS = {'person', 'alias', 'place', 'organization', 'event', 'title'}
_FAMILY_KINDS = {'technique', 'item', 'rank'}
_FLEXIBLE_KINDS = {'concept', 'other'}
_STRICT_NOTE_MARKERS = (
    'персонаж', 'псевдоним', 'личный титул', 'организация', 'локация', 'город',
    'страна', 'регион', 'континент', 'божество', 'клан', 'фракция', 'орден',
)
_MATERIAL_NOTE_MARKERS = (
    'металл', 'сплав', 'материал', 'руда', 'артефакт', 'уникальное оружие', 'особое оружие',
)
# Named-group suffixes are conservative: these keys normally denote a proper group/faction/clan label.
_CJK_NAMED_GROUP_SUFFIXES = ('族', '會', '会', '盟', '派', '門', '门', '教', '團', '团')
def normalize_enforcement(value: Any) -> str:
    normalized = str(value or '').strip().casefold()
    aliases = {
        'hard': ENFORCEMENT_STRICT,
        'exact': ENFORCEMENT_STRICT,
        'required': ENFORCEMENT_STRICT,
        'family': ENFORCEMENT_LEXICAL_FAMILY,
        'morphological': ENFORCEMENT_LEXICAL_FAMILY,
        'morphology': ENFORCEMENT_LEXICAL_FAMILY,
        'soft': ENFORCEMENT_FLEXIBLE,
        'preferred': ENFORCEMENT_FLEXIBLE,
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in _VALID else ''
def note_kind(note: str) -> str:
    match = _TYPE_RE.search(str(note or ''))
    return match.group(1).casefold() if match else ''
def infer_enforcement(
    original: str,
    target: str,
    note: str = '',
    provenance: dict | None = None,
    *,
    kind: str = '',
    explicit: str = '',
) -> str:
    """Infer how strictly a glossary binding should be enforced.
    Manual/project-authored glossary rows remain strict by default. Semantic-preflight rows
    are graded: named entities/groups are strict, techniques/material-like terms require the
    same lexical family, while generic concepts are preferences rather than per-occurrence
    hard constraints.
    """
    explicit_value = normalize_enforcement(explicit)
    if explicit_value:
        return explicit_value
    provenance = provenance if isinstance(provenance, dict) else {}
    explicit_value = normalize_enforcement(provenance.get('enforcement') or provenance.get('enforcement_class'))
    if explicit_value:
        return explicit_value
    note_text = str(note or '').strip()
    note_lower = note_text.casefold()
    effective_kind = str(kind or provenance.get('semantic_kind') or note_kind(note_text) or '').strip().casefold()
    if effective_kind in _STRICT_KINDS:
        return ENFORCEMENT_STRICT
    if effective_kind in _FAMILY_KINDS:
        return ENFORCEMENT_LEXICAL_FAMILY
    if any(marker in note_lower for marker in _STRICT_NOTE_MARKERS):
        return ENFORCEMENT_STRICT
    if any(marker in note_lower for marker in _MATERIAL_NOTE_MARKERS):
        return ENFORCEMENT_LEXICAL_FAMILY
    source = str(original or '').strip()
    if source.endswith(_CJK_NAMED_GROUP_SUFFIXES):
        return ENFORCEMENT_STRICT
    provenance_source = str(provenance.get('source') or '').casefold()
    promoted_from = str(provenance.get('promoted_from') or '').casefold()
    semantic_origin = (
        'semantic_preflight' in provenance_source
        or promoted_from == 'session_canonical'
        or provenance.get('semantic_kind') is not None
    )
    if effective_kind in _FLEXIBLE_KINDS or semantic_origin:
        return ENFORCEMENT_FLEXIBLE
    # A human/project glossary row historically meant an explicit lexical contract.
    return ENFORCEMENT_STRICT
def semantic_promotion_eligible(binding: dict, *, evidence_count: int, confidence: float) -> tuple[bool, str]:
    """Return whether an automatic semantic observation may become durable project authority."""
    binding = dict(binding or {})
    source = str(binding.get('original') or binding.get('source') or '').strip()
    target = str(binding.get('rus') or binding.get('translation') or binding.get('suggested_ru') or '').strip()
    note = str(binding.get('note') or '').strip()
    provenance = binding.get('provenance') if isinstance(binding.get('provenance'), dict) else {}
    enforcement = infer_enforcement(
        source,
        target,
        note,
        provenance,
        kind=str(provenance.get('semantic_kind') or note_kind(note) or ''),
        explicit=str(binding.get('enforcement') or ''),
    )
    count = max(0, int(evidence_count or 0))
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    if enforcement == ENFORCEMENT_STRICT:
        return (count >= 1 and conf >= 0.65), enforcement
    if enforcement == ENFORCEMENT_LEXICAL_FAMILY:
        # Explicit material/artifact anchors are safe with one strong evidence; generic
        # techniques/items need recurrence or unusually high confidence before becoming durable.
        material_like = any(marker in note.casefold() for marker in _MATERIAL_NOTE_MARKERS)
        return ((count >= 1 and conf >= 0.65) if material_like else (count >= 2 or conf >= 0.90)), enforcement
    # Generic concepts remain session-level after a single observation.
    return (count >= 2 and conf >= 0.75), enforcement
