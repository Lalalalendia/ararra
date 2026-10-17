/** Runtime checks for the actual TypeScript decision model (not source-text grep).
 * Transpile in-memory using the installed TypeScript build dependency.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import ts from 'typescript';

const filename = fileURLToPath(new URL('../src/features/import/model.ts', import.meta.url));
const source = readFileSync(filename, 'utf8');
const output = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  fileName: filename,
  reportDiagnostics: true,
});
assert.equal((output.diagnostics ?? []).filter(x => x.category === ts.DiagnosticCategory.Error).length, 0);
const exports = {};
runInNewContext(output.outputText, { exports }, { timeout: 2000 });
const { newDraft, selectAction, editDestination, checkedReplacement,
  validateImport, buildConfirmation, countDecisions } = exports;

const preview = numbers => ({
  kind: 'book', filename: 'sample.txt', title: 'Example', chapter_count: numbers.length,
  unassigned_prefix_characters: 0, unassigned_prefix_sample: '', warnings: [],
  chapters: numbers.map((number, index) => ({
    sequence: index + 1, detected_number: number, suggested_index: number ?? index + 1,
    title: 'Глава ' + (index + 1), source_path: 'file.txt', characters: 24,
    sample: 'Пример содержимого', has_text: true, sha256: 's',
  })),
});

const create = (id = 'novel') => ({
  mode: 'create', id, title: 'Новая книга', owner: 'moderator', prefixPolicy: 'reject',
});
const existing = (id = 'novel') => ({
  mode: 'existing', id, title: '', owner: '', prefixPolicy: 'reject',
});
const issues = (choices, target = create(), prefix = false, deadline = 9000) =>
  [...validateImport(choices, target, prefix, 100, deadline)];

test('unnumbered and duplicated source headings cannot receive guessed destinations', () => {
  const rows = newDraft(preview([12, 12, null, 13]));
  assert.equal(rows.length, 4);
  for (const row of rows.slice(0, 3)) {
    assert.equal(row.chapter_id, '');
    assert.equal(row.chapter_index, '');
    assert.equal(row.chapter_path, '');
  }
  assert.equal(rows[3].chapter_id, '13');
  assert.ok(issues(rows).some(message => message.includes('Глава 1')));
  assert.ok(issues(rows).some(message => message.includes('Глава 3')));
});

test('explicit skip removes every destination and serializes without destination keys', () => {
  let rows = newDraft(preview([1, 2]));
  rows[1] = selectAction(rows[1], 'skip');
  assert.deepEqual(JSON.parse(JSON.stringify(countDecisions(rows))), {
    create: 1, replace: 0, skip: 1,
  });
  const result = buildConfirmation(rows, create(), false);
  assert.deepEqual(JSON.parse(JSON.stringify(result.chapters[1])), {
    sequence: 2, action: 'skip',
  });
  assert.equal(issues(rows).length, 0);
});

test('all-skipped or duplicate mapping or missing title/owner is rejected before HTTP', () => {
  const rows = newDraft(preview([1, 2]));
  assert.ok(issues(rows.map(x => selectAction(x, 'skip'))).some(x => x.includes('хотя бы')));
  assert.ok(issues(rows.map(x => ({ ...x, chapter_index: '1' }))).some(x => x.includes('повторяется')));
  assert.ok(issues(rows, { ...create(), title: '', owner: '' }).some(x => x.includes('ответственного')));
  assert.ok(issues(rows, { ...create(), title: '', owner: '' }).some(x => x.includes('название')));
});

test('replacement needs a loaded revision for its exact chapter and selected book', () => {
  let row = selectAction(newDraft(preview([86]))[0], 'replace');
  assert.ok(issues([row], existing()).some(x => x.includes('ревизию')));
  row = checkedReplacement(row, 'novel', {
    chapter_id: '86', chapter_index: 86,
    chapter_path: 'chapters/86.xhtml', source_revision: 7,
  });
  assert.equal(issues([row], existing()).length, 0);
  assert.equal(buildConfirmation([row], existing(), false).chapters[0].expected_revision, 7);
  assert.ok(issues([row], existing('other')).some(x => x.includes('ревизию')));
  row = editDestination(row, 'chapter_id', '87');
  assert.equal(row.expected_revision, null);
  assert.ok(issues([row], existing()).some(x => x.includes('ревизию')));
});

test('replacements cannot sneak into a new-book batch; create cannot inherit a version', () => {
  const original = newDraft(preview([1]))[0];
  const replace = selectAction(original, 'replace');
  assert.equal(replace.expected_revision, null);
  assert.ok(issues([replace], create()).some(x => x.includes('существующей')));
  const created = selectAction(replace, 'create');
  assert.equal(created.expected_revision, 0);
});

test('unsafe relative paths and stale stage deadlines are caught', () => {
  let row = newDraft(preview([1]))[0];
  row = editDestination(row, 'chapter_path', '../secret.txt');
  assert.ok(issues([row]).some(x => x.includes('безопасный')));
  row = editDestination(row, 'chapter_path', '/root/file.xhtml');
  assert.ok(issues([row]).some(x => x.includes('безопасный')));
  row = editDestination(row, 'chapter_path', 'chapters/1.xhtml');
  assert.ok(issues([row], create(), false, 99).some(x => x.includes('истекла')));
});

test('preface demands deliberate decision and is never silently discarded', () => {
  const rows = newDraft(preview([1]));
  assert.ok(issues(rows, create(), true).some(x => x.includes('предисловия')));
  assert.equal(issues(rows, { ...create(), prefixPolicy: 'preserve' }, true).length, 0);
  assert.equal(buildConfirmation(rows, { ...create(), prefixPolicy: 'discard' }, true).prefix_policy, 'discard');
  assert.equal(buildConfirmation(rows, create(), false).prefix_policy, 'reject');
});
