/** ReadEra chapter decisions, ported from the verified ararra React model.
 * This module deliberately has no DOM or server dependencies; the original
 * books_platform preview uses the same explicit chapter-numbering constraints.
 */
export function createDraft(preview) {
  const counts = new Map();
  for (const c of preview.chapters) {
    if (c.detected_number !== null) counts.set(c.detected_number, (counts.get(c.detected_number) || 0) + 1);
  }
  const positive = n => Number.isSafeInteger(n) && n > 0;
  const explicitDestination = c => {
    if (positive(c.destination_position)) return c.destination_position;
    if (positive(c.detected_number) && counts.get(c.detected_number) === 1) return c.detected_number;
    return null;
  };
  // Reserve all reliably mapped destinations first. A positional suggestion
  // must never silently collide with a numbered or source-verified chapter.
  const reserved = new Set(preview.chapters.map(explicitDestination).filter(n => n !== null));
  const suggestedUsed = new Set();
  return preview.chapters.map(c => {
    const entryType = c.entry_type || 'chapter';
    const needsReview = entryType === 'extra' || entryType === 'service' || entryType === 'unknown';
    let recommended = explicitDestination(c);
    let provisional_assignment = false;
    // Suggested index is a DESTINATION POSITION, not an author chapter
    // number. Fill it only for a normal, unnumbered chapter and only when
    // there is no collision. The explicit review/confirmation remains.
    if (!needsReview && recommended === null && c.detected_number === null &&
        positive(c.suggested_index) && !reserved.has(c.suggested_index) &&
        !suggestedUsed.has(c.suggested_index)) {
      recommended = c.suggested_index;
      provisional_assignment = true;
      suggestedUsed.add(recommended);
    }
    const number = recommended === null ? '' : String(recommended);
    return {
      sequence: c.sequence, title: c.title, sample: c.sample,
      detected_number: c.detected_number, characters: c.characters,
      entry_type: entryType, catalog_ordinal: c.catalog_ordinal ?? c.sequence,
      source_url: c.source_url ?? '',
      source_original_sha256: c.source_original_sha256 ?? '',
      source_working_sha256: c.source_working_sha256 ?? '',
      author_number: c.author_number ?? c.detected_number,
      suggested_index: c.suggested_index ?? null,
      provisional_assignment,
      auto_generated_id: provisional_assignment,
      auto_generated_path: provisional_assignment,
      action: needsReview ? 'review' : 'create', chapter_id: needsReview ? '' : number,
      chapter_index: needsReview ? '' : number,
      chapter_path: needsReview ? '' : (number ? 'chapters/' + number + '.xhtml' : ''),
      expected_revision: 0, verified_book_id: null, verified_chapter_id: null,
    };
  });
}
export function changeAction(row, action) {
  if (!['create', 'replace', 'skip'].includes(action)) throw new Error('Invalid action');
  return {...row, action,
    ...(action === 'skip' ? {
      chapter_id:'', chapter_index:'', chapter_path:'', provisional_assignment:false,
      auto_generated_id:false, auto_generated_path:false,
    } : {}),
    expected_revision: action === 'create' ? 0 : null,
    verified_book_id: null, verified_chapter_id: null,
  };
}
export function destinationEdited(row, field, value) {
  if (!['chapter_id','chapter_index','chapter_path'].includes(field)) throw new Error('Invalid field');
  const changed = {...row, [field]:value};
  if (field === 'chapter_index') {
    changed.provisional_assignment=false;
    // Keep the two generated destination fields in sync while the user edits
    // the suggested index. Explicitly edited IDs/paths never get overwritten.
    if (safePositive(value)) {
      if (row.auto_generated_id) changed.chapter_id=String(value);
      if (row.auto_generated_path) changed.chapter_path='chapters/'+value+'.xhtml';
    }
  }
  if (field === 'chapter_id') changed.auto_generated_id=false;
  if (field === 'chapter_path') changed.auto_generated_path=false;
  return {...changed,
    expected_revision: row.action === 'replace' ? null : 0,
    verified_book_id: null, verified_chapter_id: null,
  };
}
export function verifiedReplacement(row, bookId, remote) {
  if (row.action !== 'replace' || row.chapter_id.trim() !== remote.chapter_id) {
    throw new Error('Stale replacement mapping');
  }
  return {...row, chapter_index:String(remote.chapter_index),
    chapter_path:remote.chapter_path,expected_revision:remote.source_revision,
    provisional_assignment:false,auto_generated_id:false,auto_generated_path:false,
    verified_book_id:bookId,verified_chapter_id:remote.chapter_id};
}
const safePositive = n => /^[1-9]\d*$/.test(String(n)) && Number.isSafeInteger(Number(n));
const safePath = value => {
  const p = String(value || '').trim();
  return Boolean(p && p.length <= 1024 && !p.startsWith('/') &&
    !p.includes('\\') && !p.split('/').some(part => !part || part === '.' || part === '..'));
};
export function validateDraft(rows, target, stage, now=Date.now()/1000) {
  const errors = [];
  if (stage.expires_at <= now) errors.push('Временная загрузка истекла. Загрузите файл снова.');
  if (!target.book_id || target.book_id.trim().length > 128) errors.push('Укажите корректный ID книги.');
  if (target.book_mode === 'create') {
    if (!target.title?.trim()) errors.push('Введите название новой книги.');
    if (!target.owner_member_id) errors.push('Назначьте ответственного модератора.');
  }
  if (stage.unassigned_prefix_saved && !['preserve','discard'].includes(target.prefix_policy)) {
    errors.push('Примите отдельное решение по предисловию.');
  }
  if (!rows.length) errors.push('Не найдены исходные главы.');
  const ids=new Set(), indexes=new Set(), paths=new Set();
  let selected=0;
  for (const r of rows) {
    if (r.action === 'review') {
      errors.push('Запись '+r.sequence+': выберите действие для нераспознанной, дополнительной или служебной записи.');
      continue;
    }
    if (r.action === 'skip') continue;
    selected++;
    const id=r.chapter_id.trim(), index=Number(r.chapter_index), path=r.chapter_path.trim();
    if (!id || id.length>255 || !safePositive(r.chapter_index)) errors.push('Глава '+r.sequence+': укажите ID и номер.');
    if (!safePath(path)) errors.push('Глава '+r.sequence+': укажите безопасный относительный путь.');
    if (ids.has(id)||indexes.has(index)||paths.has(path)) errors.push('Глава '+r.sequence+': дубликат номера, ID или пути.');
    ids.add(id);indexes.add(index);paths.add(path);
    if (target.book_mode === 'existing' && r.provisional_assignment) {
      errors.push('Глава '+r.sequence+': для существующей книги подтвердите номер назначения вручную.');
    }
    if (r.action === 'replace') {
      if (target.book_mode !== 'existing') errors.push('Заменять можно только главы существующей книги.');
      if (r.expected_revision === null || r.verified_book_id !== target.book_id ||
          r.verified_chapter_id !== id) errors.push('Глава '+r.sequence+': загрузите текущую ревизию.');
    }
  }
  if (!selected) errors.push('Выберите хотя бы одну главу.');
  return [...new Set(errors)];
}
export function confirmation(rows,target,stage) {
  if (rows.some(r => r.action === 'review')) {
    throw new Error('Unreviewed auxiliary entries cannot be confirmed');
  }
  return {
    book_id: target.book_id.trim(), book_mode: target.book_mode,
    title: target.book_mode === 'create' ? target.title.trim() : null,
    owner_member_id: target.book_mode === 'create' ? target.owner_member_id : null,
    prefix_policy: stage.unassigned_prefix_saved ? target.prefix_policy : 'reject',
    chapters: rows.map(r => r.action === 'skip'
      ? { sequence: r.sequence, action:'skip' }
      : { sequence:r.sequence,action:r.action,chapter_id:r.chapter_id.trim(),
          chapter_index:Number(r.chapter_index), chapter_path:r.chapter_path.trim(),
          expected_revision:r.expected_revision }),
  };
}

/** Separate source numbering from suggested destination positions. */
export function importWarningText(warning) {
  if (warning === 'Archive has internal gaps in source indexes; verify missing chapters') {
    return 'В ZIP есть пропуски между индексами глав. Проверьте, какие главы не скачаны.';
  }
  if (warning === 'Archive includes collected chapters only; full source TOC coverage is not proven') {
    return 'В архив входят только собранные главы. Полнота оглавления сайта не подтверждена.';
  }
  const unnumbered = warning === 'Some entries have no explicit author number: sequence numbers are suggestions only'
    || warning === 'Some chapters have no explicit number: sequence numbers are suggestions only';
  return unnumbered
    ? 'Часть глав без номера в оригинале. Предложенные номера — только позиции в новой книге; проверьте их перед записью.'
    : warning;
}
export function sourceChapterLabel(row) {
  return row.detected_number === null
    ? (row.provisional_assignment
      ? 'Без авторского номера · предложена позиция '+row.chapter_index
      : 'Без авторского номера')
    : 'Авторский № '+row.detected_number;
}


/** Present source provenance without treating a ZIP subset as a complete TOC. */
export function sourceArchiveSummary(preview) {
  const archive=preview?.source_archive;
  if (!archive || archive.boundaries !== 'manifest') return null;
  const gaps=Array.isArray(archive.missing_positions) ? archive.missing_positions : [];
  const positions=gaps.slice(0,8).map(pair => {
    const [start,end]=pair;
    return start===end ? String(start) : start+'–'+end;
  });
  const gapsLabel=positions.length
    ? positions.join(', ')+(gaps.length>8?' и ещё '+(gaps.length-8)+' диапазонов':'')
    : 'Не обнаружены между полученными главами';
  const chapters=Array.isArray(preview.chapters) ? preview.chapters : [];
  const modified=chapters.filter(ch => ch.source_original_sha256
    && ch.source_working_sha256
    && ch.source_original_sha256 !== ch.source_working_sha256).length;
  return {
    domain: archive.source_domain || 'Не указан',
    sourceUrl: archive.source_url || 'Не указан',
    chapterCount: chapters.length,
    modifiedCount: modified,
    gapsLabel,
    gapCount: gaps.length,
    completenessWarning: 'Архив содержит только скачанные главы. Отсутствие пропусков внутри ZIP не доказывает, что весь сайт собран.',
  };
}
