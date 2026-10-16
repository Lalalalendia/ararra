from __future__ import annotations
import asyncio
import hashlib
import json
import math
import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from chaptera_translation_core.api.errors import PartialGenerationError, ProvenanceConflictError, ValidationFailedError
from chaptera_translation_core.core.translation_units import (
    build_source_atoms, has_meaningful_translation_text, mark_coverage_atoms_for_repair, rebind_coverage_to_expected_scope,
    reconcile_multi_job_coverage, target_contamination_suspected,
)
from chaptera_translation_core.core.translation_text_salvage import detect_markup_leak, salvage_translation_text
from chaptera_translation_core.utils.helpers import TokenCounter
@dataclass(frozen=True)
class WorkItem:
    work_id: str
    kind: str
    job_id: str
    chapter_id: str
    payload: dict
    source_chars: int
    expected_output_chars: int
    atom_count: int = 0
    closes_parent: bool = False
    age_seconds: float = 0.0
@dataclass
class RequestPack:
    pack_id: str
    kind: str
    items: list[WorkItem] = field(default_factory=list)
    source_chars: int = 0
    expected_output_chars: int = 0
    @property
    def job_count(self) -> int:
        # Возвращает число независимых work items внутри одного provider request.
        return len(self.items)
    @property
    def atom_count(self) -> int:
        # Суммирует source atoms всех jobs для soft reliability target одного RequestPack.
        return sum(max(0, int(item.atom_count or 0)) for item in self.items)
_ENVELOPE_SPLIT_MARKERS = ('Request envelope', 'Request payload', 'TPM=', 'configured limit')
def is_pre_provider_envelope_failure(error: Exception) -> bool:
    # Отличает deterministic hard-envelope admission от содержательного provider rejection.
    return isinstance(error, ValidationFailedError) and any(marker in str(error) for marker in _ENVELOPE_SPLIT_MARKERS)
def split_request_pack(pack: RequestPack, *, left_suffix: str = 'L', right_suffix: str = 'R') -> tuple[RequestPack, RequestPack] | tuple[()]:
    # Делит только multi-job pack, сохраняя exact WorkItem identity и пересчитывая budgets без нового claim.
    if pack.job_count <= 1:
        return ()
    midpoint = max(1, pack.job_count // 2)
    def build(items: list[WorkItem], suffix: str) -> RequestPack:
        items = list(items)
        return RequestPack(
            f'{pack.pack_id}-{suffix}', pack.kind, items,
            sum(item.source_chars for item in items), sum(item.expected_output_chars for item in items),
        )
    return build(pack.items[:midpoint], left_suffix), build(pack.items[midpoint:], right_suffix)
def fair_worker_claim_limit(concurrency_limit: int, configured_instances: int, active_queue_tasks: int) -> int:
    # Не дает первому worker мгновенно claim-нуть почти всю короткую очередь до запуска остальных выбранных keys.
    hard = max(1, int(concurrency_limit or 1))
    instances = max(1, int(configured_instances or 1))
    active = max(0, int(active_queue_tasks or 0))
    if instances <= 1 or active <= 0:
        return hard
    return min(hard, max(1, math.ceil(active / instances)))
@dataclass(frozen=True)
class PackingBudget:
    max_jobs: int = 24
    max_source_chars: int = 12000
    max_expected_output_chars: int = 18000
    soft_target_source_chars: int = 8000
    max_atoms: int = 80
def debt_scope_hash(parent_task_id: str, job_id: str, chapter_id: str, payload: dict) -> str:
    # Хеширует только immutable debt ownership/source, не включая mutable target context или retry state.
    source_texts = [str(value) for value in (payload or {}).get('source_texts') or ()]
    canonical = {
        'parent_task_id': str(parent_task_id or ''),
        'job_id': str(job_id or ''),
        'chapter_id': str(chapter_id or ''),
        'atom_ids': [str(value) for value in (payload or {}).get('atom_ids') or ()],
        'source_hashes': [hashlib.sha256(value.encode('utf-8')).hexdigest() for value in source_texts],
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
def debt_work_item(row: dict, now: float) -> WorkItem:
    # Превращает durable debt row в чистый объект packer-а без DB-зависимостей.
    payload = dict(row.get('payload')) if isinstance(row.get('payload'), dict) else json.loads(row.get('payload_json') or '{}')
    expected_scope_hash = debt_scope_hash(row.get('parent_task_id'), row.get('job_id'), row.get('chapter_path'), payload)
    stored_scope_hash = str(payload.get('scope_hash') or '')
    if stored_scope_hash and stored_scope_hash != expected_scope_hash:
        raise ProvenanceConflictError(f"INTERNAL_SCOPE_MISMATCH: debt {row.get('debt_id')} durable scope hash поврежден.")
    payload['scope_version'] = 1
    payload['scope_hash'] = expected_scope_hash
    source_texts = [str(value) for value in payload.get('source_texts') or ()]
    current_translation = str(payload.get('current_translation') or '')
    source_chars = max(0, int(row.get('source_chars') or 0)) if 'source_chars' in row else sum(len(value) for value in source_texts) + len(current_translation)
    atom_count = len(payload.get('atom_ids') or ())
    debt_kind = str(row['debt_kind'])
    if debt_kind == 'alignment':
        # Alignment output должен как минимум вместить уже оплаченный перевод плюс JSON/segment overhead.
        expected_output_chars = max(256, len(current_translation) + atom_count * 128)
    elif debt_kind == 'missing' and payload.get('missing_window_expanded'):
        # Expanded missing становится boundary redistribution: provider должен иметь бюджет и на уже существующий соседний target.
        expected_output_chars = max(256, len(current_translation) + sum(len(value) for value in source_texts) * 2 + atom_count * 96)
    else:
        expected_output_chars = max(256, sum(len(value) for value in source_texts) * 2 + atom_count * 96)
    return WorkItem(
        work_id=str(row['debt_id']), kind=debt_kind, job_id=str(row.get('job_id') or row['debt_id']),
        chapter_id=str(row.get('chapter_path') or ''), payload=payload, source_chars=source_chars,
        expected_output_chars=expected_output_chars,
        atom_count=atom_count, closes_parent=bool(row.get('closes_parent')),
        age_seconds=max(0.0, now - float(row.get('created_at') or now)),
    )
def work_value(item: WorkItem) -> float:
    # Оценивает marginal value одного work item в единицах полезной работы на provider slot.
    base = max(1.0, len(item.payload.get('atom_ids') or ()))
    completion_bonus = 8.0 if item.closes_parent else 0.0
    age_bonus = min(4.0, item.age_seconds / 300.0)
    alignment_bonus = 1.5 if item.kind == 'alignment' else 1.0
    return (base + completion_bonus + age_bonus) * alignment_bonus
def pack_work_items(items: list[WorkItem], budget: PackingBudget, *, pack_prefix: str = 'pack') -> list[RequestPack]:
    # Упаковывает work items одного типа greedily по реальным char/output budgets, а не по числу repair windows.
    if not items:
        return []
    ordered = sorted(items, key=lambda item: (-work_value(item), item.source_chars, item.work_id))
    packs = []
    for item in ordered:
        placed = False
        for pack in packs:
            if pack.kind != item.kind or len(pack.items) >= budget.max_jobs:
                continue
            if pack.source_chars + item.source_chars > budget.max_source_chars:
                continue
            if pack.expected_output_chars + item.expected_output_chars > budget.max_expected_output_chars:
                continue
            if budget.max_atoms > 0 and pack.atom_count + item.atom_count > budget.max_atoms:
                continue
            pack.items.append(item)
            pack.source_chars += item.source_chars
            pack.expected_output_chars += item.expected_output_chars
            placed = True
            break
        if not placed:
            pack = RequestPack(f'{pack_prefix}-{len(packs):04d}', item.kind, [item], item.source_chars, item.expected_output_chars)
            packs.append(pack)
    return packs
def estimate_pack_count(items: list[WorkItem], budget: PackingBudget) -> int:
    # Возвращает conservative число provider requests для текущего work debt.
    return len(pack_work_items(items, budget))
def _expand_payload_for_context_echo(
    payload: dict, *, debt_kind: str, expansion_key: str, expanded_key: str
) -> dict | None:
    # Один раз расширяет editable repair scope только по заранее materialized точным neighbor segment scopes.
    # Никаких догадок по числовому atom_id: если durable snapshot не содержит полный соседний segment, expansion запрещен.
    if not isinstance(payload, dict) or payload.get(expanded_key) or payload.get('editable_window_expanded'):
        return None
    expansion = payload.get(expansion_key)
    if not isinstance(expansion, dict):
        return None
    original_ids = [str(value) for value in payload.get('atom_ids') or ()]
    original_sources = [str(value) for value in payload.get('source_texts') or ()]
    if not original_ids or len(original_ids) != len(original_sources) or len(original_ids) != len(set(original_ids)):
        return None
    def normalize_candidate(value):
        if not isinstance(value, dict):
            return None
        atom_ids = [str(item) for item in value.get('atom_ids') or ()]
        source_texts = [str(item) for item in value.get('source_texts') or ()]
        target = str(value.get('target_translation') or '')
        if not atom_ids or len(atom_ids) != len(source_texts) or len(atom_ids) != len(set(atom_ids)) or not target.strip():
            return None
        return {'atom_ids': atom_ids, 'source_texts': source_texts, 'target_translation': target}
    left = normalize_candidate(expansion.get('left'))
    right = normalize_candidate(expansion.get('right'))
    occupied = set(original_ids)
    for candidate in (left, right):
        if candidate and occupied.intersection(candidate['atom_ids']):
            return None
        if candidate:
            occupied.update(candidate['atom_ids'])
    if not left and not right:
        return None
    new_ids = []
    new_sources = []
    target_parts = []
    if left:
        new_ids.extend(left['atom_ids'])
        new_sources.extend(left['source_texts'])
        target_parts.append(left['target_translation'])
    new_ids.extend(original_ids)
    new_sources.extend(original_sources)
    current_translation = str(payload.get('current_translation') or '')
    if current_translation.strip():
        target_parts.append(current_translation)
    if right:
        new_ids.extend(right['atom_ids'])
        new_sources.extend(right['source_texts'])
        target_parts.append(right['target_translation'])
    if len(new_ids) != len(set(new_ids)) or len(new_ids) != len(new_sources):
        return None
    expanded = dict(payload)
    expanded['atom_ids'] = new_ids
    expanded['source_texts'] = new_sources
    expanded['current_translation'] = '\n'.join(part.strip() for part in target_parts if part.strip())
    expanded['left_context'] = str(expansion.get('outer_left_source_context') or '')
    expanded['left_source_context'] = str(expansion.get('outer_left_source_context') or '')
    expanded['left_target_context'] = str(expansion.get('outer_left_target_context') or '')
    expanded['right_context'] = str(expansion.get('outer_right_source_context') or '')
    expanded['right_source_context'] = str(expansion.get('outer_right_source_context') or '')
    expanded['right_target_context'] = str(expansion.get('outer_right_target_context') or '')
    expanded['editable_window_expanded'] = True
    expanded[expanded_key] = True
    expanded[f'{debt_kind}_window_original_atom_ids'] = original_ids
    expanded[f'{debt_kind}_window_added_atom_ids'] = [atom_id for atom_id in new_ids if atom_id not in set(original_ids)]
    expanded[f'{debt_kind}_window_expansion_reason'] = 'CONTEXT_ECHO'
    # Повторное расширение запрещено; provenance hash будет пересчитан durable queue перед requeue.
    expanded.pop('scope_hash', None)
    return expanded
def expand_alignment_payload_for_context_echo(payload: dict) -> dict | None:
    return _expand_payload_for_context_echo(
        payload, debt_kind='alignment', expansion_key='alignment_expansion', expanded_key='alignment_window_expanded'
    )
def expand_missing_payload_for_context_echo(payload: dict) -> dict | None:
    return _expand_payload_for_context_echo(
        payload, debt_kind='missing', expansion_key='missing_expansion', expanded_key='missing_window_expanded'
    )
def build_debt_pack_prompt(pack: RequestPack, glossary_context: str = '', style_guidance: str = '') -> str:
    # Строит один multi-job repair prompt с независимым salvage по debt_id и строгим coverage contract.
    jobs = []
    for item in pack.items:
        payload = item.payload
        job = {
            'debt_id': item.work_id,
            'kind': item.kind,
            'atom_ids': list(payload.get('atom_ids') or ()),
            'render_domain_ids': list(payload.get('render_domain_ids') or ()),
            'source_texts': list(payload.get('source_texts') or ()),
            'current_translation': str(payload.get('current_translation') or ''),
            'left_source_context': str(payload.get('left_source_context') or payload.get('left_context') or ''),
            'left_target_context': str(payload.get('left_target_context') or ''),
            'right_source_context': str(payload.get('right_source_context') or payload.get('right_context') or ''),
            'right_target_context': str(payload.get('right_target_context') or ''),
        }
        if payload.get('editable_window_expanded') or payload.get('alignment_window_expanded') or payload.get('missing_window_expanded'):
            job['editable_window_expanded'] = True
        jobs.append(job)
    if pack.kind == 'alignment':
        mode_instruction = (
            'ALIGNMENT jobs already contain translated meaning in current_translation. Redistribute it across the exact atom_ids; '
            'if current_translation demonstrably lacks source meaning, restore that meaning from source_texts. Return one segment per atom_id. '
            'If editable_window_expanded=true, every listed atom_id is editable: neighbor target text was intentionally moved into current_translation '
            'after a boundary echo, so redistribute it inside this expanded scope. Only left/right context fields remain READ-ONLY.'
        )
    elif pack.kind == 'completeness':
        mode_instruction = (
            'COMPLETENESS jobs are already covered structurally, but current_translation is deterministically incomplete, meaningless, '
            'or abnormally short for the requested source_texts. Retranslate the requested source_texts completely; use current_translation '
            'only as context and do not preserve omissions. Return coverage only for the requested atom_ids.'
        )
    else:
        mode_instruction = (
            'MISSING jobs normally contain uncovered source atoms: translate only those source_texts. '
            'When editable_window_expanded=true, a boundary CONTEXT_ECHO proved that missing meaning is physically attached to a neighboring target segment; '
            'the listed atom_ids are then the complete editable repair scope and current_translation intentionally contains that moved neighbor target. '
            'Redistribute/translate only inside the listed atom_ids and cover every listed atom exactly once; only left/right context remains READ-ONLY.'
        )
    return (
        'You are a Russian literary translation recovery engine.\n'
        + mode_instruction + '\n'
        'Each debt_id is independent. Never move text between debt_id values.\n'
        'render_domain_ids is aligned 1:1 with atom_ids and is a HARD DOM boundary. '
        'A TARGET_SEGMENT may cover multiple adjacent atom_ids only when all of them have the same non-empty render_domain_id. '
        'Never merge across different or empty render_domain_id values.\n'
        'Do not move attribution, speaker tags, action beats, short replies, or punctuation-bearing narration across atom boundaries. '
        'If such text has its own source atom, keep its meaning inside that atom even when Russian literary word order would normally attach it to the neighboring dialogue line.\n'
        'left/right source+target context is READ-ONLY style/continuity context. Never copy, repeat, paraphrase, or return those neighbor target lines.\n'
        'current_translation is the OLD text for the requested scope. Return a REPLACEMENT for the requested atom_ids, never old+new alternatives and never an appended second version.\n'
        'For every debt_id cover every requested atom_id exactly once and no other atom IDs.\n'
        'Return ONLY compact JSONL, one object per debt_id: '
        '{"debt_id":"...","segments":[{"segment_id":"...","covers":["000001"],"translation":"..."}]}.\n'
        'No HTML, Markdown, commentary, source-language residue, or empty translations.\n\n'
        'STYLE_GUIDANCE:\n' + (style_guidance or '') + '\n\n'
        'GLOSSARY_CONTEXT:\n' + (glossary_context or '') + '\n\n'
        'REPAIR_JOBS_JSON:\n' + json.dumps(jobs, ensure_ascii=False, separators=(',', ':'))
    )
def _decode_json_line(line: str):
    # Декодирует одну строку debt JSONL без попыток угадывать поврежденный смысловой payload.
    try:
        return json.loads(line.strip().rstrip(','))
    except (json.JSONDecodeError, TypeError):
        return None
def _decode_json_sequence(text: str) -> list[dict]:
    # Salvage-ит подряд идущие JSON objects даже без перевода строки между ними:
    # Gemini иногда возвращает `}{` вместо JSONL на коротком recovery tail.
    decoder = json.JSONDecoder()
    source = str(text or '')
    index = 0
    items = []
    while index < len(source):
        while index < len(source) and (source[index].isspace() or source[index] == ','):
            index += 1
        if index >= len(source):
            break
        try:
            value, end = decoder.raw_decode(source, index)
        except json.JSONDecodeError:
            # Не угадываем битый объект: двигаемся к следующему вероятному JSON object.
            next_object = source.find('{', index + 1)
            if next_object < 0:
                break
            index = next_object
            continue
        if isinstance(value, dict):
            items.append(value)
        index = end
    return items
def _normalized_repair_guard_text(value: str) -> str:
    # Нормализует текст только для консервативной проверки accidental context echo.
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", " ", str(value or "").lower().replace("ё", "е")).strip()
_REPAIR_CANDIDATE_FP_RE = re.compile(r"\s*\[candidate_fp:([0-9a-f]{24})\]\s*$", re.IGNORECASE)
def _repair_candidate_fingerprint(item: dict) -> str:
    # Хеширует смысловую форму candidate без segment_id и пунктуационного шума.
    # Нужен только для сравнения двух неудачных alignment retry одного durable debt.
    segments = item.get('segments') if isinstance(item, dict) else None
    if not isinstance(segments, list) or not segments:
        return ''
    canonical = []
    for segment in segments:
        if not isinstance(segment, dict):
            return ''
        covers = segment.get('covers')
        if isinstance(covers, str):
            covers = [covers]
        covers = [str(value) for value in covers or ()]
        translation = segment.get('translation')
        if not covers or not isinstance(translation, str) or not translation.strip():
            return ''
        canonical.append({
            'covers': covers,
            'translation': _normalized_repair_guard_text(translation),
        })
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:24]
def repair_error_candidate_fingerprint(error: str) -> str:
    # Извлекает машинный fingerprint из durable error, не меняя человекочитаемый префикс ошибки.
    match = _REPAIR_CANDIDATE_FP_RE.search(str(error or ''))
    return match.group(1).lower() if match else ''
def repair_error_reason(error: str) -> str:
    # Удаляет retry metadata, чтобы сравнивать стабильную причину отклонения между попытками.
    return _REPAIR_CANDIDATE_FP_RE.sub('', str(error or '')).strip()
def repair_error_code(error: str) -> str:
    # Возвращает компактный reason code для diagnostics/агрегации.
    reason = repair_error_reason(error)
    if not reason:
        return 'UNKNOWN'
    head = reason.split(':', 1)[0].strip()
    if re.fullmatch(r'[A-Z0-9_]+', head):
        return head
    return re.sub(r'[^A-Z0-9]+', '_', head.upper()).strip('_') or 'UNKNOWN'
def _decorate_repair_error(reason: str, candidate_fp: str) -> str:
    # Добавляет fingerprint только когда модель действительно вернула структурированный candidate.
    clean = repair_error_reason(reason)
    return f'{clean} [candidate_fp:{candidate_fp}]' if candidate_fp else clean
def _repair_copies_neighbor_target(candidate: str, neighbor_target: str, neighbor_source: str, repair_source: str) -> bool:
    # Source-aware CONTEXT_ECHO: ловит и длинный overlap, и короткий spillover attribution/reply.
    # Если source действительно повторяется, target repetition остаётся разрешённым.
    if target_contamination_suspected(candidate, neighbor_target, repair_source, neighbor_source, short_spillover=True):
        return True
    candidate_norm = _normalized_repair_guard_text(candidate)
    neighbor_norm = _normalized_repair_guard_text(neighbor_target)
    if min(len(candidate_norm), len(neighbor_norm)) < 36:
        return False
    source_norm = re.sub(r"\s+", "", str(repair_source or ""))
    neighbor_source_norm = re.sub(r"\s+", "", str(neighbor_source or ""))
    if source_norm and neighbor_source_norm and source_norm == neighbor_source_norm:
        return False
    if neighbor_norm in candidate_norm and len(neighbor_norm) / max(1, len(candidate_norm)) >= 0.55:
        return True
    shorter, longer = sorted((candidate_norm, neighbor_norm), key=len)
    if len(shorter) >= 48 and SequenceMatcher(None, shorter, longer).ratio() >= 0.93:
        return True
    return False
def _accepted_debt_text(result: dict) -> str:
    # Склеивает только accepted target segments одного debt для cross-item contamination guard.
    return '\n'.join(str(segment.get('translation') or '') for segment in result.get('segments') or ())
def _reject_cross_debt_contamination(accepted: dict[str, dict], errors: dict[str, str], expected: dict[str, WorkItem], pack: RequestPack) -> int:
    # Requeue-ит только более поздний repair item, если его target подозрительно повторяет другой независимый debt source scope.
    rejected = 0
    ordered_ids = [item.work_id for item in pack.items if item.work_id in accepted]
    for left_index, left_id in enumerate(ordered_ids):
        if left_id not in accepted:
            continue
        left_result = accepted[left_id]
        left_source = ''.join(str(value) for value in expected[left_id].payload.get('source_texts') or ())
        left_target = _accepted_debt_text(left_result)
        for right_id in ordered_ids[left_index + 1:]:
            if right_id not in accepted:
                continue
            right_source = ''.join(str(value) for value in expected[right_id].payload.get('source_texts') or ())
            right_target = _accepted_debt_text(accepted[right_id])
            left_work = expected[left_id]
            right_work = expected[right_id]
            left_atoms = [str(value) for value in left_work.payload.get('atom_ids') or ()]
            right_atoms = [str(value) for value in right_work.payload.get('atom_ids') or ()]
            same_local_scope = left_work.chapter_id == right_work.chapter_id and left_work.job_id == right_work.job_id
            adjacent_local_scope = False
            if same_local_scope and left_atoms and right_atoms and all(value.isdigit() for value in [*left_atoms, *right_atoms]):
                left_range = (min(map(int, left_atoms)), max(map(int, left_atoms)))
                right_range = (min(map(int, right_atoms)), max(map(int, right_atoms)))
                adjacent_local_scope = min(abs(right_range[0] - left_range[1]), abs(left_range[0] - right_range[1])) <= 2
            if not target_contamination_suspected(
                left_target, right_target, left_source, right_source, short_spillover=adjacent_local_scope
            ):
                continue
            accepted.pop(right_id, None)
            errors[right_id] = f'CROSS_ITEM_CONTAMINATION_SUSPECTED: target overlaps debt {left_id}'
            rejected += 1
    return rejected
def parse_debt_pack_response(raw_text: str, pack: RequestPack) -> tuple[dict[str, dict], dict[str, str]]:
    # Salvage-ит каждый debt_id независимо, чтобы один плохой job не заставлял повторять весь RequestPack.
    expected = {item.work_id: item for item in pack.items}
    items = []
    text = (raw_text or '').strip()
    if text.startswith('```'):
        text = text.replace('```jsonl', '', 1).replace('```json', '', 1).replace('```', '').strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get('repairs'), list):
        items = parsed['repairs']
    elif isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict) and 'debt_id' in parsed:
        items = [parsed]
    else:
        # Сначала пробуем permissive JSON sequence salvage: он понимает и JSONL,
        # и два валидных объекта, склеенных как `}{`. По строкам оставляем fallback
        # для старых ответов с мусорным префиксом/хвостом.
        items = _decode_json_sequence(text)
        if not items:
            items = [value for value in (_decode_json_line(line) for line in text.splitlines()) if value is not None]
    accepted = {}
    errors = {}
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        debt_id = str(item.get('debt_id') or '')
        work = expected.get(debt_id)
        if work is None or debt_id in seen:
            continue
        seen.add(debt_id)
        candidate_fp = _repair_candidate_fingerprint(item) if pack.kind == 'alignment' else ''
        segments = item.get('segments')
        if not isinstance(segments, list) or not segments:
            errors[debt_id] = _decorate_repair_error('SEGMENTS_MISSING: segments отсутствуют', candidate_fp)
            continue
        requested = [str(value) for value in work.payload.get('atom_ids') or ()]
        requested_set = set(requested)
        covered = []
        normalized = []
        valid = True
        local_salvage_count = 0
        source_texts = [str(value) for value in work.payload.get('source_texts') or ()]
        source_by_atom = {
            atom_id: (source_texts[index] if index < len(source_texts) else '')
            for index, atom_id in enumerate(requested)
        }
        render_domain_ids = [str(value) for value in work.payload.get('render_domain_ids') or ()]
        render_domain_contract_available = len(render_domain_ids) == len(requested)
        render_domain_by_atom = {
            atom_id: (render_domain_ids[index] if index < len(render_domain_ids) else '')
            for index, atom_id in enumerate(requested)
        }
        left_source_context = str(work.payload.get('left_source_context') or work.payload.get('left_context') or '')
        left_target_context = str(work.payload.get('left_target_context') or '')
        right_source_context = str(work.payload.get('right_source_context') or work.payload.get('right_context') or '')
        right_target_context = str(work.payload.get('right_target_context') or '')
        invalid_reason = 'COVERAGE_MISMATCH: coverage не совпадает с requested atom_ids'
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                valid = False
                invalid_reason = 'INVALID_SEGMENT_OBJECT: segment должен быть JSON object'
                break
            covers = segment.get('covers')
            if isinstance(covers, str):
                covers = [covers]
            covers = [str(value) for value in covers or ()]
            translation = segment.get('translation')
            if not covers:
                valid = False
                invalid_reason = 'COVERAGE_EMPTY: segment не содержит covers'
                break
            unknown = [atom_id for atom_id in covers if atom_id not in requested_set]
            if unknown:
                valid = False
                invalid_reason = f'COVERAGE_UNKNOWN_ATOM: неизвестные atom_id={unknown[:4]}'
                break
            if not isinstance(translation, str) or not translation.strip():
                valid = False
                invalid_reason = 'EMPTY_TRANSLATION: segment не содержит непустой translation'
                break
            if pack.kind == 'alignment' and len(covers) != 1:
                valid = False
                invalid_reason = 'ALIGNMENT_MULTI_COVER: alignment segment должен покрывать ровно один atom_id'
                break
            if len(covers) > 1 and render_domain_contract_available:
                render_domains = {render_domain_by_atom.get(atom_id, '') for atom_id in covers}
                if len(render_domains) != 1 or '' in render_domains:
                    valid = False
                    invalid_reason = 'RENDER_DOMAIN_MERGE: repair segment пересекает hard DOM boundary'
                    break
            source_scope = ''.join(source_by_atom.get(atom_id, '') for atom_id in covers)
            salvaged = salvage_translation_text(translation, source_scope)
            if not salvaged.safe or not salvaged.text.strip() or detect_markup_leak(salvaged.text, source_scope):
                valid = False
                invalid_reason = 'MARKUP_LEAK: deterministic salvage не доказал чистый text atom'
                break
            # Если сам immutable source scope состоит только из пунктуации,
            # punctuation-only target является валидным переводом/сохранением
            # структуры. Reject нужен только когда source действительно содержит
            # буквенно-цифровое содержание, которое target потерял.
            if has_meaningful_translation_text(source_scope) and not has_meaningful_translation_text(salvaged.text):
                valid = False
                invalid_reason = 'MEANINGLESS_TRANSLATION: repair потерял смысловой текст source scope'
                break
            if (
                _repair_copies_neighbor_target(salvaged.text, left_target_context, left_source_context, source_scope)
                or _repair_copies_neighbor_target(salvaged.text, right_target_context, right_source_context, source_scope)
            ):
                valid = False
                invalid_reason = 'CONTEXT_ECHO: repair повторяет read-only соседний target вместо замены своего scope'
                break
            if salvaged.changed:
                local_salvage_count += 1
            covered.extend(covers)
            normalized.append({
                'segment_id': str(segment.get('segment_id') or f'{debt_id}-s{index:03d}'),
                'covers': covers,
                'translation': salvaged.text,
            })
        if valid and len(covered) != len(set(covered)):
            valid = False
            invalid_reason = 'DUPLICATE_COVERAGE: один atom_id покрыт больше одного раза'
        if valid and set(covered) != requested_set:
            valid = False
            missing = [atom_id for atom_id in requested if atom_id not in set(covered)]
            invalid_reason = f'COVERAGE_MISMATCH: missing atom_id={missing[:6]}'
        if not valid:
            errors[debt_id] = _decorate_repair_error(invalid_reason, candidate_fp)
            continue
        accepted[debt_id] = {
            'atom_ids': requested,
            'segments': normalized,
            'local_salvage_count': local_salvage_count,
            'scope_hash': str(work.payload.get('scope_hash') or ''),
        }
    _reject_cross_debt_contamination(accepted, errors, expected, pack)
    for debt_id in expected:
        if debt_id not in accepted and debt_id not in errors:
            errors[debt_id] = 'DEBT_RESPONSE_MISSING: debt_id отсутствует в ответе'
    return accepted, errors
def _translation_pack_contamination_suspects(pack: RequestPack, coverages: dict[str, object]) -> dict[str, set[str]]:
    # Находит forward cross-item target bleed и возвращает только suspect atom scopes более поздних transport items.
    suspects: dict[str, set[str]] = {}
    ordered = [item for item in pack.items if item.work_id in coverages]
    for left_index, left_item in enumerate(ordered):
        left_coverage = coverages[left_item.work_id]
        left_atoms = {atom.atom_id: atom for atom in left_coverage.atoms}
        for right_item in ordered[left_index + 1:]:
            right_coverage = coverages[right_item.work_id]
            right_atoms = {atom.atom_id: atom for atom in right_coverage.atoms}
            for left_segment in left_coverage.segments:
                left_source = ''.join(left_atoms[atom_id].source_text for atom_id in left_segment.covers if atom_id in left_atoms)
                for right_segment in right_coverage.segments:
                    right_source = ''.join(right_atoms[atom_id].source_text for atom_id in right_segment.covers if atom_id in right_atoms)
                    if target_contamination_suspected(left_segment.translation, right_segment.translation, left_source, right_source):
                        suspects.setdefault(right_item.work_id, set()).update(right_segment.covers)
    return suspects
class TranslationPackCoordinator:
    def __init__(self, worker):
        # Создает event-loop-local coalescer, который объединяет только одновременно готовые coverage-v2 translation jobs.
        self.worker = worker
        self._pending = []
        self._flush_task = None
        self._sequence = 0
    def _budget(self) -> PackingBudget:
        # Вычисляет soft multi-job envelope; hard context/output/RPD/RPM ограничения остаются в BaseApiHandler.
        model_config = getattr(self.worker, 'model_config', {}) or {}
        context_length = int(model_config.get('context_length', 0) or 0)
        max_output_tokens = int(model_config.get('max_output_tokens', 0) or 0)
        diagnostics = getattr(self.worker, 'run_diagnostics', None)
        telemetry_getter = getattr(diagnostics, 'packing_telemetry_snapshot', None)
        telemetry = telemetry_getter() if callable(telemetry_getter) else None
        target_atoms = adaptive_translation_target(telemetry, default_target=80, minimum=32, maximum=80)
        max_source_chars = 24000 if context_length <= 0 else max(8000, min(32000, context_length // 7))
        max_output_chars = 48000 if max_output_tokens <= 0 else max(12000, min(72000, max_output_tokens * 2))
        return PackingBudget(
            max_jobs=8, max_source_chars=max_source_chars, max_expected_output_chars=max_output_chars,
            soft_target_source_chars=int(max_source_chars * 0.72), max_atoms=target_atoms,
        )
    async def submit(self, *, source_html: str, source_units: list[dict], chapter_path: str, source_unit_ids=None, log_prefix: str = '', use_stream: bool = False, job_id: str | None = None):
        # Регистрирует translation job и сохраняет caller-owned leaf identity при split/recovery.
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._sequence += 1
        pack_item_id = f'r{self._sequence:06d}'
        if job_id is None:
            job_id = f'j{self._sequence:06d}'
        else:
            job_id = str(job_id)
        source_chars = sum(len(str(item.get('source_text') or '')) for item in source_units)
        diagnostics = getattr(self.worker, 'run_diagnostics', None)
        task_snapshot_getter = getattr(diagnostics, 'current_task_snapshot', None)
        item = {
            'pack_item_id': pack_item_id, 'job_id': job_id, 'source_html': source_html, 'source_units': list(source_units),
            'chapter_path': chapter_path, 'source_unit_ids': list(source_unit_ids) if source_unit_ids is not None else None,
            'log_prefix': log_prefix, 'use_stream': bool(use_stream), 'future': future, 'source_chars': source_chars,
            'diagnostic_task': task_snapshot_getter() if callable(task_snapshot_getter) else None,
        }
        self._pending.append(item)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_next_turn())
        return await future
    async def _flush_next_turn(self):
        # Коалесцирует jobs только до следующего event-loop turn и затем dispatch-ит их без fixed batching delay.
        await asyncio.sleep(0)
        pending = self._pending
        self._pending = []
        try:
            await self._dispatch(pending)
        finally:
            self._flush_task = None
            if self._pending:
                self._flush_task = asyncio.create_task(self._flush_next_turn())
    async def cancel_pending(self):
        # Отменяет pending/active coalescer task и дожидается ее завершения, запрещая detached provider start после worker stop.
        pending = self._pending
        self._pending = []
        for item in pending:
            future = item.get('future')
            if future is not None and not future.done():
                future.cancel()
        task = self._flush_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._flush_task = None
    def _work_item(self, pending: dict) -> WorkItem:
        # Превращает pending translation submission в packer work item без потери job/chapter identity.
        atom_count = len(pending['source_units'])
        source_chars = int(pending['source_chars'])
        return WorkItem(
            work_id=pending['pack_item_id'], kind='translation', job_id=pending['job_id'],
            chapter_id=pending['chapter_path'], payload=pending, source_chars=source_chars,
            expected_output_chars=max(512, source_chars * 2), atom_count=atom_count,
            closes_parent=True, age_seconds=0.0,
        )
    async def _dispatch(self, pending: list[dict]):
        # Формирует multi-job packs для маленьких tails, а крупные jobs оставляет одиночными.
        if not pending:
            return
        budget = self._budget()
        work_items = [self._work_item(item) for item in pending if not item['future'].cancelled()]
        if not work_items:
            return
        tail_limit = max(16, budget.max_atoms // 2)
        tails = [item for item in work_items if item.atom_count <= tail_limit]
        large = [item for item in work_items if item.atom_count > tail_limit]
        packs = pack_work_items(tails, budget, pack_prefix=f'translation-{getattr(self.worker, "worker_id", "worker")[-4:]}')
        for item in large:
            packs.append(RequestPack(f'translation-single-{item.work_id}', 'translation', [item], item.source_chars, item.expected_output_chars))
        for pack in packs:
            await self._execute_pack(pack)
    def _subpack(self, pack: RequestPack, items: list[WorkItem], suffix: str) -> RequestPack:
        # Создает deterministic половину envelope-blocked pack без повторного claim или изменения work identity.
        return RequestPack(
            f'{pack.pack_id}-{suffix}', pack.kind, list(items),
            sum(item.source_chars for item in items), sum(item.expected_output_chars for item in items),
        )
    async def _execute_pack(self, pack: RequestPack):
        # Выполняет один translation RequestPack, salvages jobs независимо и локально split-ит только pre-provider envelope block.
        if not pack.items:
            return
        jobs = [
            {
                'job_id': item.work_id, 'local_job_id': item.job_id,
                'source_html': item.payload['source_html'], 'source_units': item.payload['source_units'],
                'chapter_path': item.chapter_id, 'source_unit_ids': item.payload.get('source_unit_ids'),
            }
            for item in pack.items
        ]
        prompt_builder = self.worker.prompt_builder
        prompt, atoms_by_job, _ = prompt_builder.prepare_translation_pack_for_api(jobs)
        diagnostics = getattr(self.worker, 'run_diagnostics', None)
        use_stream = pack.job_count == 1 and bool(pack.items[0].payload.get('use_stream'))
        diagnostic_task_token = None
        if diagnostics:
            child_pairs = [
                (item, item.payload.get('diagnostic_task'))
                for item in pack.items
                if isinstance(item.payload.get('diagnostic_task'), dict)
            ]
            if pack.job_count == 1 and child_pairs:
                diagnostic_task = child_pairs[0][1]
            else:
                diagnostic_task = {
                    'task_id': f'pack:{pack.pack_id}',
                    'task_type': 'translation_pack',
                    'payload': (
                        'translation_pack',
                        {
                            'children': [
                                {
                                    'task_id': task.get('task_id'),
                                    'task_type': task.get('task_type'),
                                    'chapter_path': item.chapter_id,
                                }
                                for item, task in child_pairs
                            ],
                            'chapters': [item.chapter_id for item in pack.items],
                        },
                    ),
                }
            binder = getattr(diagnostics, 'bind_task_snapshot', None)
            diagnostic_task_token = binder(diagnostic_task) if callable(binder) else None
        try:
            try:
                raw_response = await self.worker.api_handler_instance.execute_api_call(
                    prompt, f'[TRANSLATION-PACK] jobs={pack.job_count} atoms={pack.atom_count}',
                    allow_incomplete=False, use_stream=use_stream, request_role='translation',
                )
            finally:
                if diagnostics:
                    resetter = getattr(diagnostics, 'reset_task', None)
                    if callable(resetter):
                        resetter(diagnostic_task_token)
            call_id = diagnostics.current_api_call_id() if diagnostics else None
            coverages = reconcile_multi_job_coverage(raw_response, atoms_by_job)
            rebound_coverages = {}
            scope_errors = {}
            for item in pack.items:
                try:
                    expected_atoms = build_source_atoms(
                        item.payload['source_units'], job_id=item.job_id, chapter_id=item.chapter_id,
                        source_unit_ids=item.payload.get('source_unit_ids'),
                    )
                    rebound_coverages[item.work_id] = rebind_coverage_to_expected_scope(coverages[item.work_id], expected_atoms)
                except (KeyError, ProvenanceConflictError) as exc:
                    scope_errors[item.work_id] = exc if isinstance(exc, ProvenanceConflictError) else ProvenanceConflictError(
                        f'INTERNAL_SCOPE_MISMATCH: provider response не содержит transport item {item.work_id!r}.'
                    )
            contamination = _translation_pack_contamination_suspects(pack, rebound_coverages)
            for work_id, atom_ids in contamination.items():
                rebound_coverages[work_id] = mark_coverage_atoms_for_repair(
                    rebound_coverages[work_id], atom_ids, 'cross_item_contamination_suspected',
                )
            if diagnostics:
                diagnostics.annotate_api_call(
                    call_id, stage='request_pack', status='accepted', decision='translation_pack',
                    details={
                        'pack_id': pack.pack_id, 'pack_kind': 'translation', 'job_count': pack.job_count,
                        'atom_count': pack.atom_count, 'source_chars': pack.source_chars,
                        'pack_item_ids': [item.work_id for item in pack.items],
                        'job_ids': [item.job_id for item in pack.items], 'chapters': [item.chapter_id for item in pack.items],
                        'scope_mismatch_count': len(scope_errors), 'contamination_suspect_items': len(contamination),
                    },
                )
            for item in pack.items:
                future = item.payload['future']
                if future.done():
                    continue
                error = scope_errors.get(item.work_id)
                if error is not None:
                    if diagnostics:
                        diagnostics.annotate_api_call(
                            call_id, stage='scope_validation', status='rejected', reason=str(error), decision='fail_closed_scope_mismatch',
                            details={'pack_id': pack.pack_id, 'pack_item_id': item.work_id, 'local_job_id': item.job_id, 'chapter': item.chapter_id},
                        )
                    future.set_exception(error)
                    continue
                future.set_result((rebound_coverages[item.work_id], call_id))
        except PartialGenerationError as exc:
            call_id = diagnostics.current_api_call_id() if diagnostics else None
            partial = str(getattr(exc, 'partial_text', '') or '')
            coverages = reconcile_multi_job_coverage(partial, atoms_by_job) if partial else {}
            setattr(exc, 'diagnostic_call_id', call_id)
            rebound_coverages = {}
            scope_errors = {}
            for item in pack.items:
                coverage = coverages.get(item.work_id)
                if coverage is None or not coverage.segments:
                    continue
                try:
                    expected_atoms = build_source_atoms(
                        item.payload['source_units'], job_id=item.job_id, chapter_id=item.chapter_id,
                        source_unit_ids=item.payload.get('source_unit_ids'),
                    )
                    rebound_coverages[item.work_id] = rebind_coverage_to_expected_scope(coverage, expected_atoms)
                except ProvenanceConflictError as scope_error:
                    scope_errors[item.work_id] = scope_error
            contamination = _translation_pack_contamination_suspects(pack, rebound_coverages)
            for work_id, atom_ids in contamination.items():
                rebound_coverages[work_id] = mark_coverage_atoms_for_repair(
                    rebound_coverages[work_id], atom_ids, 'cross_item_contamination_suspected',
                )
            for item in pack.items:
                future = item.payload['future']
                if future.done():
                    continue
                scope_error = scope_errors.get(item.work_id)
                if scope_error is not None:
                    future.set_exception(scope_error)
                elif item.work_id in rebound_coverages:
                    future.set_result((rebound_coverages[item.work_id], call_id))
                else:
                    future.set_exception(exc)
            exc.partial_text = ''
        except ValidationFailedError as exc:
            call_id = diagnostics.current_api_call_id() if diagnostics else None
            setattr(exc, 'diagnostic_call_id', call_id)
            split = split_request_pack(pack) if is_pre_provider_envelope_failure(exc) else ()
            if split:
                left, right = split
                await self._execute_pack(left)
                await self._execute_pack(right)
                return
            for item in pack.items:
                future = item.payload['future']
                if not future.done():
                    future.set_exception(exc)
        except BaseException as exc:
            call_id = diagnostics.current_api_call_id() if diagnostics else None
            if call_id is not None and not hasattr(exc, 'diagnostic_call_id'):
                setattr(exc, 'diagnostic_call_id', call_id)
            for item in pack.items:
                future = item.payload['future']
                if not future.done():
                    future.set_exception(exc)
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
def adaptive_translation_target(telemetry: dict | None, *, default_target: int = 60, minimum: int = 32, maximum: int = 96) -> int:
    # Выбирает soft unit target по durable-units/provider-starts, сохраняя безопасный data-poor default.
    buckets = (telemetry or {}).get('translation_size_buckets') if isinstance(telemetry, dict) else None
    if not isinstance(buckets, dict):
        return max(minimum, min(maximum, default_target))
    candidates = []
    for label, stats in buckets.items():
        if not isinstance(stats, dict) or int(stats.get('provider_starts', 0) or 0) < 5:
            continue
        durable = float(stats.get('durable_atoms', 0) or 0)
        starts = float(stats.get('provider_starts', 0) or 0) + float(stats.get('child_provider_starts', 0) or 0)
        target = int(stats.get('target_units', 0) or 0)
        if starts > 0 and target > 0:
            candidates.append((durable / starts, target))
    if not candidates:
        return max(minimum, min(maximum, default_target))
    _, target = max(candidates)
    return max(minimum, min(maximum, target))
def estimate_prompt_tokens(prompt: str) -> int:
    # Оценивает token cost pack-а только для soft packing; hard envelope остается в API admission layer.
    return int(TokenCounter().estimate_tokens(prompt))
def estimated_repair_runway(pending_debt_count: int, pending_source_chars: int, budget: PackingBudget, *, by_kind: dict | None = None, source_chars_by_kind: dict | None = None) -> int:
    # Оценивает provider slots отдельно по несовместимым debt kinds, чтобы missing/alignment reserve нельзя было схлопнуть в один запрос.
    if pending_debt_count <= 0:
        return 0
    normalized_kinds = {str(kind): max(0, int(count or 0)) for kind, count in (by_kind or {}).items() if int(count or 0) > 0}
    if not normalized_kinds:
        by_jobs = math.ceil(pending_debt_count / max(1, budget.max_jobs))
        by_chars = math.ceil(max(0, pending_source_chars) / max(1, budget.max_source_chars))
        return max(1, by_jobs, by_chars)
    chars_map = {str(kind): max(0, int(value or 0)) for kind, value in (source_chars_by_kind or {}).items()}
    runway = 0
    for kind, count in normalized_kinds.items():
        kind_chars = chars_map.get(kind, 0)
        by_jobs = math.ceil(count / max(1, budget.max_jobs))
        by_chars = math.ceil(kind_chars / max(1, budget.max_source_chars)) if kind_chars else 1
        runway += max(1, by_jobs, by_chars)
    return runway
