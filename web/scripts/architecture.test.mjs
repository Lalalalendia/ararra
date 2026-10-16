/** Dependency-free browser architecture checks: runs before npm install. */
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)));
const SOURCE = join(ROOT, 'src');

function allSources(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? allSources(path) : /\.(tsx?|jsx?)$/.test(entry.name) ? [path] : [];
  });
}

const sources = allSources(SOURCE);

function violations(file, source) {
  const errors = [];
  const path = relative(SOURCE, file).replaceAll('\\', '/');
  if (/\bcreateBrowserRouter\s*\(/.test(source) && path !== 'routes/router.tsx') {
    errors.push('Browser route registration must live in routes/router.tsx');
  }
  if (/\bfetch\s*\(/.test(source) && !path.startsWith('shared/api/')) {
    errors.push('HTTP transport belongs to shared/api/');
  }
  if (/\b(localStorage|sessionStorage)\b/.test(source) && /(token|secret|api.?key)/i.test(source)) {
    errors.push('Never persist team/API credentials in browser storage');
  }
  if (/chaptera_server|chaptera_translation_core|full_cycle_app/.test(source)) {
    errors.push('Frontend cannot directly depend on backend or Windows implementation');
  }
  if (path.startsWith('shared/') && /from\s*['"][^'"]*features\//.test(source)) {
    errors.push('Shared modules cannot import feature internals');
  }
  if (/VITE_.*(GEMINI|TOKEN|SECRET|API_KEY)/i.test(source)) {
    errors.push('Never bake server credentials into Vite environment variables');
  }
  if (source.split('\n').length > 301) {
    errors.push('Frontend module is over the 300-line architecture ceiling');
  }
  return errors;
}

test('web workspace has independent bootstrap, route and transport owners', () => {
  for (const expected of ['app/App.tsx', 'routes/router.tsx', 'routes/paths.ts', 'shared/api/client.ts', 'features/books/BooksPage.tsx']) {
    assert.ok(existsSync(join(SOURCE, expected)), `Missing web source: ${expected}`);
  }
  assert.ok(existsSync(join(ROOT, 'package.json')), 'Web package must be outside Python src');
  const entry = readFileSync(join(SOURCE, 'routes/router.tsx'), 'utf8');
  assert.match(entry, /createBrowserRouter\s*\(/);
  assert.match(entry, /routePaths/);
});

test('web sources obey route, feature, shared UI and API boundaries', () => {
  const all = sources.flatMap(file => violations(file, readFileSync(file, 'utf8')).map(error => `${relative(ROOT, file)}: ${error}`));
  assert.deepEqual(all, []);
});

test('guard rejects route sprawl, direct fetch and client-side credential storage', () => {
  const hypothetical = join(SOURCE, 'features/books/BooksPage.tsx');
  assert.match(violations(hypothetical, 'fetch("/v1/books")').join(' '), /shared\/api/);
  assert.match(violations(hypothetical, 'createBrowserRouter([])').join(' '), /routes\/router.tsx/);
  assert.match(violations(hypothetical, 'localStorage.setItem("api_key", token)').join(' '), /credentials/);
});
