/** Pure mapping and validation for the RE-02 three-step import wizard.
 * No HTTP, browser state, data loss, or implicit decisions live here.
 */
import type {
  BookPreview, ConfirmBookImport, ExistingChapter, ImportAction, ImportDecision,
} from '../../shared/api/imports';

export type DraftChapter = {
  sequence: number;
  title: string;
  detected_number: number | null;
  characters: number;
  sample: string;
  action: ImportAction;
  chapter_id: string;
  chapter_index: string;
  chapter_path: string;
  expected_revision: number | null;
  verified_book_id: string | null;
  verified_chapter_id: string | null;
};
export type BookTarget = {
  mode: 'create' | 'existing';
  id: string;
  title: string;
  owner: string;
  prefixPolicy: 'reject' | 'preserve' | 'discard';
};

export function newDraft(preview: BookPreview): DraftChapter[] {
  const count = new Map<number, number>();
  preview.chapters.forEach(ch => {
    if (ch.detected_number !== null) {
      count.set(ch.detected_number, (count.get(ch.detected_number) ?? 0) + 1);
    }
  });
  return preview.chapters.map(ch => {
    // No invented numbers: missing/duplicate source headings must be mapped
    // manually by the editor or explicitly skipped before confirmation.
    const unique = ch.detected_number !== null && count.get(ch.detected_number) === 1;
    const index = unique ? String(ch.detected_number) : '';
    return {
      sequence: ch.sequence, title: ch.title, detected_number: ch.detected_number,
      characters: ch.characters, sample: ch.sample, action: 'create',
      chapter_id: index, chapter_index: index,
      chapter_path: unique ? 'chapters/' + index + '.xhtml' : '',
      expected_revision: 0, verified_book_id: null, verified_chapter_id: null,
    };
  });
}

export function selectAction(chapter: DraftChapter, action: ImportAction): DraftChapter {
  if (action === 'skip') {
    return {
      ...chapter, action, chapter_id: '', chapter_index: '',
      chapter_path: '', expected_revision: null,
      verified_book_id: null, verified_chapter_id: null,
    };
  }
  // Switching to replacement NEVER inherits the expected_revision=0 of create.
  return {
    ...chapter, action, expected_revision: action === 'create' ? 0 : null,
    verified_book_id: null, verified_chapter_id: null,
  };
}

export function editDestination(chapter: DraftChapter, field: 'chapter_id' | 'chapter_index' | 'chapter_path',
                                value: string): DraftChapter {
  return {
    ...chapter, [field]: value,
    expected_revision: chapter.action === 'replace' ? null : 0,
    verified_book_id: null, verified_chapter_id: null,
  };
}

export function checkedReplacement(chapter: DraftChapter, bookId: string,
                                   server: ExistingChapter): DraftChapter {
  if (chapter.action !== 'replace' || server.chapter_id !== chapter.chapter_id.trim()) {
    throw new Error('Проверка назначения устарела; загрузите ревизию заново');
  }
  return {
    ...chapter, chapter_index: String(server.chapter_index),
    chapter_path: server.chapter_path, expected_revision: server.source_revision,
    verified_book_id: bookId, verified_chapter_id: server.chapter_id,
  };
}

const validInteger = (v: string) => /^[1-9]\d*$/.test(v) && Number.isSafeInteger(Number(v));
const validPath = (v: string) => {
  const p = v.trim();
  return p.length > 0 && p.length <= 1024 && !p.startsWith('/') &&
    !p.includes('\\') && !p.split('/').some(bit => !bit || bit === '.' || bit === '..');
};

export function validateImport(
  draft: DraftChapter[], target: BookTarget,
  hasPrefix: boolean, nowSeconds: number, expiresAt: number,
): string[] {
  const failures: string[] = [];
  if (expiresAt <= nowSeconds) failures.push('Временная загрузка истекла: загрузите файл заново.');
  if (!target.id.trim() || target.id.trim().length > 128) failures.push('Укажите ID книги (до 128 символов).');
  if (target.mode === 'create') {
    if (!target.title.trim() || target.title.trim().length > 500) failures.push('Укажите название новой книги.');
    if (!target.owner) failures.push('Назначьте ответственного модератора.');
  }
  if (hasPrefix && target.prefixPolicy === 'reject') {
    failures.push('Для предисловия обязательно выберите «сохранить» или «исключить».');
  }
  if (!hasPrefix && target.prefixPolicy === 'preserve') {
    failures.push('Предисловие не обнаружено.');
  }
  if (!draft.length) failures.push('Не обнаружены главы для импорта.');
  const ids = new Set<string>(), indexes = new Set<number>(), paths = new Set<string>();
  let selected = 0;
  for (const row of draft) {
    if (row.action === 'skip') continue;
    selected += 1;
    const id = row.chapter_id.trim();
    const index = Number(row.chapter_index);
    const path = row.chapter_path.trim();
    if (!id || id.length > 255 || !validInteger(row.chapter_index)) {
      failures.push('Глава ' + row.sequence + ': проверьте ID и номер назначения.');
    }
    if (!validPath(path)) failures.push('Глава ' + row.sequence + ': укажите безопасный относительный путь.');
    if (ids.has(id) || indexes.has(index) || paths.has(path)) {
      failures.push('Глава ' + row.sequence + ': повторяется ID, номер или путь назначения.');
    }
    ids.add(id); indexes.add(index); paths.add(path);
    if (row.action === 'replace') {
      if (target.mode !== 'existing') failures.push('Замена глав возможна только в существующей книге.');
      if (row.expected_revision === null ||
          row.verified_book_id !== target.id.trim() ||
          row.verified_chapter_id !== id) {
        failures.push('Глава ' + row.sequence + ': загрузите актуальную ревизию для замены.');
      }
    }
  }
  if (!selected) failures.push('Выберите хотя бы одну главу для записи.');
  return [...new Set(failures)];
}

export function buildConfirmation(draft: DraftChapter[], target: BookTarget,
                                  hasPrefix: boolean): ConfirmBookImport {
  const chapters: ImportDecision[] = draft.map(row => {
    if (row.action === 'skip') return { sequence: row.sequence, action: 'skip' };
    return {
      sequence: row.sequence, action: row.action,
      chapter_id: row.chapter_id.trim(),
      chapter_index: Number(row.chapter_index),
      chapter_path: row.chapter_path.trim(),
      expected_revision: row.expected_revision,
    };
  });
  return {
    book_id: target.id.trim(), book_mode: target.mode,
    title: target.mode === 'create' ? target.title.trim() : null,
    owner_member_id: target.mode === 'create' ? target.owner : null,
    prefix_policy: hasPrefix ? target.prefixPolicy : 'reject',
    chapters,
  };
}

export function countDecisions(chapters: DraftChapter[]) {
  return {
    create: chapters.filter(c => c.action === 'create').length,
    replace: chapters.filter(c => c.action === 'replace').length,
    skip: chapters.filter(c => c.action === 'skip').length,
  };
}
