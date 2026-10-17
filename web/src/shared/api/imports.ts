/** Source-import transport and versioned contracts for the ReadEra browser.
 * The API key is supplied by the logged-in editor for each request. It is
 * never persisted or included in a URL. Staged chapter bodies stay server-side.
 */
export const MAX_IMPORT_BYTES = 12 * 1024 * 1024;
export const SUPPORTED_IMPORT_EXTENSIONS = ['.txt', '.html', '.htm', '.xhtml', '.epub', '.docx'];

export type ImportCandidate = {
  sequence: number;
  detected_number: number | null;
  suggested_index: number;
  title: string;
  source_path: string;
  characters: number;
  sha256: string;
  sample: string;
  has_text: boolean;
};
export type BookPreview = {
  kind: 'book';
  format: string;
  filename: string;
  title: string;
  chapter_count: number;
  chapters: ImportCandidate[];
  warnings: string[];
  unassigned_prefix_characters: number;
  unassigned_prefix_sample: string;
  stored: false;
};
export type StageSnapshot = {
  stage_id: string;
  status: 'staged' | 'committed';
  file_sha256: string;
  expires_at: number;
  preview: BookPreview;
  unassigned_prefix_saved: boolean;
  unassigned_prefix_html?: string;
  commit_result?: ImportReceipt;
};
export type BookSummary = {
  book_id: string;
  title: string;
  owner_member_id: string | null;
  owner_revision: number;
};
export type ExistingChapter = {
  chapter_id: string;
  chapter_index: number;
  chapter_path: string;
  source_revision: number;
};
export type ImportAction = 'create' | 'replace' | 'skip';
export type ImportDecision = {
  sequence: number;
  action: ImportAction;
  chapter_id?: string | null;
  chapter_index?: number | null;
  chapter_path?: string | null;
  expected_revision?: number | null;
};
export type ConfirmBookImport = {
  book_id: string;
  book_mode: 'create' | 'existing';
  title: string | null;
  owner_member_id: string | null;
  prefix_policy: 'reject' | 'preserve' | 'discard';
  chapters: ImportDecision[];
};
export type ImportReceipt = {
  stage_id: string;
  status: 'committed';
  book_id: string;
  created_book: boolean;
  imported_count: number;
  skipped_count: number;
  unassigned_prefix_preserved: boolean;
  chapters: Array<{
    sequence: number;
    action: ImportAction;
    chapter_id?: string;
    chapter_index?: number;
    source_revision?: number;
  }>;
};

export class ImportHttpError extends Error {
  constructor(readonly status: number, detail: string) {
    super(detail);
    this.name = 'ImportHttpError';
  }
}

async function api<T>(url: string, key: string, init: RequestInit = {}): Promise<T> {
  if (!key) throw new ImportHttpError(401, 'Требуется ключ участника');
  const response = await fetch(url, {
    ...init,
    credentials: 'omit',
    cache: 'no-store',
    headers: {
      'X-API-Key': key,
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = 'Не удалось выполнить запрос';
    try {
      const json = await response.json() as { detail?: unknown };
      if (typeof json.detail === 'string') detail = json.detail.slice(0, 350);
    } catch { /* Untrusted HTTP error is not displayed verbatim. */ }
    throw new ImportHttpError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const importApi = {
  listBooks: async (key: string) =>
    (await api<{ books: BookSummary[] }>('/v1/books', key)).books,
  stage: (key: string, file: File, idempotencyKey: string) => {
    const data = new FormData();
    data.append('file', file, file.name);
    return api<StageSnapshot>('/v1/imports/stages', key, {
      method: 'POST',
      body: data,
      headers: { 'Idempotency-Key': idempotencyKey },
    });
  },
  stageDetails: (key: string, stageId: string) =>
    api<StageSnapshot>('/v1/imports/stages/' + encodeURIComponent(stageId), key),
  chapter: (key: string, bookId: string, chapterId: string) =>
    api<ExistingChapter>(
      '/v1/books/' + encodeURIComponent(bookId) + '/chapters/' + encodeURIComponent(chapterId),
      key,
    ),
  confirm: (key: string, stageId: string, choices: ConfirmBookImport) =>
    api<ImportReceipt>('/v1/imports/stages/' + encodeURIComponent(stageId) + '/confirm', key, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(choices),
    }),
};
