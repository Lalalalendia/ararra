# RE-02 WEB-SOURCE-02 — durable chapter collection

**Scope:** complete a user-selected chapter range from an already inspected public-source TOC without losing verified chapters on disconnect or process restart. This follows the independent source-crawler slice in [PR #53](https://github.com/Lalalalendia/ararra/pull/53).

Reference implementation principles: uploaded `book_crawler.zip` modules `chapter_checkpoint.py`, `worker_checkpoint.py`, `worker_queue.py` and `http_transport.py`. ReadEra does **not** import the Books Platform ORM, payment, publication or user access models.

## User flow

1. A logged-in admin or moderator opens **Импорт глав → Собрать книгу с сайта**, chooses public site and URL, obtains the immutable catalog.
2. Select start/end (up to 2000 chapters, all present in this catalog) and choose **Создать фоновое задание**. Save the returned UUID or reopen it later using the same ID.
3. A separately started `chaptera-server source-worker` claims the next missing chapter. Its identity consists of source, book URL, catalog digest, source chapter id, source URL, ordinal and immutable manifest hash.
4. The worker retrieves a public page using the proven restricted `PublicHtmlFetcher`, checks recognized chapter body, character/byte budget and duplicate body SHA-256, then writes a single durable result to SQLite before releasing its claim.
5. UI can inspect progress without keeping a browser tab active. A blocked/login/locked/missing chapter is a failure receipt, **never** a recovered source. The moderator can retry failures (up to three explicit retry passes) while already-verified items remain untouched.
6. A verified contiguous batch (up to 20) can be handed to the **existing** `ImportSessions.stage`, followed by the existing explicit chapter-by-chapter create/replace/skip CAS confirmation. Collection does not translate, accept translations or publish.

## State and durability

- `source_collect_jobs` persists immutable validated manifest, digest, actor, catalog identity, idempotency key, job status, lease token, lease expiration, current item index and bounded manual retry count.
- `source_collect_items` contains a unique row per manifest index and ordinal, original source IDs/URLs, checked source text, SHA-256 and explicit failure receipts.
- `BEGIN IMMEDIATE` serializes claims and item commits. If the process crashes before committing the chapter, its timed-out lease allows re-fetch of that single item. A stale token cannot overwrite the new holder. Verified items are never fetched again by ordinary resume or manual retry.
- A cancellation revokes the lease and prevents further claims. A failed item is not silently retried; explicit actor action is required. Failure of a larger job does not make its partial text a complete book.
- Source text storage is capped at **1 MiB per chapter** and **256 MiB per collection job**. Over-budget text receives a failure receipt without discarding previously checkpointed chapters. The per-actor active-job cap is 3; total historical disk usage still needs explicit operational monitoring and a future retention/delete workflow.
- Both worker and API use the **same SQLite file**. A separate service/process is required; running only `chaptera-server serve` will not execute jobs.

## API contracts

- `POST /v1/source-books/jobs`: actor-authenticated job creation using `catalog_id`, expected SHA, explicit start/end, optional `Idempotency-Key`.
- `GET /v1/source-books/jobs/{id}`: actor-only progress and errors (no raw novel text in the response).
- `POST /v1/source-books/jobs/{id}/retry`: explicit failed-only retry.
- `POST /v1/source-books/jobs/{id}/cancel`: revoke pending/current work.
- `POST /v1/source-books/jobs/{id}/stage`: verified contiguous 1..20 chapter range to existing import stage; deterministic idempotency key and provenance receipts.

## Full original TXT assembly (RE-02-WEB-SOURCE-03)

`GET /v1/source-books/jobs/{job_id}/assembly` is an actor-authenticated
**read-only completeness report** for the exact selected job manifest. It
reports ordered first/last chapter, selected and verified counts, pending,
failed and damaged checkpoints, up to 50 structured failure reasons, source
catalog/selection digests and a content SHA-256 receipt when fully ready.
Neither original prose nor Russian translations are included in the report.

`GET /v1/source-books/jobs/{job_id}/export.txt?expected_manifest_sha256=<sha>&expected_content_sha256=<sha>`
requires **both receipts** from a fresh report. It recomputes each saved
chapter's ordinal, source ID/URL, SHA-256, UTF-8 byte count, and duplicate
body detection using a consistent SQLite read snapshot. A forged `complete`
flag alone never enables download; any missing/failed/damaged/duplicate
chapter yields HTTP 409 and **no TXT**. A changed content hash after preview
also yields 409. An actor cannot inspect/download another actor's job.

The file contains the **original source**, in ascending selected ordinal,
with one `Глава N. Название` heading per chapter. Line endings are normalized
to LF, text is UTF-8, and chapter body is otherwise preserved. A seekable
temporary file holds the fully checked output before streaming; the server
does not buffer the whole novel as a single Python bytes object. The
response has no-store/nosniff, source-scope, manifest and content SHA-256
headers. The browser recomputes SHA-256 before triggering a file download.

Memory/disk cap: **64 MiB per assembled TXT** (less than the existing
256-MiB durable source job budget). Larger source collections remain in
SQLite and need a future explicitly chunked export, not a silently
truncated full-book TXT.

**Scope boundary:** `ready=true` proves all chapters of the selected
contiguous *observed catalog manifest*, not all chapters ever published
on the source site. An incomplete or paginated catalog must not be called
a complete novel. This feature never marks any chapter accepted, translated
or published. The existing moderator confirmation is still necessary to
import chapter source into ReadEra's managed library.

## Server-owned history and explicit cleanup (RE-02-WEB-SOURCE-04)

The user no longer needs to copy and retain opaque UUIDs to reopen a crawl.
`GET /v1/source-books/jobs?limit=30` returns the authenticated actor's
recent jobs ordered by last update, with source, original book URL, selected
range, counts, failure receipts and status — **never raw novel text**.
The native Jinja UI renders a recent-job picker, reloads the server-owned
job before opening it, and clears the list whenever the authenticated
workspace is reset. No browser `localStorage` or new role is introduced.

`DELETE /v1/source-books/jobs/{job_id}` requires an explicit confirmed
action and the job state to be `complete`, `partial`, or `cancelled`.
A `queued` or `running` worker must first be cancelled, which revokes
its current lease. An immediate SQLite transaction removes only the source
capture record and its chapter checkpoints; actor boundaries still apply.
Its return receipt records removed checkpoint count and reclaimed source
bytes. Confirmed BookLibrary chapters, accepted translations, staged
imports and their separate provenance remain unchanged. There is **no
automatic retention/deletion of downloaded chapters**.

**Outstanding production gate:** job history is bounded per query (max 50),
but historical source storage across all actors is not globally quota
controlled. VPS installation requires monitored disk budgets and a
well-defined retention/quota policy before opening bulk uploads to a larger
team. Deleting a source job does not rewrite/undo any published book.

## Operational / release gates

- Run **one** source-worker initially alongside the standard Gemini worker; they use different queues. `CHAPTERA_GEMINI_API_KEY` is **not required** for the crawler worker. Start via `chaptera-server source-worker` in an explicitly managed unit/service.
- During server restart, terminate workers cleanly, back up the live WAL-consistent SQLite database using the existing RE-08 backup method and restore using its verified offline recovery path.
- The current per-process HTTP pacing is not a multi-worker distributed rate limit. Do not scale source-workers or expose uncontrolled bulk captures until domain-level cluster admission, monitored queue depth and shutdown discipline are in place.
- Real public HTTPS page tests, anti-blocking status handling, source rights/robots checks, log redaction, memory/disk budgets and long-corpus fault injection are required before production activation.
- The catalog is a snapshot of pages **observed**, not proof that the entire source site's pagination was discovered. Job `complete` means **all selected manifest chapters fetched**, not `all chapters ever published`.
- Chunks are intentionally limited to existing 12 MiB upload / 24 MiB stage budgets; 2000 chapters do not automatically become one oversized `ImportSessions` stage.
- No new cloud backend, payment, public signup, translation provider, or bypass of CAPTCHA/paid/login-only chapters is added.

## Tests

- Crash/restart with SQLite reopening, claim fencing and stale worker rejection.
- Separate explicit failed-only retry preserving already accepted hashes.
- Duplicate source-body fail closed; tampering of saved source text detected.
- Auth-isolated HTTP job create/status/stage; deterministic duplicate stage replay.
- Existing book ownership, explicit import CAS, editor and browser auth must remain green.
