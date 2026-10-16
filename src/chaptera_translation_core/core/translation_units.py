from __future__ import annotations
import hashlib
import json
import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from bs4 import BeautifulSoup, Comment, NavigableString
from chaptera_translation_core.api.errors import ProvenanceConflictError
from .translation_text_salvage import detect_markup_leak, salvage_translation_text
_CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002FA1F]')
_MEANINGFUL_TARGET_RE = re.compile(r'[0-9A-Za-zА-Яа-яЁё\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002FA1F]')
def has_meaningful_translation_text(value: str) -> bool:
    # Проверяет, что target содержит смысловой буквенно-цифровой символ, а не одни знаки пунктуации.
    return bool(_MEANINGFUL_TARGET_RE.search(str(value or '')))
@dataclass(frozen=True)
class SourceAtom:
    job_id: str
    atom_id: str
    source_text: str
    order: int
    chapter_id: str = ''
    render_domain_id: str = ''
    source_unit_id: str | None = None
@dataclass(frozen=True)
class SemanticGroup:
    group_id: str
    job_id: str
    atom_ids: tuple[str, ...]
    source_text: str
@dataclass(frozen=True)
class TargetSegment:
    job_id: str
    segment_id: str
    covers: tuple[str, ...]
    translation: str
    origin: str = 'translation'
@dataclass
class CoverageMap:
    job_id: str
    atoms: tuple[SourceAtom, ...]
    segments: list[TargetSegment] = field(default_factory=list)
    missing_atom_ids: list[str] = field(default_factory=list)
    invalid_segments: list[dict] = field(default_factory=list)
    legacy_empty_atom_ids: list[str] = field(default_factory=list)
    @property
    def complete(self) -> bool:
        # Возвращает True только когда каждый source atom доказанно покрыт ровно один раз.
        return not self.missing_atom_ids
    def segment_by_atom(self) -> dict[str, TargetSegment]:
        # Индексирует доказанные target segments по каждому покрытому source atom.
        result = {}
        for segment in self.segments:
            for atom_id in segment.covers:
                result[atom_id] = segment
        return result
@dataclass(frozen=True)
class RenderDebt:
    debt_kind: str
    job_id: str
    atom_ids: tuple[str, ...]
    source_texts: tuple[str, ...]
    current_translation: str
    reason: str
@dataclass
class RenderPlan:
    rendered_html: str | None
    debts: list[RenderDebt]
def expected_render_domain_rows(atoms: list[SourceAtom] | tuple[SourceAtom, ...]) -> list[tuple[str, tuple[str, ...]]]:
    # Single source of truth for the source-owned segmentation contract used by both prompt and reconciler.
    # Empty render_domain_id values are intentionally singleton rows: an unknown DOM owner is never merge authority.
    rows: list[tuple[str, tuple[str, ...]]] = []
    current_domain = None
    current_atom_ids: list[str] = []
    for atom in atoms:
        domain_id = str(atom.render_domain_id or '')
        same_domain = bool(current_atom_ids) and bool(domain_id) and bool(current_domain) and domain_id == current_domain
        if current_atom_ids and not same_domain:
            rows.append((str(current_domain or ''), tuple(current_atom_ids)))
            current_atom_ids = []
        if not current_atom_ids:
            current_domain = domain_id
        current_atom_ids.append(str(atom.atom_id))
    if current_atom_ids:
        rows.append((str(current_domain or ''), tuple(current_atom_ids)))
    return rows
def expected_render_domain_covers(atoms: list[SourceAtom] | tuple[SourceAtom, ...]) -> list[tuple[str, ...]]:
    return [covers for _domain_id, covers in expected_render_domain_rows(atoms)]
def coverage_matches_render_domain_contract(coverage: CoverageMap) -> bool:
    # Proves that the final target segmentation is exactly source-owned, independent of provider-supplied segment IDs.
    expected = expected_render_domain_covers(coverage.atoms)
    actual = [tuple(segment.covers) for segment in coverage.segments]
    return coverage.complete and actual == expected
def _decode_json_candidate(candidate: str):
    # Декодирует один JSON/JSONL объект, разрешая только безопасный структурный хвост.
    candidate = (candidate or '').strip().rstrip(',')
    if candidate in {'', '[', ']', '{', '}'}:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    try:
        value, end = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        return None
    remainder = candidate[end:].strip()
    if remainder and re.search(r'[0-9A-Za-zА-Яа-яЁё\u3400-\u9fff]', remainder):
        return None
    return value if not remainder or re.fullmatch(r"[\s,\]\}\"']+", remainder) else None
def _decode_adjacent_json_objects(candidate: str) -> list[dict]:
    # Декодирует несколько валидных JSON objects, случайно склеенных как `}{`.
    # Любой иной мусор оставляем malformed вместо попытки угадывать payload.
    source = str(candidate or '').strip()
    decoder = json.JSONDecoder()
    index = 0
    values = []
    while index < len(source):
        while index < len(source) and (source[index].isspace() or source[index] == ','):
            index += 1
        if index >= len(source):
            break
        try:
            value, end = decoder.raw_decode(source, index)
        except json.JSONDecodeError:
            return []
        if not isinstance(value, dict):
            return []
        values.append(value)
        index = end
    return values if len(values) > 1 else []
def _iter_response_items(raw_text: str) -> tuple[list, list[int]]:
    # Извлекает JSON objects из array/object/JSONL и отдельно фиксирует битые строки.
    text = (raw_text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^\s*```(?:json|jsonl)?\s*|\s*```\s*$', '', text, flags=re.IGNORECASE).strip()
    malformed = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get('segments'), list):
        return list(parsed['segments']), malformed
    if isinstance(parsed, dict) and isinstance(parsed.get('translations'), list):
        return list(parsed['translations']), malformed
    if isinstance(parsed, list):
        return list(parsed), malformed
    if isinstance(parsed, dict) and ('covers' in parsed or 'unit_id' in parsed):
        return [parsed], malformed
    items = []
    for line_index, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.strip().startswith('```'):
            continue
        item = _decode_json_candidate(line)
        if item is None:
            adjacent = _decode_adjacent_json_objects(line)
            if adjacent:
                items.extend(adjacent)
                continue
            malformed.append(line_index)
            continue
        items.append(item)
    return items, malformed
def build_source_atoms(source_units: list[dict], *, job_id: str = 'j0', chapter_id: str = '', source_unit_ids: list[str] | None = None) -> list[SourceAtom]:
    # Превращает provider payload в immutable source atoms с независимой source identity.
    if source_unit_ids is not None and len(source_unit_ids) != len(source_units):
        raise ValueError('source_unit_ids не покрывают source_units 1:1 до provider call.')
    atoms = []
    for index, item in enumerate(source_units):
        atom_id = str(item['unit_id'])
        atoms.append(SourceAtom(
            job_id=job_id,
            atom_id=atom_id,
            source_text=str(item.get('source_text') or ''),
            order=index,
            chapter_id=chapter_id,
            render_domain_id=str(item.get('render_domain_id') or ''),
            source_unit_id=source_unit_ids[index] if source_unit_ids is not None else item.get('source_unit_id'),
        ))
    return atoms
def _source_atom_identity(atom: SourceAtom, *, include_job_id: bool = True) -> dict:
    # Материализует immutable provenance одного source atom для exact scope comparison.
    identity = {
        'atom_id': atom.atom_id,
        'source_hash': hashlib.sha256(atom.source_text.encode('utf-8')).hexdigest(),
        'order': int(atom.order),
        'chapter_id': atom.chapter_id,
        'render_domain_id': atom.render_domain_id,
        'source_unit_id': atom.source_unit_id,
    }
    if include_job_id:
        identity['job_id'] = atom.job_id
    return identity
def assert_coverage_scope(coverage: CoverageMap, expected_atoms: list[SourceAtom] | tuple[SourceAtom, ...]) -> None:
    # Fail-closed запрещает render/debt, если CoverageMap не принадлежит exact caller-owned source scope.
    expected_atoms = tuple(expected_atoms)
    expected_job_id = expected_atoms[0].job_id if expected_atoms else coverage.job_id
    if coverage.job_id != expected_job_id:
        raise ProvenanceConflictError(f'INTERNAL_SCOPE_MISMATCH: coverage job_id={coverage.job_id!r}, expected={expected_job_id!r}.')
    actual_scope = [_source_atom_identity(atom) for atom in coverage.atoms]
    expected_scope = [_source_atom_identity(atom) for atom in expected_atoms]
    if actual_scope != expected_scope:
        raise ProvenanceConflictError('INTERNAL_SCOPE_MISMATCH: CoverageMap source atoms не совпадают с caller-owned source scope.')
    if any(segment.job_id != expected_job_id for segment in coverage.segments):
        raise ProvenanceConflictError('INTERNAL_SCOPE_MISMATCH: target segment принадлежит другому local job_id.')
def rebind_coverage_to_expected_scope(coverage: CoverageMap, expected_atoms: list[SourceAtom] | tuple[SourceAtom, ...]) -> CoverageMap:
    # Переводит request-local provider identity обратно в caller local job_id только после exact source provenance check.
    expected_atoms = tuple(expected_atoms)
    if len(coverage.atoms) != len(expected_atoms):
        raise ProvenanceConflictError('INTERNAL_SCOPE_MISMATCH: provider coverage atom count не совпадает с caller scope.')
    actual_scope = [_source_atom_identity(atom, include_job_id=False) for atom in coverage.atoms]
    expected_scope = [_source_atom_identity(atom, include_job_id=False) for atom in expected_atoms]
    if actual_scope != expected_scope:
        raise ProvenanceConflictError('INTERNAL_SCOPE_MISMATCH: provider coverage содержит source provenance другого work item.')
    local_job_id = expected_atoms[0].job_id if expected_atoms else coverage.job_id
    segments = [
        TargetSegment(local_job_id, segment.segment_id, segment.covers, segment.translation, segment.origin)
        for segment in coverage.segments
    ]
    rebound = CoverageMap(
        local_job_id, expected_atoms, segments, list(coverage.missing_atom_ids),
        list(coverage.invalid_segments), list(coverage.legacy_empty_atom_ids),
    )
    assert_coverage_scope(rebound, expected_atoms)
    return rebound
def build_semantic_groups(atoms: list[SourceAtom], max_atoms: int = 8, max_chars: int = 1400) -> list[SemanticGroup]:
    # Группирует соседние atoms только внутри одного render domain.
    # render_domain_id — hard DOM boundary: если semantic_group пересекает его, модель получает
    # противоречивый сигнал "свяжи эти atoms", а renderer затем обязан создать alignment debt.
    groups = []
    current = []
    current_chars = 0
    for atom in atoms:
        atom_chars = len(atom.source_text)
        domain_changed = bool(current) and (
            not atom.render_domain_id
            or not current[-1].render_domain_id
            or atom.render_domain_id != current[-1].render_domain_id
        )
        if current and (domain_changed or len(current) >= max_atoms or current_chars + atom_chars > max_chars):
            group_index = len(groups)
            groups.append(SemanticGroup(f'g{group_index:04d}', current[0].job_id, tuple(item.atom_id for item in current), '\n'.join(item.source_text for item in current)))
            current = []
            current_chars = 0
        current.append(atom)
        current_chars += atom_chars
    if current:
        group_index = len(groups)
        groups.append(SemanticGroup(f'g{group_index:04d}', current[0].job_id, tuple(item.atom_id for item in current), '\n'.join(item.source_text for item in current)))
    return groups
def serialize_source_atoms_for_prompt(
    atoms: list[SourceAtom],
    groups: list[SemanticGroup] | None = None,
    *,
    include_job_id: bool = False,
) -> str:
    # Сериализует source atoms columnar-v1: имена полей передаются один раз, а rows сохраняют полный порядок/identity.
    group_by_atom = {}
    for group in groups or ():
        for atom_id in group.atom_ids:
            group_id = f'{group.job_id}:{group.group_id}' if include_job_id else group.group_id
            group_by_atom[(group.job_id, atom_id)] = group_id
    columns = ['atom_id', 'semantic_group_id', 'render_domain_id', 'source_text']
    if include_job_id:
        columns.insert(0, 'job_id')
    rows = []
    for atom in atoms:
        row = [
            atom.atom_id,
            group_by_atom.get((atom.job_id, atom.atom_id)),
            atom.render_domain_id,
            atom.source_text,
        ]
        if include_job_id:
            row.insert(0, atom.job_id)
        rows.append(row)
    payload = {'columns': columns, 'rows': rows}
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
def _normalize_provider_covers(covers: object, expected_index: dict[str, int]) -> list[str] | None:
    # Локально чинит только однозначный provider formatting drift: ["000001,000002"] -> ["000001", "000002"].
    # Если хотя бы одна comma-part неизвестна caller-owned source scope, ничего не угадываем и оставляем strict rejection ниже.
    if isinstance(covers, str):
        values = [covers]
    elif isinstance(covers, list):
        values = covers
    else:
        return None
    if not values:
        return []
    normalized: list[str] = []
    for value in values:
        atom_id = str(value).strip()
        if atom_id in expected_index or ',' not in atom_id:
            normalized.append(atom_id)
            continue
        parts = [part.strip() for part in atom_id.split(',')]
        if parts and all(part and part in expected_index for part in parts):
            normalized.extend(parts)
        else:
            normalized.append(atom_id)
    return normalized
def reconcile_coverage_response(
    raw_text: str,
    atoms: list[SourceAtom],
    *,
    default_job_id: str = 'j0',
    enforce_render_domain_contract: bool = False,
) -> CoverageMap:
    # Salvage-ит доказанные target segments и отличает explicit coverage от legacy unit JSON без догадок о смысле.
    if not atoms:
        return CoverageMap(default_job_id, tuple())
    expected_order = [atom.atom_id for atom in atoms]
    expected_index = {atom_id: index for index, atom_id in enumerate(expected_order)}
    expected_contract_rows = expected_render_domain_covers(atoms) if enforce_render_domain_contract else []
    expected_contract_set = set(expected_contract_rows)
    source_by_id = {atom.atom_id: atom.source_text for atom in atoms}
    items, malformed_lines = _iter_response_items(raw_text)
    result = CoverageMap(default_job_id, tuple(atoms))
    if malformed_lines:
        result.invalid_segments.append({'reason': 'malformed_lines', 'lines': malformed_lines})
    covered = set()
    for item in items:
        if not isinstance(item, dict):
            result.invalid_segments.append({'reason': 'non_object'})
            continue
        job_id = default_job_id
        legacy_unit_id = item.get('unit_id')
        covers = item.get('covers')
        if covers is None and legacy_unit_id is not None:
            covers = [legacy_unit_id]
        covers = _normalize_provider_covers(covers, expected_index)
        if not covers:
            result.invalid_segments.append({'reason': 'missing_covers'})
            continue
        translation = item.get('translation')
        if not isinstance(translation, str) or not translation.strip():
            if legacy_unit_id in expected_index:
                result.legacy_empty_atom_ids.append(str(legacy_unit_id))
            continue
        source_scope = ''.join(source_by_id.get(atom_id, '') for atom_id in covers if atom_id in source_by_id)
        salvaged = salvage_translation_text(translation, source_scope)
        if not salvaged.safe or detect_markup_leak(salvaged.text, source_scope):
            result.invalid_segments.append({'reason': 'forbidden_translation_structure', 'covers': covers})
            continue
        if salvaged.changed:
            result.invalid_segments.append({
                'reason': 'local_translation_salvage',
                'covers': covers,
                'salvage_reasons': list(salvaged.reasons),
            })
        translation = salvaged.text
        if re.search(r'\b(?:TSEG|TSRC)_', translation, flags=re.IGNORECASE):
            result.invalid_segments.append({'reason': 'forbidden_translation_structure', 'covers': covers})
            continue
        if _CJK_RE.search(translation):
            result.invalid_segments.append({'reason': 'cjk_residue_advisory', 'covers': covers})
        # Пунктуационный source atom (например, «……»/«—») не обязан внезапно
        # превращаться в буквенно-цифровой текст. Раньше любой punctuation-only
        # target считался meaningless, из-за чего корректные «…» создавали
        # ложный missing debt и затем трижды гонялись через recovery.
        if has_meaningful_translation_text(source_scope) and not has_meaningful_translation_text(translation):
            result.invalid_segments.append({'reason': 'meaningless_translation', 'covers': covers})
            continue
        if any(atom_id not in expected_index for atom_id in covers):
            result.invalid_segments.append({'reason': 'unknown_atom_id', 'covers': covers})
            continue
        positions = [expected_index[atom_id] for atom_id in covers]
        if positions != list(range(min(positions), max(positions) + 1)):
            result.invalid_segments.append({'reason': 'non_contiguous_coverage', 'covers': covers})
            continue
        if enforce_render_domain_contract and tuple(covers) not in expected_contract_set:
            result.invalid_segments.append({
                'reason': 'render_domain_contract_mismatch',
                'covers': covers,
                'expected_rows': [list(row) for row in expected_contract_rows],
            })
            continue
        if any(atom_id in covered for atom_id in covers):
            # Keep the first already-proven source->target assignment and reject only the later
            # overlapping claim. Blanketing both sides used to erase a correct earlier segment
            # and manufacture a missing-debt that recovery could rewrite semantically (real ch14).
            overlapping = sorted(set(covers).intersection(covered), key=lambda atom_id: expected_index.get(atom_id, 10**9))
            previous_covers = [
                list(previous.covers) for previous in result.segments
                if set(previous.covers).intersection(overlapping)
            ]
            result.invalid_segments.append({
                'reason': 'overlapping_coverage_later_segment_rejected',
                'covers': covers,
                'overlapping_atoms': overlapping,
                'preserved_previous_covers': previous_covers,
            })
            continue
        segment_id = f's{len(result.segments):06d}'
        covered.update(covers)
        result.segments.append(TargetSegment(job_id, segment_id, tuple(covers), translation.strip(), 'translation'))
    covered = {atom_id for segment in result.segments for atom_id in segment.covers}
    result.segments.sort(key=lambda segment: min(expected_index[atom_id] for atom_id in segment.covers))
    result.missing_atom_ids = [atom_id for atom_id in expected_order if atom_id not in covered]
    return result
def _single_edit_distance_candidate(value: str, candidates: set[str]) -> str | None:
    # Conservatively salvages one-character transport-ID drift only when exactly one caller-owned candidate matches.
    # This is deliberately narrower than fuzzy matching: the model never gets authority to invent routing.
    def within_one(left: str, right: str) -> bool:
        if abs(len(left) - len(right)) > 1:
            return False
        if len(left) == len(right):
            return sum(a != b for a, b in zip(left, right)) <= 1
        shorter, longer = (left, right) if len(left) < len(right) else (right, left)
        i = j = edits = 0
        while i < len(shorter) and j < len(longer):
            if shorter[i] == longer[j]:
                i += 1
                j += 1
                continue
            edits += 1
            if edits > 1:
                return False
            j += 1
        return True
    matches = [candidate for candidate in candidates if within_one(value, candidate)]
    return matches[0] if len(matches) == 1 else None
def reconcile_multi_job_coverage(
    raw_text: str,
    atoms_by_job: dict[str, list[SourceAtom]],
    *,
    enforce_render_domain_contract: bool = True,
) -> dict[str, CoverageMap]:
    # Разделяет один multi-job provider response по job_id и применяет тот же strict coverage salvage независимо к каждому job.
    items, malformed_lines = _iter_response_items(raw_text)
    job_ids = set(atoms_by_job)
    grouped = {job_id: [] for job_id in atoms_by_job}
    local_invalid = {job_id: [] for job_id in atoms_by_job}
    global_invalid = []
    single_job_id = next(iter(job_ids)) if len(job_ids) == 1 else None
    for item in items:
        if not isinstance(item, dict):
            global_invalid.append({'reason': 'non_object'})
            continue
        job_id = single_job_id if single_job_id is not None else str(item.get('job_id') or '')
        if job_id not in grouped:
            salvaged_job_id = _single_edit_distance_candidate(job_id, job_ids) if job_id else None
            if salvaged_job_id is None:
                global_invalid.append({'reason': 'unknown_or_missing_job_id', 'job_id': job_id})
                continue
            local_invalid[salvaged_job_id].append({
                'reason': 'job_id_transport_salvage',
                'provider_job_id': job_id,
                'resolved_job_id': salvaged_job_id,
            })
            job_id = salvaged_job_id
        grouped[job_id].append(item)
    results = {}
    for job_id, atoms in atoms_by_job.items():
        job_text = json.dumps(grouped[job_id], ensure_ascii=False, separators=(',', ':'))
        coverage = reconcile_coverage_response(
            job_text,
            atoms,
            default_job_id=job_id,
            enforce_render_domain_contract=enforce_render_domain_contract,
        )
        if malformed_lines:
            coverage.invalid_segments.append({'reason': 'global_malformed_lines', 'lines': malformed_lines})
        if global_invalid:
            coverage.invalid_segments.extend(global_invalid)
        if local_invalid[job_id]:
            coverage.invalid_segments.extend(local_invalid[job_id])
        results[job_id] = coverage
    return results
def _marked_atom_nodes(marked_source: str) -> tuple[BeautifulSoup, dict[str, NavigableString]]:
    # Находит source text node каждого TSEG, строго пропуская optional TSRC companion.
    soup = BeautifulSoup(marked_source or '', 'html.parser')
    nodes = {}
    for marker in soup.find_all(string=True):
        if not isinstance(marker, Comment):
            continue
        match = re.fullmatch(r'\s*TSEG_(\d{6})\s*', str(marker))
        if not match:
            continue
        target = marker.next_sibling
        if isinstance(target, Comment) and re.fullmatch(r'\s*TSRC_[0-9a-fA-F]{12}\s*', str(target)):
            target = target.next_sibling
        if not isinstance(target, NavigableString) or isinstance(target, Comment):
            raise ValueError(f'Не найден immutable source text node для atom {match.group(1)}.')
        nodes[match.group(1)] = target
    return soup, nodes
def _normalized_duplicate_text(value: str) -> str:
    # Нормализует target только для консервативной deterministic проверки соседних дублей.
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё\u3400-\u9fff\uf900-\ufaff]+", " ", str(value or "").lower().replace("ё", "е")).strip()
def _near_duplicate_target(left: str, right: str) -> bool:
    # Ловит только почти идентичные варианты; намеренно не пытается семантически угадывать парафразы.
    left_norm = _normalized_duplicate_text(left)
    right_norm = _normalized_duplicate_text(right)
    if min(len(left_norm), len(right_norm)) < 36:
        return False
    if left_norm == right_norm:
        return True
    shorter, longer = sorted((left_norm, right_norm), key=len)
    if len(shorter) >= 48 and shorter in longer and len(shorter) / max(1, len(longer)) >= 0.78:
        return True
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    if ratio >= 0.90:
        return True
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if min(len(left_tokens), len(right_tokens)) < 6:
        return False
    jaccard = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    return jaccard >= 0.86
def _token_shingles(tokens: list[str], size: int = 3) -> set[tuple[str, ...]]:
    # Строит короткие word shingles только для deterministic overlap detector без внешней semantic модели.
    if len(tokens) < size:
        return set()
    return {tuple(tokens[index:index + size]) for index in range(len(tokens) - size + 1)}
def _sources_allow_same_target(left_source: str, right_source: str) -> bool:
    # Явно разрешает identical/near-identical source repetition, чтобы recovery не стирал авторские повторы.
    left_source_norm = _normalized_duplicate_text(left_source)
    right_source_norm = _normalized_duplicate_text(right_source)
    if not left_source_norm or not right_source_norm:
        return False
    if left_source_norm == right_source_norm:
        return True
    shorter_source, longer_source = sorted((left_source_norm, right_source_norm), key=len)
    # SequenceMatcher autojunk returns spurious near-zero similarity for very
    # long, highly repetitive CJK prose. Exact source containment is explicit
    # evidence that identical Russian repetitions can be intentional.
    if len(shorter_source) >= 24 and (longer_source.startswith(shorter_source) or longer_source.endswith(shorter_source)):
        return True
    similarity = SequenceMatcher(None, shorter_source, longer_source).ratio()
    # Preserve the pre-R15.1.5 allowance for long near-identical source repetition.
    if len(shorter_source) >= 24 and similarity >= 0.92:
        return True
    # Short source repetition gets a stricter threshold because tiny phrases collide easily.
    if len(shorter_source) >= 12 and similarity >= 0.96:
        return True
    return False
def _short_target_spillover_suspected(left_norm: str, right_norm: str) -> bool:
    # Ловит локальный boundary spillover. Для 9–12 слов разрешаем только строгий exact/prefix/suffix,
    # чтобы закрыть diagnostics4/ch5 и не превращать fuzzy guard в глобальный стилистический matcher.
    if min(len(left_norm), len(right_norm)) < 12:
        return False
    shorter, longer = sorted((left_norm, right_norm), key=len)
    short_tokens = shorter.split()
    long_tokens = longer.split()
    token_count = len(short_tokens)
    if token_count < 2 or token_count > 12:
        return False
    if shorter == longer:
        return True
    boundary_containment = shorter in longer and (longer.startswith(shorter) or longer.endswith(shorter))
    if boundary_containment and len(shorter) / max(1, len(longer)) >= 0.42:
        return True
    # Более свободная перестановка слов остаётся только для очень коротких attribution/reply (2–8 слов).
    if token_count > 8:
        return False
    if shorter in longer and len(shorter) / max(1, len(longer)) >= 0.42:
        return True
    short_set = set(short_tokens)
    long_set = set(long_tokens)
    token_containment = len(short_set & long_set) / max(1, len(short_set))
    ratio = SequenceMatcher(None, shorter, longer).ratio()
    # Разрешаем перестановку слов в короткой attribution: «кивнул Жирный монах» ↔ «Жирный монах кивнул».
    return token_containment >= 0.90 and ratio >= 0.44
def target_contamination_suspected(left: str, right: str, left_source: str = '', right_source: str = '', *, short_spillover: bool = False) -> bool:
    # Source-aware detector для short spillover и длинного paraphrased/contained target overlap.
    left_norm = _normalized_duplicate_text(left)
    right_norm = _normalized_duplicate_text(right)
    if not left_norm or not right_norm:
        return False
    if _sources_allow_same_target(left_source, right_source):
        return False
    if short_spillover and _short_target_spillover_suspected(left_norm, right_norm):
        return True
    if min(len(left_norm), len(right_norm)) < 60:
        return False
    if _near_duplicate_target(left, right):
        return True
    left_tokens = left_norm.split()
    right_tokens = right_norm.split()
    if min(len(left_tokens), len(right_tokens)) < 10:
        return False
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    intersection = len(left_set & right_set)
    jaccard = intersection / max(1, len(left_set | right_set))
    containment = intersection / max(1, min(len(left_set), len(right_set)))
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_shingles = _token_shingles(left_tokens)
    right_shingles = _token_shingles(right_tokens)
    shingle_containment = len(left_shingles & right_shingles) / max(1, min(len(left_shingles), len(right_shingles)))
    shorter, longer = sorted((left_norm, right_norm), key=len)
    if shorter in longer and len(shorter) / max(1, len(longer)) >= 0.58:
        return True
    return (
        (jaccard >= 0.62 and ratio >= 0.50)
        or (containment >= 0.64 and jaccard >= 0.44 and shingle_containment >= 0.20)
        or (shingle_containment >= 0.42 and jaccard >= 0.52)
    )
def mark_coverage_atoms_for_repair(coverage: CoverageMap, atom_ids: set[str], reason: str) -> CoverageMap:
    # Удаляет целые target segments, пересекающие suspect atoms, чтобы они ушли в targeted debt вместо silent commit.
    atom_ids = {str(atom_id) for atom_id in atom_ids}
    segments = [segment for segment in coverage.segments if not atom_ids.intersection(segment.covers)]
    covered = {atom_id for segment in segments for atom_id in segment.covers}
    missing = [atom.atom_id for atom in coverage.atoms if atom.atom_id not in covered]
    invalid = list(coverage.invalid_segments)
    invalid.append({'reason': reason, 'atom_ids': sorted(atom_ids)})
    return CoverageMap(coverage.job_id, coverage.atoms, segments, missing, invalid, list(coverage.legacy_empty_atom_ids))
def _recovery_duplicate_debts(coverage: CoverageMap) -> list[RenderDebt]:
    # Блокирует source-incompatible соседний target bleed, включая короткие attribution/reply и debt_repair↔debt_repair.
    if len(coverage.segments) < 2:
        return []
    atom_by_id = {atom.atom_id: atom for atom in coverage.atoms}
    order_by_id = {atom.atom_id: atom.order for atom in coverage.atoms}
    ordered = sorted(
        coverage.segments,
        key=lambda segment: min((order_by_id.get(atom_id, 10**9) for atom_id in segment.covers), default=10**9),
    )
    debts = []
    seen = set()
    for left, right in zip(ordered, ordered[1:]):
        left_orders = [order_by_id.get(atom_id) for atom_id in left.covers if atom_id in order_by_id]
        right_orders = [order_by_id.get(atom_id) for atom_id in right.covers if atom_id in order_by_id]
        if not left_orders or not right_orders or max(left_orders) + 1 != min(right_orders):
            continue
        left_source = ''.join(atom_by_id[atom_id].source_text for atom_id in left.covers if atom_id in atom_by_id)
        right_source = ''.join(atom_by_id[atom_id].source_text for atom_id in right.covers if atom_id in atom_by_id)
        allow_short_spillover = left.origin == 'debt_repair' or right.origin == 'debt_repair'
        if not target_contamination_suspected(
            left.translation, right.translation, left_source, right_source, short_spillover=allow_short_spillover
        ):
            continue
        repair_covers = tuple((*left.covers, *right.covers))
        signature = repair_covers
        if not signature or signature in seen:
            continue
        seen.add(signature)
        debts.append(RenderDebt(
            'completeness', coverage.job_id, repair_covers,
            tuple(atom_by_id[atom_id].source_text for atom_id in repair_covers if atom_id in atom_by_id),
            left.translation + '\n' + right.translation,
            'adjacent_target_contamination_suspected',
        ))
    return debts
def build_render_plan(marked_source: str, coverage: CoverageMap) -> RenderPlan:
    # Собирает все alignment debts даже рядом с missing atoms и рендерит HTML только при полной DOM-safe coverage.
    soup, nodes = _marked_atom_nodes(marked_source)
    source_by_id = {atom.atom_id: atom for atom in coverage.atoms}
    debts = _recovery_duplicate_debts(coverage)
    covered_ids = {atom_id for segment in coverage.segments for atom_id in segment.covers}
    segment_by_exact_covers = {tuple(segment.covers): segment for segment in coverage.segments}
    for expected_covers in expected_render_domain_covers(coverage.atoms):
        expected_set = set(expected_covers)
        # Missing coverage is already represented by missing debt; only fully-covered-but-wrong segmentation is alignment debt.
        if not expected_set.issubset(covered_ids) or expected_covers in segment_by_exact_covers:
            continue
        overlapping = [segment for segment in coverage.segments if expected_set.intersection(segment.covers)]
        current_translation = '\n'.join(segment.translation for segment in overlapping)
        debts.append(RenderDebt(
            'alignment', coverage.job_id, expected_covers,
            tuple(source_by_id[atom_id].source_text for atom_id in expected_covers),
            current_translation, 'render_domain_contract_mismatch',
        ))
    for segment in coverage.segments:
        segment_nodes = [nodes.get(atom_id) for atom_id in segment.covers]
        if any(node is None for node in segment_nodes):
            raise ValueError(f'Render plan не нашел source atom: {segment.covers}.')
        render_domains = {source_by_id[atom_id].render_domain_id for atom_id in segment.covers}
        if len(segment.covers) > 1 and (len(render_domains) != 1 or '' in render_domains):
            debts.append(RenderDebt(
                'alignment', segment.job_id, segment.covers,
                tuple(source_by_id[atom_id].source_text for atom_id in segment.covers),
                segment.translation, 'cross_render_domain_many_to_one',
            ))
            continue
        source_scope = ''.join(source_by_id[atom_id].source_text for atom_id in segment.covers)
        salvaged = salvage_translation_text(segment.translation, source_scope)
        if not salvaged.safe or detect_markup_leak(salvaged.text, source_scope):
            debts.append(RenderDebt(
                'completeness', segment.job_id, segment.covers,
                tuple(source_by_id[atom_id].source_text for atom_id in segment.covers),
                segment.translation, 'markup_leak_after_reconcile',
            ))
            continue
        first = segment_nodes[0]
        source_piece = str(first)
        leading = source_piece[:len(source_piece) - len(source_piece.lstrip())]
        trailing = source_piece[len(source_piece.rstrip()):]
        first.replace_with(NavigableString(leading + salvaged.text + trailing))
        for extra in segment_nodes[1:]:
            extra.replace_with(NavigableString(''))
    if coverage.missing_atom_ids or debts:
        return RenderPlan(None, debts)
    return RenderPlan(str(soup), [])
def build_missing_debts(coverage: CoverageMap) -> list[RenderDebt]:
    # Превращает только реально uncovered atoms в MissingDebt, не считая N:1 coverage пропуском.
    atom_by_id = {atom.atom_id: atom for atom in coverage.atoms}
    debts = []
    for atom_id in coverage.missing_atom_ids:
        atom = atom_by_id[atom_id]
        debts.append(RenderDebt('missing', atom.job_id, (atom_id,), (atom.source_text,), '', 'uncovered_source_atom'))
    return debts
def _coverage_atom_scope(atoms: list[SourceAtom] | tuple[SourceAtom, ...]) -> list[dict]:
    # Формирует точный leaf-local scope, чтобы checkpoint нельзя было наложить на parent/sibling atom universe.
    return [
        {
            'atom_id': atom.atom_id,
            'source_hash': hashlib.sha256(atom.source_text.encode('utf-8')).hexdigest()[:16],
            'render_domain_id': atom.render_domain_id,
            'source_unit_id': atom.source_unit_id,
        }
        for atom in atoms
    ]
def _coverage_scope_hash(scope: list[dict]) -> str:
    # Хеширует canonical scope для быстрой диагностики и защиты от частично поврежденных checkpoint payloads.
    encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
def coverage_to_json(coverage: CoverageMap) -> dict:
    # Сериализует coverage вместе с точным atom scope; durable resume остается leaf-owned, а не parent-wide.
    atom_scope = _coverage_atom_scope(coverage.atoms)
    return {
        'job_id': coverage.job_id,
        'atom_scope': atom_scope,
        'scope_hash': _coverage_scope_hash(atom_scope),
        'segments': [
            {'job_id': segment.job_id, 'segment_id': segment.segment_id, 'covers': list(segment.covers), 'translation': segment.translation, 'origin': segment.origin}
            for segment in coverage.segments
        ],
        'missing_atom_ids': list(coverage.missing_atom_ids),
        'legacy_empty_atom_ids': list(coverage.legacy_empty_atom_ids),
    }
def coverage_from_json(payload: dict, atoms: list[SourceAtom]) -> CoverageMap:
    # Восстанавливает checkpoint только на том же exact leaf atom universe и fail-closed отклоняет parent/sibling scope.
    payload = payload or {}
    current_scope = _coverage_atom_scope(atoms)
    stored_scope = payload.get('atom_scope')
    if isinstance(stored_scope, list):
        if stored_scope != current_scope or payload.get('scope_hash') not in (None, '', _coverage_scope_hash(stored_scope)):
            raise ValueError('Coverage checkpoint atom scope не совпадает с текущим leaf source.')
    else:
        # Legacy checkpoint не имел scope: выводим полный declared universe из segments/missing/empty и запрещаем расширение на parent source.
        declared = []
        for item in payload.get('segments', []):
            if isinstance(item, dict):
                declared.extend(str(value) for value in item.get('covers') or ())
        declared.extend(str(value) for value in payload.get('missing_atom_ids') or ())
        declared.extend(str(value) for value in payload.get('legacy_empty_atom_ids') or ())
        declared_unique = list(dict.fromkeys(declared))
        current_ids = [atom.atom_id for atom in atoms]
        if declared_unique and (len(declared_unique) != len(current_ids) or set(declared_unique) != set(current_ids)):
            raise ValueError('Legacy coverage checkpoint atom universe не совпадает с текущим leaf source.')
    job_id = str(payload.get('job_id') or (atoms[0].job_id if atoms else 'j0'))
    if atoms and any(atom.job_id != job_id for atom in atoms):
        raise ValueError('Coverage checkpoint job scope не совпадает с текущим leaf job_id.')
    segments = []
    for item in payload.get('segments', []):
        if not isinstance(item, dict):
            continue
        segments.append(TargetSegment(
            str(item.get('job_id') or job_id), str(item.get('segment_id') or ''),
            tuple(str(value) for value in item.get('covers') or ()), str(item.get('translation') or ''), str(item.get('origin') or 'translation'),
        ))
    expected = [atom.atom_id for atom in atoms]
    expected_set = set(expected)
    if any(segment.job_id != job_id or any(atom_id not in expected_set for atom_id in segment.covers) for segment in segments):
        raise ValueError('Coverage checkpoint содержит segment вне текущего atom/job scope.')
    covered = {atom_id for segment in segments for atom_id in segment.covers}
    return CoverageMap(job_id, tuple(atoms), segments, [atom_id for atom_id in expected if atom_id not in covered], [], list(payload.get('legacy_empty_atom_ids') or ()))
def apply_debt_segments(coverage: CoverageMap, debt_results: list[dict]) -> CoverageMap:
    # Targeted repair имеет semantics REPLACE, а не APPEND: old target того же atom scope удаляется до вставки repair.
    # Legacy result без atom_ids выводит scope из covers; частичное пересечение many-to-one сегмента fail-closed пропускается.
    segments = list(coverage.segments)
    source_by_id = {atom.atom_id: atom.source_text for atom in coverage.atoms}
    valid_atom_ids = set(source_by_id)
    invalid_segments = list(coverage.invalid_segments)
    for result in debt_results:
        candidate_segments = []
        candidate_atoms = set()
        valid_result = True
        for item in result.get('segments', []):
            covers = tuple(str(value) for value in item.get('covers', ()))
            if not covers or any(atom_id not in valid_atom_ids for atom_id in covers):
                valid_result = False
                break
            source_scope = ''.join(source_by_id.get(atom_id, '') for atom_id in covers)
            salvaged = salvage_translation_text(str(item.get('translation') or ''), source_scope)
            if not salvaged.safe or not salvaged.text.strip() or detect_markup_leak(salvaged.text, source_scope):
                valid_result = False
                break
            candidate_atoms.update(covers)
            candidate_segments.append(TargetSegment(
                coverage.job_id, str(item.get('segment_id') or ''), covers, salvaged.text, 'debt_repair'
            ))
        if not valid_result or not candidate_segments:
            invalid_segments.append({'reason': 'invalid_debt_repair_result'})
            continue
        declared_atoms = {str(atom_id) for atom_id in result.get('atom_ids', []) if str(atom_id)}
        replacement_atoms = declared_atoms or candidate_atoms
        if not replacement_atoms or candidate_atoms != replacement_atoms:
            invalid_segments.append({
                'reason': 'debt_repair_scope_mismatch',
                'declared_atoms': sorted(declared_atoms), 'candidate_atoms': sorted(candidate_atoms),
            })
            continue
        # Нельзя отрезать половину старого many-to-one target: такой repair должен быть сформирован заново на полном scope.
        crossing = [
            segment for segment in segments
            if replacement_atoms.intersection(segment.covers) and not set(segment.covers).issubset(replacement_atoms)
        ]
        if crossing:
            invalid_segments.append({
                'reason': 'debt_repair_partial_overlap',
                'replacement_atoms': sorted(replacement_atoms),
                'crossing_segments': [list(segment.covers) for segment in crossing],
            })
            continue
        segments = [segment for segment in segments if not replacement_atoms.intersection(segment.covers)]
        segments.extend(candidate_segments)
    expected_index = {atom.atom_id: atom.order for atom in coverage.atoms}
    segments.sort(key=lambda segment: min(expected_index.get(atom_id, 10**9) for atom_id in segment.covers))
    covered = {atom_id for segment in segments for atom_id in segment.covers}
    missing = [atom.atom_id for atom in coverage.atoms if atom.atom_id not in covered]
    return CoverageMap(coverage.job_id, coverage.atoms, segments, missing, invalid_segments, list(coverage.legacy_empty_atom_ids))
def build_coverage_provenance(coverage: CoverageMap, document_id: str | None, source_unit_ids: list[str] | None = None) -> list[dict]:
    # Строит many-to-one source-owned provenance напрямую из explicit coverage без угадывания target marker boundaries.
    if not document_id:
        return []
    if source_unit_ids is not None and len(source_unit_ids) != len(coverage.atoms):
        raise ValueError('source_unit_ids не покрывают coverage atoms 1:1.')
    atom_index = {atom.atom_id: atom for atom in coverage.atoms}
    source_ids = {
        atom.atom_id: (source_unit_ids[index] if source_unit_ids is not None else atom.source_unit_id or f'{document_id}:{index:06d}')
        for index, atom in enumerate(coverage.atoms)
    }
    provenance = []
    for segment in coverage.segments:
        covered_source_ids = tuple(source_ids[atom_id] for atom_id in segment.covers)
        segment_identity = hashlib.sha256('\0'.join((document_id, *covered_source_ids)).encode('utf-8')).hexdigest()[:20]
        target_slot_id = f'TGT_SEG_{segment_identity}'
        target_fragment_id = f'TFR_SEG_{segment_identity}'
        kind = 'MERGED' if len(segment.covers) > 1 else 'EXACT'
        for part_index, atom_id in enumerate(segment.covers):
            atom = atom_index[atom_id]
            provenance.append({
                'source_unit_id': source_ids[atom_id],
                'target_slot_id': target_slot_id,
                'target_fragment_id': target_fragment_id,
                'source_text': atom.source_text,
                'translated_text': segment.translation,
                'alignment_kind': kind,
                'origin': segment.origin,
                'source_hash_short': '',
                'target_dom_path': f'{target_fragment_id}:{atom.render_domain_id}',
                'part_index': part_index,
                'part_count': len(segment.covers),
                'marker_repair': {'kind': 'explicit_coverage', 'changed': False, 'runs': []},
            })
    return provenance
def normalize_coverage_provenance(provenance: list[dict] | None, document_id: str | None) -> list[dict]:
    # Мигрирует leaf-local target IDs из старых split checkpoint в document-scoped стабильные IDs.
    items = [dict(item) for item in provenance or ()]
    if not items or not document_id:
        return items
    groups = []
    current = []
    for item in items:
        part_index = int(item.get('part_index', 0))
        part_count = max(1, int(item.get('part_count', 1)))
        same_segment = bool(current) and part_index == len(current) and part_count == int(current[0].get('part_count', 1)) and item.get('target_fragment_id') == current[0].get('target_fragment_id') and item.get('target_slot_id') == current[0].get('target_slot_id') and item.get('translated_text') == current[0].get('translated_text')
        if current and not same_segment:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    normalized = []
    for group in groups:
        source_ids = tuple(str(item.get('source_unit_id') or '') for item in group)
        if any(not source_unit_id for source_unit_id in source_ids):
            raise ValueError('Coverage provenance содержит пустой source_unit_id.')
        segment_identity = hashlib.sha256('\0'.join((document_id, *source_ids)).encode('utf-8')).hexdigest()[:20]
        target_slot_id = f'TGT_SEG_{segment_identity}'
        target_fragment_id = f'TFR_SEG_{segment_identity}'
        for item in group:
            target_dom_path = str(item.get('target_dom_path') or '')
            if target_dom_path.startswith(('TFR_SEG_', 'TFR_LEGACY_')) and ':' in target_dom_path:
                target_dom_path = target_dom_path.split(':', 1)[1]
            item['target_slot_id'] = target_slot_id
            item['target_fragment_id'] = target_fragment_id
            item['target_dom_path'] = f'{target_fragment_id}:{target_dom_path}' if target_dom_path else target_fragment_id
            normalized.append(item)
    return normalized
