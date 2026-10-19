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

## Source TOC coverage and bounded pagination (RE-02-WEB-SOURCE-05)

A successful source export has always proved integrity only for the chosen
manifest. That is **not** enough to prove that all chapters published on a
source site were discovered.

- **Royal Road:** the public table may include prologues, interludes, Roman
  chapter numbers and manually reordered chapters. Parser identity now
  follows the observed official TOC order and stable same-book source IDs;
  displayed ordinal means *position in the observed TOC*, not the chapter
  number typed into a title by its author.
- **Bounded explicit pagination:** collector follows up to 12 linked public
  HTML TOC pages whose URL is the *exact same source host + book path* and
  contains only a single positive numeric `page=N` query. Unsupported,
  cross-book, unsafe, sparse or dynamic load-more pagination is rejected
  rather than silently creating a truncated catalog. No guessed AJAX calls,
  cookies, paywall/CAPTCHA bypass or site-specific private API requests.
- **Advertised chapter count:** when Royal Road exposes an explicit
  `N Chapters` marker, the collected public links must match it exactly.
  Inconsistency blocks the catalog; a missing marker is reported as
  `observed_html_only`. A matched marker is reported as
  `declared_count_matched`, **not** a guarantee that off-site, deleted,
  private, locked or unpublished content is accessible.
- **Provenance:** actor-scoped SQLite catalogs save page URLs, number of pages,
  the advertised count where available, and coverage state. Additive schema
  migration does not destroy already stored source chapters or jobs.
  Browser preview shows the scope and warns when a site did not publish a
  comparable count. The unchanged SHA-256 selection digest still covers
  exact chapter ordinal, source ID and URL.

The resulting job's `complete` flag still means every **chosen observed**
chapter has been verified and checkpointed, and the original TXT export still
requires an exact manifest and content receipt. A book marked stubbed or
partly hosted elsewhere must not be described as the complete novel.

Follow-up: inspect permitted real site markup and `robots.txt` and add
separately verified coverage adapters for SPA/AJAX TOCs. The current
implementation only covers explicitly linked HTML pagination.

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

## Cross-book verified-source quotas (RE-02-WEB-SOURCE-06)

A single collection already had a 256 MiB verified-source cap, but an
unbounded number of completed jobs could consume the shared VPS disk.
Admission now uses **two durable logical quotas** across all retained
checkpointed source texts:

- `CHAPTERA_SOURCE_ACTOR_QUOTA_MIB` (default **512 MiB/member**).
- `CHAPTERA_SOURCE_GLOBAL_QUOTA_MIB` (default **2048 MiB/shared SQLite**).
  The global limit must be at least the per-member limit. Both values must
  be positive integers with a hard upper bound of 1 TiB.
- Every successful source chapter updates usage via the same
  `BEGIN IMMEDIATE` SQLite transaction that checks its per-job, per-actor
  and global budget. Already accepted checkpoints are never dropped to
  satisfy quota. Over-budget chapters record a visible **failure receipt**;
  they are not accepted as a completed source. The actor can explicitly
  clean up retained completed captures and retry only the failed chapter.
- An actor that has exhausted storage cannot create new collection jobs;
  an idempotent replay of an existing accepted job ID still succeeds.
  Quotas count **all verified raw text retained**, including old completed
  jobs after a process restart, and do not rely on a volatile counter.
- `GET /v1/source-books/jobs` includes an actor-only `usage` object:
  their retained raw bytes, their own limit, and effective space remaining.
  It never exposes the exact global usage belonging to other team members.
  The Jinja history view shows remaining source capacity.

Both `serve` and `source-worker` **must run with matching environment
configuration** and the same database. The defaults are designed for small
VPS installations; operators must size quotas against their actual disk
and backup capacity before enabling the crawler for multiple moderators.

**Important:** these are limits on durable source **body bytes**, not an
OS filesystem quota. SQLite WAL/database overhead, original catalog
manifests, all other chapter/import data, exported temporary files,
encrypted backups and filesystem allocation still consume disk.
`DELETE` releases logical quota but the SQLite file may not shrink until
safe WAL checkpoint/VACUUM during an approved maintenance window.
Use OS disk-space monitoring, backups and capacity alarms independently.

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


## RE-02-WEB-SOURCE-07 — Profiles and multi-user collection

Implementation: [issue #83](https://github.com/Lalalalendia/ararra/issues/83).
This extends the durable source jobs and verified text-storage quotas.
The profile budgets below are ReadEra's **own conservative constraints**,
not official site scraping permission.

| Site | Allowed HTTPS hosts | Minimum delay | Requests / UTC day |
|---|---|---:|---:|
| Royal Road | royalroad.com, www.royalroad.com | 4 seconds | 240 |
| WebNovel | webnovel.com, www.webnovel.com | 8 seconds | 100 |
| UUKanshu | uukanshu.cc | 6 seconds | 180 |

The typed source_site_profiles.py registry drives the strict source-to-host
allowlist and authenticated site directory. A new source must have a reviewed
parser and real permitted metadata fixture; an arbitrary wildcard or
moderator-supplied CSS profile is not acceptable. robots.txt can impose a
longer delay or forbid access; login, paid, CAPTCHA and JS/AJAX-only pages
are not bypassed. The release gate is [issue #65](https://github.com/Lalalalendia/ararra/issues/65).

### Site-wide concurrency and request caps

Both authenticated API import and source-worker share SourceRequestLimiter
against the **same SQLite database**. Before every outbound GET, including
robots.txt, a BEGIN IMMEDIATE transaction reserves a unique fenced token for
the site (www/non-www aliases share limits), honoring a one-request-at-a-time
lease, site interval, UTC daily counter and shared upstream cooldown.

Successful admissions count toward the daily site budget; a process crash
leaves a 75-second expiring lease, never a permanent network lock. A stale
token cannot release the successor. Regular pacing waits no more than 12
seconds; longer saturation returns HTTP 429 with Retry-After. An upstream
HTTP 429 triggers a bounded site-wide cooldown. UTC rollover resets only
the counter, not a live lease or cooldown. Network security checks and
robots.txt remain mandatory.

### Fair job distribution and durable waiting

New additive job fields are dispatch_turn, ready_at and defer_reason.
SQLite per-actor turns rotate moderators while per-job turns rotate a
moderator's multiple books; long books cannot monopolize all dispatches.
An existing verified chapter is immutable; a denied/busy site postpones
only the next chapter without a failure receipt or a manual retry count.

Pending bounds are three active source jobs per member, twelve per site,
and thirty-two global (queued + running). Existing logical text storage
bounds remain 256 MiB per job, and configurable 512 MiB per actor /
2048 MiB global by default. These limits **exclude** SQLite/WAL/temp,
indexes, media, other books and backups. Actual disk headroom and
backup/restore are still separate release gates. Job status exposes only
the owner's next_attempt_at and waiting_reason, never another moderator's
content, ID or aggregate disk usage.

### Source-worker operational gate

The current VPS runs API and Gemini worker but has **no systemd-managed
source-worker yet**. No PR here automatically activates crawling.
Before enabling a single reviewed source-worker: independently validate
the real public-site robots/TLS/TOC behavior under #65, enforce unit
RAM/CPU and disk/WAL/temp monitoring, verify backup/restore, and ensure
the Reader remains healthy on the memory-limited VPS. No credential
sharing with translation or parallel worker multiplication to force speed.

Tests: two independently opened SQLite handles and a parallel reservation
race; robots GET pacing, remote 429, day rollover, expiry fencing,
round-robin actor turns across worker restarts, deferred chapter restart,
global/site pending caps, actor-only API and Chromium waiting-state UI.


## RE-02 — Shared originals are team-readable (owner clarification, 2026-10-19)

**Owner decision:** All authenticated ReadEra team members may READ originals
and accepted translations. The existing BookLibrary already shares imported
chapters, translation revisions and context among the team; the book
owner_member_id field denotes editorial responsibility, not a read ACL.

Background source collection jobs had a separate actor-only SQLite boundary.
That boundary remains for controls, quotas, stage sessions and job status,
but does **not** restrict access to checksum-verified original text.

### Read-only source collection endpoints

- GET /v1/source-books/shared?limit=30 — bounded team index of **only**
  jobs with at least one stored verified original chapter, including the
  responsible moderator, source URL and number of verified checkpoints.
  No raw prose, private failures, quota of other members or secrets.
- GET /v1/source-books/shared/{job_id}?offset=0&limit=50 —
  paginated read-only identities of stored verified chapters.
- GET /v1/source-books/shared/{job_id}/chapters/{ordinal} —
  exactly one original chapter, after matching its manifest index,
  source ID, URL, byte count and SHA-256 against SQLite. Return 404 for
  nonexistent/unverified chapters; 409 for corrupted original data.
  Respond with Cache-Control: no-store, private; browser inserts only
  textContent, never executable HTML.
- GET /v1/source-books/shared/{job_id}/assembly — original TXT
  completeness/digest preview with no prose. GET the companion
  /export.txt?expected_manifest_sha256=...&expected_content_sha256=...
  only after complete SHA-verified selection. Partial books still permit
  reading their individually verified chapters.
- The Import page includes a compact collapsed Общие исходники команды
  reader, which clears previous text and selection on logout. It has no
  retry/cancel/stage/delete/edit controls.

GET requires the current valid team key or cookie for every request. Personal
job list, job DELETE, retry/cancel, mutable import stage and per-member
quotas remain actor-only. A team read does not create a duplicate checkpoint,
consume another member's quota, pay for Gemini, or fetch the source URL.
Role revocation must immediately prevent new HTTP reads; no browser
persistent cache is used.

### Retention and future dedup

This is **sharing of existing verified checkpoints**, not yet a global
physical deduplication cache. The original submitting moderator may delete
their own completed source job, and the shared source reader then stops
listing its checkpoints. Chapters previously confirmed into BookLibrary
remain team-shared and unaffected. A separately designed durable common
archive/reference-counted cache can later prevent deletion of the last
readable copy, and avoid repeat HTTP requests after same-source catalog
revision and rights checks. Do not conflate this with arbitrary license to
redistribute copyrighted material.

No additional source-worker is enabled on VPS and no real site is contacted
during this implementation. The live-site permissions/robots/TLS and VPS
physical RAM/disk/backup gates in issues #65 and #83 still apply.

## RE-02 immutable team archive on source job removal (2026-10-19)

[Follow-up #88](https://github.com/Lalalalendia/ararra/issues/88)
addresses the retention gap after team source reading [PR #87].
This is an archive/dedup first slice, not automatic site crawling.

### Verified text lifecycle

The existing source_collect_items records remain readable by the whole team
as long as a collection job exists. When its collector explicitly deletes a
complete/partial/cancelled job, SourceJobStore.delete_finished() checks each
successful chapter's manifest index, ordinal, source chapter ID, exact
allowlisted HTTPS URL, UTF-8 bytes and SHA-256. Under the SAME SQLite
BEGIN IMMEDIATE transaction, it inserts the immutable content-addressed
body and source revision in the team archive, then removes the personal job
and checkpoints. If integrity or the archive budget fails, the deletion
rolls back, preserving the originals and the job.

- source_original_bodies is keyed by SHA-256. Identical original bytes are
  stored once **inside the archive** and may be referenced by different
  source chapter identities.
- source_original_revisions stores exact reviewed site/book/chapter/URL/body
  provenance, the first collecting member and capture time. Revised text
  creates a new revision and never overwrites a prior one.
- The initial archive quota is 256 MiB of unique UTF-8 body bytes. This
  EXCLUDES SQLite indexes, free pages, WAL, other source jobs and offsite
  backup copies. Physical disk availability + an actual backup/restore
  remain release blockers on the memory-limited VPS.
- Existing source jobs are not auto-copied on startup. Additive SQLite tables
  preserve all old checkpoints. Archived text is created on explicit job
  deletion, one verified chapter at a time; never from an external network GET.

### Separate team archive API

All GETs require valid team authentication and return no-store responses:

- GET /v1/source-books/archive lists books with retained originals.
- GET /v1/source-books/archive/{archive_book_id} lists bounded, distinct
  source chapter revisions by content hash.
- GET /v1/source-books/archive/revisions/{revision_id} returns exactly one
  checksum-verified original. Browser rendering uses textContent, not HTML.
- DELETE /v1/source-books/archive/revisions/{revision_id} is ADMIN-ONLY:
  removes exactly this archive revision and the unique physical body only
  if no other revision references it. Personal source checkpoints and the
  accepted BookLibrary are NOT touched. A complete content takedown across
  all these stores is an additional audited operator procedure.

The Import page has two collapsed readers: live shared source jobs and the
durable archive. A moderator cannot purge archived revisions or manage
another moderator's collection job. The personal delete confirmation warns
that verified originals remain in the team archive; a full archive returns
409 instead of silently losing text.

### Still outstanding

Automatic reuse of an archived chapter to prevent repeated third-party
HTTP requests is NOT enabled. Such a feature requires site-specific
permission/robots/terms acceptance, freshness/version semantics and
race-safe attribution and cancellation. Bulk source-worker remains disabled.
Physical archive/WAL disk monitoring, backups/restore and permission
revocation are production release gates. Do not declare issue #88 fully
closed from this first archive slice.
