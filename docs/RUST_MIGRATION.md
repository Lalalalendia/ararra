# RUST-01: modular Erida migration with measurable memory targets

## Current state

ReadEra/Erida runs Python FastAPI + a separate Python Gemini worker.
The source worker is opt-in and currently inactive. Moving it first cannot
reduce **current** idle memory. The continuously active API and translation
worker are the useful first targets.

The live VPS resource probe (20 Oct 2026) reported 891 MiB total and
427 MiB available; this is **not** the RSS of a Python process. The host
preflight in chaptera-site PR #77 adds read-only process RSS, PSS (when
accessible), VmHWM and cgroup memory to establish the missing baseline.

## Rust workspace implemented here

- `rust/crates/erida-auth`: team-key and browser-session lookup, immediate
  managed-token revocation and header-over-cookie precedence. No new auth.
- `rust/crates/erida-store`: read-only SQLite connections, existing JSON
  translation-job/event projections, bounded list results. No schema changes.
- `rust/crates/erida-api`: loopback-only Axum server with **GET-only**
  `/healthz`, `/v1/me`, `/v1/jobs`, `/v1/jobs/{id}`,
  `/v1/jobs/{id}/events` endpoints. Every private read rechecks the team
  credential; no unsafe unauthenticated fallback.
- `scripts/compare_rust_api.py`: Python ASGI reference vs Rust JSON/status
  comparison for static/managed/browser users, disabled/rotated keys, missing
  authentication, header precedence, job listing/detail/events and no writes.
  CI then starts both disposable HTTP processes and records /proc RSS/PSS.
  **This comparison is not equal-feature**: Python still has books, writes,
  editor, ImportSessions, sources, quotas, Gemini integration and more.

### Safety and rollout

This is a **research-only** Rust replacement candidate, not an installed API.

- Rust binds only to 127.0.0.1 (or IPv6 loopback) on a distinct port.
- Existing SQLite must exist, and Rust opens it READ ONLY. No migrations,
  WAL ownership changes, source downloads, credential issuance, Gemini calls,
  OAuth writes, or BookLibrary edits.
- Stop, fail closed on schema mismatch; do not treat successful `/healthz`
  as production readiness.
- The site deployment repo and VPS units are **not modified** by this PR.
- Never switch the live `/opt/chaptera-translator/current` symlink or Nginx
  to Rust until all existing Python API routes, sessions, error codes,
  cancellation, jobs and accepted translations have byte/semantic parity.
- Rust candidate memory is compared to **full Python API** only as an
  indicative idle/small-read lower bound, NOT a promised proportional saving
  or a justification for weakening memory gates.

## Roadmap and module boundaries

1. **RUST-01**: obtain measured Python API/Gemini worker RSS and PSS on
   actual VPS; compile this Rust read-only API slice; confirm auth and JSON
   parity on disposable SQLite and profile its idle memory.
2. **RUST-02**: port write paths for book and job storage in the same Rust
   process with transactional revision checks, leases, idempotency and
   cancellation. Prove concurrent Python/Rust behavior only on a cloned DB.
3. **RUST-03**: port Gemini HTTP transport, quotas and durable worker
   scheduler. Compare paid-attempt counters, source hashes, retries, fencing
   and checkpoint recovery against existing Python fixtures.
4. **RUST-04**: port Chaptera translation algorithms and quality gates
   incrementally with golden manuscript fixtures. Preserve resulting output,
   incomplete/partial conditions and manual import constraints.
5. **RUST-05**: migrate site adapters, parser and Drive integration once the
   always-running Python processes can retire. UI remains current JS/CSS.

Prefer Rust crates in **one API process** and a distinct credential-isolated
translation worker when needed. Do not create a microservice per crate.

### Memory target and measurement rule

Targets for discussion, **not achieved measurements**: API idle < 40 MiB RSS,
translation worker idle < 60 MiB RSS. Compare same machine, same warmed
workload, process tree, PSS, RSS, cgroup memory, and peaks under long chapter
processing. The goal is lower always-on RAM without degrading chapter quality.

## RUST-02a: one-step job write transaction integration

Prototype Rust API now implements `POST /v1/jobs` and
`POST /v1/jobs/{job_id}/cancel` on a **disposable SQLite database only**,
with `BEGIN IMMEDIATE`, actor-scoped `Idempotency-Key`, the same canonical
`JobSpec` SHA-256 digest, atomic queue events, cancellation by current state,
session revocation and HMAC CSRF verification for cookie-authenticated writes.

The write routes are **not registered by default**. Both
`ERIDA_RUST_TEST_WRITES=1` and `ERIDA_RUST_DISPOSABLE_ROOT` are mandatory;
the canonical SQLite path must be a regular file directly within that
declared directory, which must itself be below the system temporary root.
The existing live DB under `/var/lib` is therefore not eligible.

`scripts/compare_rust_writes.py` exercises mixed Python/Rust execution on
one temporary SQLite: Python-created jobs replay in Rust and vice versa;
cross-process simultaneous submissions with the same idempotency key
produce a single job and a single queue event. Cancellation of queued,
running and terminal jobs is checked, as are actor scoping, disabled/rotated
tokens, HMAC CSRF and explicit-header precedence. No source fetch or
Gemini/Drive requests are permitted.

**RUST-02a does not replace the full API.** Book/accepted-text writes,
outbox, source crawl, Gemini transport, paid quotas, advanced glossary
contract and all editor/import endpoints still use Python. Additional
conformance evidence is required before any production migration.

## RUST-02b: book directory and ownership CAS

A new `rust/crates/erida-library` crate owns the book-directory JSON
projection, deterministic chapter/accepted counts, book creation with
immutable owner audit, and compare-and-swap ownership transfer against the
existing Python SQLite schema. No schema changes, automatic migrations,
source-text writing, accepted translations or glossary mutations.

On the experimental Rust Axum process:

- GET `/v1/books` and GET `/v1/books/{book_id}` require member auth.
- POST `/v1/books` and PUT `/v1/books/{book_id}/owner` require the
  same **explicitly enabled disposable SQLite** write mode as RUST-02a.
- New book ownership is restricted to active moderators; only the admin
  can assign another moderator or transfer existing ownership. Auth is
  rechecked against the current credential row for each request.
- `BEGIN IMMEDIATE` and expected owner_revision ensure exactly one
  outcome under races. A no-op owner reassertion writes no audit event.

`scripts/compare_rust_books.py` exercises shared-DB Python/Rust creation
replay, owner CAS races, immutable audit history, directory parity,
ownership policy, CSRF and token header precedence.

This still is NOT a full replacement for Python BookLibrary. In particular,
chapter source revisions, accepted translations, editing and glossary flows
must be independently ported and golden-checked before any release.

## RUST-02c: chapter reading with stale-acceptance parity

The `erida-library::chapters` module implements two authenticated **GET-only**
routes using the existing Python-owned SQLite schema:

- `GET /v1/books/{book_id}/chapters` — stable chapter-index/id ordered
  directory, without original or accepted text bodies.
- `GET /v1/books/{book_id}/chapters/{chapter_id}` — complete single-chapter
  metadata, original HTML, hashes and accepted translation status.

The current accepted text is usable only if it is nonempty and tied to the
current source revision, exactly as in Python BookLibrary. Old accepted HTML
remains visible in the chapter detail but is explicitly `accepted_current=false`
after any source revision change; the book directory count must decrease.

`scripts/compare_rust_books.py` checks Python vs Rust response JSON/status
before acceptance, after acceptance and after subsequent source editing. It also
verifies that lists never return source or accepted HTML, even when a chapter
is already translated. No Rust mutations of chapter text or glossary are enabled.

**Not yet migrated:** chapter source hash normalization, saving chapter text,
accepted-translation revision writes, translation-context calculation, editor
drafts, import/exports and actual Gemini translation. A production switch still
requires their respective contracts to be implemented and accepted.

## RUST-02d: strict source-chapter writes (limited, no policy drift)

`erida-library::put_chapter_conservative` adds the exact Python
`BEGIN IMMEDIATE` and expected-source-revision CAS shape for creation and
updates of source chapters. The new PUT route is *disabled by default*, just
like prior Rust writes: `ERIDA_RUST_TEST_WRITES=1` and a canonical, previously
created SQLite file directly inside `ERIDA_RUST_DISPOSABLE_ROOT` under
the system temporary directory are mandatory. No production writer exists.

### Why source-hash behavior is restricted

Python computes
`SHA256(strip_source_boilerplate(source_html, current_chapter_path))`,
**not** SHA-256 of arbitrary raw HTML. Python's classifier strips site footer,
watermarks and appended next-chapter navigation using BeautifulSoup, NFKC and
text-node context. Until all that has an independent Rust implementation, it
would be incorrect to silently hash the raw source.

The first proven identity subset is deliberately narrow:

- one or more *immediately adjacent*, attribute-free `<p>text</p>` elements;
- no nested tags, entities, control codes or markup inside text;
- each node has **more than 260 NFKC alphanumeric codepoints**, excluding the
  decorative `丨` character, and no digit sequence potentially containing
  a deobfuscated `101` promotional domain token;
- all other HTML fails with a clear HTTP 422 and never reaches the database.

These conditions exclude the known shorter-than-220-character next-chapter
markers, shorter-than-260-character standalone promotional text, standalone
chapter-ending markers and domain-based policy removals. For accepted inputs
the Python strip operation is identity, so Rust must write the raw UTF-8 hash.
This is an intentionally small **tested compatibility subset**—not yet a
complete parser. Typical short dialogue paragraphs, nested or malformed HTML,
and promotional material remain Python-only.

`scripts/compare_rust_chapter_writes.py` checks accepted fixtures against
the actual Python stripper; then uses shared disposable SQLite to compare
Python/Rust INSERT/replay, two-writer revision conflicts, hash identity,
uniqueness constraints, preserving old accepted translations as stale after a
source revision change, and explicit fail-closed unsupported HTML.

### Remaining after RUST-02d

- Port **full** source classification/normalization and compare against the
  Python golden fixtures for every historical chapter type.
- Port accepted-translation writes with text validation, immutable revision
  archive and source-revision fencing.
- Preserve glossary/context/recovery/editor contracts and translation worker
  before replacing the actual Python API process.

## RUST-02e: accepted translation revision writer (deliberate subset)

The experimental Rust `PUT /v1/books/{book_id}/chapters/{chapter_id}/accepted-translation`
now implements Python's `BEGIN IMMEDIATE` acceptance transaction:
- exact `accepted_revision` and `source_revision` fencing;
- identical `accepted_html` replay without duplicating audit events;
- atomic increment of accepted revision and immutable
  `book_accepted_revisions` history with the authenticated actor;
- rejection of acceptance against changed source revision, even when the
  previous translated text is still retained as stale;
- authenticated, mandatory browser-CSRF and disabled-by-default
  disposable-only write route.

Python validates accepted translation by parsing HTML with BeautifulSoup,
then computing a bounded visible text tail. Until that parser is ported, Rust
only accepts one or more adjacent attribute-free `<p>text</p>` fragments
with visible alphanumeric text. It rejects unsupported markup/entities via
HTTP 422 **before writing** rather than silently approving an empty
translation or losing semantic text.

`scripts/compare_rust_acceptance.py` exercises Python/Rust bidirectional
accept/replay, concurrent contradictory accept operations (one 200, one 409),
source revision changes, independent accepted-revision history checks and
cookie CSRF. This does NOT yet implement full HTML acceptance or any
paid translation worker. The existing Python server remains authoritative.
