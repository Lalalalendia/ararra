# M1: server-owned books, versioned glossary and automatic context prepass

This is **server-only** functionality for Chaptera team members. It does not
alter `full_cycle_app`. Requires Python 3.11+ and one SQLite worker until the
PostgreSQL queue milestone.

## Why book-managed jobs exist

The v0.2 `POST /v1/jobs` accepts self-contained chapters plus optional snapshots
from clients; that API is unchanged. A new book-managed workflow requires no
caller-supplied context hashes, glossary hashes or predecessor references:

1. `POST /v1/books` with `{"book_id":"novel-1", "title":"Example"}`.
2. `PUT /v1/books/novel-1/chapters/001` with
   `{"chapter_index":1,"chapter_path":"text/001.xhtml","source_html":"<p>源文本</p>","expected_revision":0}`.
   Repeat per chapter. Chapter indexes are authoritative; the predecessor is
   exactly index N-1 (never the nearest earlier available chapter).
3. `PUT /v1/books/novel-1/glossary` with
   `{"glossary":{"李明":{"rus":"Ли Мин","authority":"required"}},"glossary_versions":{},"glossary_authority":"READY","expected_revision":0}`.
4. `POST /v1/books/novel-1/chapters/001/translate` with `{}` (optional
   `source_language`, `target_language`, `style_instructions`, `max_attempts`),
   with `X-API-Key` and preferably `Idempotency-Key` headers.
5. The worker first loads immutable book/chapter/glossary revisions, checks
   whether the cached schema-v3/prompt-v5 context is **current**, and if not
   requests JSON context from Gemini **as an asynchronous preflight stage of
   the same durable translation job**. Only complete, explicitly identified
   context may enter the snapshot store. Then it translates using the pinned
   source/glossary and the current predecessor dependency.
6. The context is persisted before translation; restarting the worker reuses a
   current snapshot. If the preflight is incomplete/malformed, the same job
   enters bounded retry (`retry_wait`) and no translation is published.
7. A human editor can accept a result with `PUT /v1/books/novel-1/chapters/001/accepted-translation`:
   `{"translated_html":"<p>Перевод</p>","expected_revision":0,"expected_source_revision":1}`. Both accepted
   translation and source revisions must match the editor's view. Merely
   completing the model job **does not** promote output to accepted memory.

All endpoints except `/healthz` require the member token, including GET
`/v1/books/{book_id}`, `/v1/books/{book_id}/chapters/{chapter_id}` and
`/v1/books/{book_id}/chapters/{chapter_id}/context`. Context GET only returns a
snapshot with `status=current`; stale/missing context returns `snapshot=null`.

## Revision/freshness semantics

- `books` and `book_glossary_revisions` store canonical book-level glossary,
  scoped term overrides and revision history, including the editing team member.
- `book_chapters` stores indexed source HTML, source SHA, source revision,
  accepted translation + revision and the source revision it was accepted for.
- `book_accepted_revisions` stores accepted translations and editor identity.
- `chapter_context_snapshots` stores model context with exact book/chapter,
  source, glossary and immediately preceding chapter dependency.
- Source revision, glossary revision and predecessor hash/tail dependency are
  rechecked before using cached context and before **atomically** committing
  a completed translation job. If anything changes, stale output never becomes
  `completed`. Glossary changes require optimistic revision match; conflicting
  scoped bindings are rejected before saving.
- Editing the original of a previously accepted chapter preserves its old
  accepted text for review but makes it **ineligible for continuity** until a
  new translation is explicitly accepted for that source revision.
- Semantic name/term suggestions are stored in context snapshots for review,
  **never automatically upgraded** into REQUIRED/SESSION glossary bindings.
- The context preflight receives only the current chapter and its immediate
  predecessor source/accepted Russian tail (bounded); it never receives later
  chapters. The visible-source-matched glossary is read-only.

## Operational limits

- Single SQLite worker, one shared team library and API tokens. Multi-process
  / HA worker queue and PostgreSQL remain separate milestones.
- Context preflight currently accepts at most 50,000 visible source characters
  per chapter; longer texts need a future bounded multi-part context planner.
- Context is an async **stage of the translation job**, not yet an independently
  scheduled/batched job type. Completed translation is not automatically marked
  accepted; team review must happen first.
- The source-atom JSON PromptBuilder, target-specific repair, live Gemini E2E
  validation, a UI and production deployment are not complete.
- SQLite state requires backups. `.env` and team/Gemini tokens must stay outside
  Git. `GET` endpoints are team-wide by design; no per-book ACL yet.

## Tests

`pytest -q tests/test_book_memory.py` covers two users, persistent cache,
revision conflict handling, CJK source grounding, future spoiler exclusion,
nonadjacent chapters, source/glossary/predecessor invalidation, cancellation and
malformed model responses. Run `pytest -q` for the full regression suite.

## v0.5 windowed preflight upgrade

Schema v3 / prompt version **6** fingerprints window coverage and invalidates earlier prompt-v5 cached snapshots on demand. Existing SQLite tables remain unchanged. Details and failure cases: [WINDOWED_CONTEXT.md](WINDOWED_CONTEXT.md).
