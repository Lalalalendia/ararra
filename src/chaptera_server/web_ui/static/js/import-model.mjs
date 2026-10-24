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
      author_number: c.author_number ?? c.detected_number,
      suggested_index: c.suggested_index ?? null,
      provisional_assignment,
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
    } : {}),
    expected_revision: action === 'create' ? 0 : null,
    verified_book_id: null, verified_chapter_id: null,
  };
}
export function destinationEdited(row, field, value) {
  if (!['chapter_id','chapter_index','chapter_path'].includes(field)) throw new Error('Invalid field');
  return {...row, [field]:value,
    ...(field === 'chapter_index' ? { provisional_assignment:false } : {}),
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
    provisional_assignment:false,
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
