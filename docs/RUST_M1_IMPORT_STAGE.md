# RUST-M1o — durable, actor-owned import stages

Dependency: M1n, [PR #136](https://github.com/Lalalalendia/ararra/pull/136). This change is based on M1n feature HEAD and must **not be merged into main first**. The original Python contract is `ImportSessions.stage/get/cancel` in `src/chaptera_server/import_sessions.py`.

## User-visible behavior

A member can upload a supported UTF-8 TXT book, inspect its chapter suggestions and persist a temporary *private* stage for an explicit later chapter-by-chapter confirmation. It survives restart of the Rust process through the existing SQLite table. The upload is **not** yet a book import.

### Endpoints

- `POST /v1/imports/stages` (disposable-only test writer): multipart `file` up to 12 MiB, optional `Idempotency-Key` at most 128 characters, response 201 with public stage projection.
- `GET /v1/imports/stages/{stage_id}` (authenticated read): only the stage's actor can access the preview and escaped unassigned prefix. Other actors see the same 404 as an unknown ID.
- `DELETE /v1/imports/stages/{stage_id}` (disposable-only test writer): cancels and releases a staged upload. Wrong actor receives 404; expired stage 410; committed stage cannot be cancelled.

All stage POST/DELETE actions use the existing `ERIDA_RUST_TEST_WRITES=1` and canonical disposable root gate, with Cookie+HMAC CSRF or a valid team API key. Production Rust writers remain absent. The GET route remains read-only even when the write gate is disabled.

## Exact data invariants

Python has a three-stage boundary: 1) preview (M1n), 2) private stage (M1o), 3) explicit book commit (M1p). The Rust stage reuses the M1n parser's **strictly limited UTF-8 TXT grammar only**, so no unsafe HTML/ZIP, fallback decoding, filename guessing or hidden chapter type inference.

- Detects standalone `Глава N` / `Chapter N`, or one unnumbered chapter, and retains exact escaped raw chapter HTML: `<h1>Глава N</h1>\n<p>...</p>`, `<br/>` for line breaks, blank-line paragraph boundaries and Python `html.escape(..., quote=True)` entity spellings.
- Stores Python-compatible `payload_json` and `preview_json` in the existing `book_import_stages` schema, with all stage inputs parsed before transaction acquisition.
- Hashes original uploaded bytes with SHA-256, and uses SHA-256 of compact/sorted UTF-8 JSON `[filename, source_sha256]` for the idempotency fingerprint.
- Same actor+Idempotency-Key+identical file replays the original stage id and expiry; a different file conflicts (409). Concurrent contenders share a `BEGIN IMMEDIATE` SQLite lock and the unique `(actor_id,idempotency_key)` schema index.
- At most three active stages per actor, 30-minute staged TTL, 24 MiB expanded packed source+prefix cap. No body text appears in stage POST or GET projections. GET includes the escaped prefix only when requested by that same actor.
- Imported chapters, accepted translations, editorial drafts, revisions, glossary, jobs and source archives are never modified. Stage source HTML is private data in SQLite: it must not be put into logs, query strings or returned to another actor.

## Test evidence required

`scripts/compare_rust_import_stages.py` uses the existing Python ASGI and actual Rust HTTP process on one disposable SQLite. It checks both runtime directions, Python/Rust **byte-exact persisted payload JSON and fingerprint**, actor isolation, header API key and cookie-CSRF, idempotency conflicts, three-active-stage limit, 404/410/413 semantics and cancellation. It also shuts down Rust and restarts **without** the test-write gate against the same SQLite, then proves a private GET survives but POST/DELETE remain unavailable.

Rust unit tests and all pre-existing Rust/Python/UI Chromium CI must pass at the exact PR HEAD. Do not consider a queued or obsolete commit's success as acceptance.

## Not implemented (M1p)

There is intentionally **no** Rust `POST /v1/imports/stages/{stage_id}/confirm`. No create/replace/skip mapping of every source chapter, no book owner assignment, no CAS destination checks, no durable source writes and no import UI Chromium test. Those require a separate security and crash-consistency acceptance. Python production remains the reference.

## Deployment blockers

The Python server and live VPS/Nginx are untouched. Rust's authenticated mutations continue to be enabled only for canonical disposable SQLite. Actual external-IP identity behind Nginx, broader file formats and real Chromium upload→confirm remain separate release gates.
