# ReadEra RE-06C — durable chapter batch submission (first API slice)

This implementation makes the existing single-worker, moderator-key queue usable
for an explicit multi-chapter selection. It **does not** introduce another
scheduler, Gemini key, background process or publication path.

## Client workflow

1. Obtain source revisions and numeric positions from
   `GET /v1/books/{book_id}/chapters`; obtain the current glossary revision from
   `GET /v1/books/{book_id}`. All requests require a team session (with CSRF
   for mutations) or an authenticated `X-API-Key`.
2. `POST /v1/books/{book_id}/translation-batches/preview` with the selected
   chapter IDs, their exact `expected_source_revision`, the book's
   `expected_glossary_revision`, and translation options.
3. Display the returned chapters (numeric chapter order) and require explicit
   human confirmation of the exact `manifest_sha256`.
4. `POST /v1/books/{book_id}/translation-batches` with the same JSON, the
   returned `expected_manifest_sha256`, and a stable **Idempotency-Key** header
   (1–128 characters). Store the returned `batch_id`.
5. Resume/inspect through `GET /v1/books/{book_id}/translation-batches/{batch_id}`;
   list recent batches with `GET .../translation-batches`. Progress is a join
   of the existing durable jobs; no separate progress engine is involved.

Minimal preview JSON:

```json
{
  "expected_glossary_revision": 0,
  "chapters": [
    {"chapter_id": "chapter-1", "expected_source_revision": 1},
    {"chapter_id": "chapter-2", "expected_source_revision": 1}
  ],
  "source_language": "Chinese",
  "target_language": "Russian",
  "transport": "html_v0",
  "max_attempts": 3
}
```

The confirm request adds `"expected_manifest_sha256": "<sha256 from preview>"`
and the `Idempotency-Key` HTTP header. An identical retry returns the original
batch **and the same job IDs**, even after a server restart. Reusing a key with
different content returns HTTP 409.

## Safety boundaries

- All source IDs/revisions, the book glossary version and the preview SHA are
  checked in one `BEGIN IMMEDIATE` transaction. An invalid or outdated member
  of the batch aborts the **entire** queue submission; no partial work.
- 1–100 unique source chapters, 16 MiB total selected UTF-8 source data.
- Snapshot jobs use current book source, glossary and existing transport. Jobs
  retain the original queue actor and exact source/glossary revisions. The worker
  already checks for staleness again before calling Gemini.
- Job scheduling remains **one ready chapter first across moderators**, based on
  existing ready-job counts; never chapter text length. The same per-project quota
  logic and per-moderator stored Gemini keys continue to apply. Members with no
  key do not trigger paid calls.
- `preview`, `submit`, `GET` progress, CI and smoke tests make **zero**
  Gemini requests. Returning batch status does not accept or publish RU text.
- Shared library permissions remain exactly administrator/moderator. Book owner
  is still tracked separately; no new per-book ACL is invented. Team auth and
  browser CSRF are mandatory through the common dependency.
- Batch metadata contains chapter IDs, revision numbers and digests, not full
  source, generated text, prompt artifacts or secrets.

## Remaining RE-06C work

A real browser multi-select/confirmation/progress interface; quota-budget
admission based on the configured project/model; explicit whole-batch cancel
semantics if needed; production acceptance and browser UX validation.
These are **not** implied by this API's CI pass. Root bootstrap for persistent
translator systemd services is still blocked by chaptera-site issue #46.
