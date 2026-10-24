#!/usr/bin/env python3
"""Start only the Rust UI process; run actual Chromium against that origin.

Python bootstraps the existing empty schema. The fixture book and source chapters
are created through Rust HTTP, not the UI. All editing/export steps are UI clicks.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import httpx
from chaptera_server.storage import QueueStore
from chaptera_server.library import BookLibrary
from chaptera_server.editorial import EditorialStore
from chaptera_server.team_credentials import TeamCredentials
from chaptera_server.team_sessions import TeamSessions
from chaptera_server.team_login_throttle import BrowserLoginThrottle

BASE = 'http://127.0.0.1:18492'
ADMIN = 'm1k-synthetic-administrator-key'
MOD = 'm1k-synthetic-moderator-key'
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit('usage: verify_rust_ui_browser.py /path/to/erida-ui-preview')
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix='erida-m1k-ui-') as folder:
        directory = Path(folder)
        db = directory / 'disposable.sqlite3'
        store = QueueStore(db)
        for constructor in (BookLibrary, EditorialStore, TeamCredentials,
                            TeamSessions, BrowserLoginThrottle):
            constructor(store)
        env = dict(os.environ)
        for key in ('CREDENTIALS_DIRECTORY', 'CHAPTERA_GEMINI_API_KEY',
                    'CHAPTERA_GEMINI_VAULT_KEY',
                    'ERIDA_RUST_TRUST_LOOPBACK_PROXY_CLIENT_IP'):
            env.pop(key, None)
        env.update({
            'CHAPTERA_DB_PATH': str(db),
            'CHAPTERA_ADMIN_MEMBER_ID': 'chief',
            'CHAPTERA_TEAM_TOKENS_JSON': json.dumps({'chief': ADMIN, 'mod': MOD}),
            'CHAPTERA_BROWSER_SESSION_SECURE': '0',
            'ERIDA_RUST_ADDR': '127.0.0.1:18492',
            'ERIDA_RUST_TEST_WRITES': '1',
            'ERIDA_RUST_UI_PREVIEW': '1',
            'ERIDA_RUST_DISPOSABLE_ROOT': str(directory),
        })
        # Preview must not start from a normal/default production environment.
        for missing in ('ERIDA_RUST_TEST_WRITES', 'ERIDA_RUST_UI_PREVIEW'):
            refused = dict(env)
            refused.pop(missing)
            result = subprocess.run([binary], env=refused, timeout=5,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert result.returncode != 0, 'preview safety flag was not enforced'
        process = subprocess.Popen([binary], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            with httpx.Client(base_url=BASE, timeout=3) as client:
                while True:
                    assert process.poll() is None, 'Rust UI process exited during startup'
                    try:
                        if client.get('/healthz').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError('Rust UI did not become ready')
                    time.sleep(0.1)
                headers = {'X-API-Key': MOD}
                book = client.post('/v1/books', headers=headers, json={
                    'book_id': 'm1k-book', 'title': 'Rust UI acceptance',
                })
                assert book.status_code == 201, 'Rust fixture book creation failed'
                for index in (1, 2):
                    source = '<p>' + ('Original source excluded from export. ' * 20) + '</p>'
                    response = client.put(f'/v1/books/m1k-book/chapters/c{index}',
                        headers=headers, json={
                            'chapter_index': index, 'chapter_path': f'chapter{index}.xhtml',
                            'source_html': source, 'expected_revision': 0,
                        })
                    assert response.status_code == 200, 'Rust fixture chapter creation failed'
                # Negative UI and static-file boundaries use no proxy fallback.
                assert client.get('/app/import').status_code == 200
                assert client.get('/app/unknown').status_code == 404
                for path in ('/app-static/.env', '/app-static/js/readera.test.mjs',
                             '/app-static/%2e%2e%2fCargo.toml'):
                    assert client.get(path).status_code == 404, 'Unexpected static-file exposure'
            browser_env = dict(env)
            browser_env.update({'READ_E2E_BASE_URL': BASE,
                                'READ_E2E_ADMIN_KEY': ADMIN})
            subprocess.run(['node', str(ROOT / 'tests/browser/rust-native-ui.mjs')],
                           env=browser_env, cwd=ROOT, check=True, timeout=240)
            assert process.poll() is None, 'Rust server exited during browser test'
            print('rust_ui_fixture_creation=RUST_HTTP_NOT_UI')
            print('rust_ui_browser_backend=RUST_ONLY')
            print('python_http_server_started=NO')
            print('production_sqlite_access=NO')
            print('gemini_calls=0')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
