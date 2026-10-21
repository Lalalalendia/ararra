# RUST-M1d — editorial review notes, Python/Rust shared SQLite

Source: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac), [migration issue #103](https://github.com/Lalalalendia/ararra/issues/103).

## What the editor can do

On an existing book chapter, an authenticated moderator can:
1. Read the bounded list of shared review notes, ordered by creation timestamp and ID, max 1,000.
2. Add a note against the source, draft and accepted revisions observed by the editor.
3. Resolve or reopen the note using per-note optimistic CAS. Another editor's stale patch is rejected; an identical replay leaves resolver, revision and timestamp unchanged.

The contract matches the current Python EditorialStore. Notes have UUID4 lowercase-hex IDs, Unicode text trimmed on insertion, actor attribution and the existing SQLite editor_review_notes schema. Note creation returns HTTP 201. Old notes can still be resolved after source/accepted changes (Python semantics), but a new note must have up-to-date source/draft/accepted versions.

## Endpoints

- GET /v1/books/{book_id}/chapters/{chapter_id}/notes — authenticated read.
- POST /v1/books/{book_id}/chapters/{chapter_id}/notes — create, only under disposable SQLite writer gate.
- PATCH /v1/books/{book_id}/chapters/{chapter_id}/notes/{note_id} — resolve/reopen, only under disposable SQLite writer gate.

The gate requires ERIDA_RUST_TEST_WRITES=1 and an explicit canonical ERIDA_RUST_DISPOSABLE_ROOT under the system temporary directory. There is no supported production Rust write path.

- Invalid body lengths or negative revision fields are rejected.
- CAS conflicts return HTTP 409, missing chapter/note HTTP 404.
- Team API key or cookie-session auth; cookie mutation requires CSRF.
- Failed requests must not modify the chapter, accepted translation, draft, or note revision.

## Acceptance

Run: python scripts/compare_rust_review_notes.py rust/target/release/erida-api.

The cross-runtime parity script runs the real Python ASGI app and Rust loopback with the same synthetic SQLite. Tests Rust create to Python read, Python create to Rust read, status changes both ways, concurrent Python/Rust CAS (one HTTP 200, one 409), cookie CSRF and API-key precedence, stale source/accepted/draft rejection, actor/resolver attribution, content isolation and reopen persistence.

Rust CI also covers all previous source, glossary, accepted translation, history and editor draft parity tests plus Rust workspace tests and Clippy. Python 3.11/3.12, Web and Chromium gates run separately. Merge only on exact tested HEAD.

## Not part of this slice

No live Gemini, import, QA, full editor HTML/UI migration, login/logout parity or export. Production Python API and worker remain owners. The original Python note implementation does not keep append-only status-change events; it tracks the last resolver and note_revision. Do not claim a complete note audit log.

M0 full-functional parity RSS/PSS, session/UI and offsite restore release gates remain unresolved.
