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

## RUST-03a: standalone source-text classification, independently verified

The `erida-source-policy` crate ports the source-unit **standalone text**
classifier: NFKC, HTML entity decoding, zero-width/decorative separators,
promotion prefixes/fragments, anti-scraper domains, chapter-ending markers,
next-chapter navigation and position-dependent tail labels. Its prefix and
fragment tables are copied verbatim from
`chaptera_translation_core/utils/source_units.py`. Its API returns
`CONTENT`, `SOURCE_BOILERPLATE` or `NEXT_CHAPTER_BOUNDARY` and exactly
the existing Python `reason` text. It also independently exposes the
last-visible-node tail label predicate.

The offline JSONL `source-policy-probe` binary and
`scripts/compare_rust_source_policy.py` perform seeded tests against
Python's original `classify_source_text()` and
`is_source_tail_boilerplate_text()`. Corpus covers simplified/traditional
Chinese, Russian, English, promotional watermarks, obfuscated 101 domains,
empty/decorative text, full-width NFKC and narrative mentions.

This is NOT a full BeautifulSoup DOM port. The number, hierarchy and
serialization of HTML text nodes, title inference, contextual
`NEXT_CHAPTER_BOUNDARY`, block decomposition and canonical source hashes
still come from Python. **The new crate is intentionally NOT imported by
the Rust chapter writer**, so an insufficiently validated new text predicate
cannot silently widen the writable HTML scope or corrupt revision hashes.
Casefold-exceptional Unicode needs a separate corpus before source policy
can be declared complete.

## RUST-03b: read-only visible DOM text nodes

`erida-source-dom` is a separate Rust module wrapping `scraper`/html5ever.
It reads bounded, well-formed HTML and returns visible text nodes in source
order. Whitespace-only nodes, comments and script/style descendants are
excluded, matching Python `source_units._source_text_nodes` behavior for the
explicitly validated fixture scope. If a literal `<body>` exists, only that
subtree is considered, mirroring Python's `soup.body or soup` root choice.

The source-dom probe is **not wired into any production source hash, DOM
mutation, accepted-translation validation or source worker**. BeautifulSoup's
`html.parser` and html5ever use different recovery rules for malformed HTML.
A correct result for simple fragments or valid whole documents does NOT prove
their DOMs serialize identically after deletion of source noise. It also does
not yet prove exact text-ordinal DOM paths or current/next chapter boundaries.

`scripts/compare_rust_source_dom.py` compares actual `BeautifulSoup`
ordered text nodes with the Rust read-only probe for multilingual nested
inline tags, headings, paragraphs, explicit body roots, script/style/comments,
entities and deterministic well-formed combinations. This is a strict
differential test; any mismatch fails CI. **Do not loosen chapter-write
`UnsupportedSource` until full cleaning and canonical source hash parity
is demonstrated on both parser implementations.**

## RUST-03c: small proven-identity HTML source-cleaning subset

The `erida-source-clean` crate uses the independently accepted Rust
standalone source-text classifier to clean **immediately adjacent**
plain, attribute-free HTML paragraphs. This is the first Rust cleaning
module to *actually delete* known standalone promotional paragraphs,
last-visible-node tail hint labels and all nodes after an explicit
next-chapter tail marker. It then calculates SHA-256 of the cleaned HTML,
matching Python `strip_source_boilerplate` for the eligible grammar.

The intentionally narrow source contract rejects nested tags, entities,
empty nodes, control characters, Unicode outside tested ranges, numeric
chapter headings and numbered chapter-path context. Such documents still
must use the Python cleaner. This is **not** a full BeautifulSoup/html5ever
serialized DOM replacement and is **not integrated into the Rust writer**.

The `source-clean-probe` JSONL executable plus
`scripts/compare_rust_source_clean.py` compare Rust output to a live
Python `strip_source_boilerplate` oracle on synthetic multilingual
short/narrative/promotional/tail examples and a reproducible randomized
corpus. Each accepted sample verifies cleaned HTML byte equality and its
SHA-256 hash; unsupported complex markup must return an explicit
unsupported status. This prevents source-hash drift while establishing
the next concrete step toward lifting the Rust writer's >260-character
minimum without bypassing Python policy.

## RUST-03d: bounded source-cleaning integration with a disposable writer

The `erida-library` chapter writer now calls the independently verified
`erida-source-clean` Rust crate **before** it opens a SQLite write
transaction. For short simple HTML paragraphs in neutral chapter-path
contexts, the database still stores the **raw** `source_html` but saves the
SHA-256 of HTML after removing Python-compatible standalone watermarks,
last-node hints and explicit next-chapter tails. This is the existing Python
`BookLibrary.put_chapter` semantics, not a new canonicalization scheme.

A narrowed gate blocks numbered/heading-based chapter paths and ambiguous
numbered headings, all-removed/no-content cases, potential obfuscated
`101` domains, markup/entities and Unicode requiring unproven casefold
behavior. If the new cleaner rejects, the old long-paragraph proof gate
remains available; otherwise Rust returns HTTP 422 without modifying SQLite.

`scripts/compare_rust_clean_writer.py` tests Rust-created short chapters
replayed by Python, Python-created short chapters replayed by Rust, byte
equality of original HTML and Python-cleaned SHA-256, true competing
source-revision writes with one winner, preservation of stale accepted
translations, unsupported HTML non-mutation and CSRF.

**Still a research-only test SQLite slice.** No arbitrary HTML, source
worker, paid Gemini request, runtime switch or VPS deployment is enabled.
Production remains on Python until complete API+worker parity and memory
measurements. The new writer code must not be confused with a universal
BeautifulSoup replacement.

## RUST-03e: source text-node DOM path ordinals

Python `source_units._source_node_dom_path()` assigns deterministic text
identities based on each tag's **ordinal among all sibling tags** and the
text node's ordinal among all direct text siblings, including whitespace.
These paths are needed to preserve text-unit attribution as Erida migrates
away from the Python HTML processing pipeline.

`erida-source-dom::source_text_paths` now computes this key for the
read-only, independently verified subset of well-formed fragments and
documents with an explicit `<body>`. The html5ever-inserted wrapper for
fragments is excluded from the logical Python `[document]` root. Comment
nodes do not count as text siblings; whitespace-only nodes count toward
the ordinal but are excluded from visible results.

The CI-only JSONL DOM probe includes `text_paths` alongside its original
`nodes` output. `scripts/compare_rust_dom_paths.py` compares both text
and full ordinal path **byte for byte** with the original Python
`_source_node_dom_path`, across nested inline tags, sibling containers,
explicit-body HTML, encoded entities and deterministic well-formed samples.

Parser-dependent malformed HTML and implicit full-document repair remain
explicitly unsupported. This work does NOT serialize cleaned HTML, change
source hashing or change any Rust SQLite write allowance. No production
processes are affected.

## RUST-03f: read-only nested inline paragraph source-cleaning parity

The source cleaner now offers **another isolated, opt-in read-only function**:
`clean_inline_paragraphs`. It accepts immediately adjacent, attribute-free
`<p>...</p>` blocks with balanced nested `b`, `strong`, `i`, `em` and
`span` inline tags. It preserves the raw HTML bytes of all retained blocks,
removes only standalone plain-paragraph promotions/footers, and cuts the
explicit next-chapter tail. SHA-256 is computed from the resulting cleaned
HTML exactly as the Python `strip_source_boilerplate` policy does within
this intentionally limited grammar.

Important fail-closed boundaries:

- Mixed/ambiguous **nested text nodes** that Python could independently
  remove are rejected. The implementation does not guess BeautifulSoup's
  `NavigableString.extract()` or serializer behavior.
- No HTML entities, attributes, comments, empty/nonnarrative inline tags,
  unbalanced markup, script/style, numeric/CJK chapter-title context,
  unsupported scripts, or incomplete HTML.
- Only the specifically differential-tested, well-formed HTML subset may
  pass. Parser recovery of malformed HTML and arbitrary document wrappers
  remain Python-owned.

`scripts/compare_rust_inline_clean.py` verifies source-cleaned HTML bytes and
SHA-256 against the **real Python** BeautifulSoup source stripper on a
deterministic multilingual corpus with nested Russian/Chinese/English
paragraphs, standalone scraper watermarks and explicit next-chapter tails.
It also verifies that invalid cases fail closed. This result is **not wired
into the test SQLite writer**, much less the production API. A separate
cross-runtime CAS/parity gate and explicit approval would be required before
extending the set of writable source inputs.

## RUST-04a: admin-only membership list (read-only identity parity)

The experimental Rust API implements the existing
`GET /v1/admin/members` endpoint. Authorization requires successful
team authentication **and** the explicitly configured administrator
identity. Ordinary moderators and disabled/revoked credentials receive
the existing Python response and status; an invalid API-key header
takes precedence over any valid browser cookie.

The Rust projection mirrors Python `TeamCredentials.list_members`:
sorted union of configured and managed IDs, administrator/ordinary
moderator roles, current active/disabled status, and a coarse
`configuration`/`managed` credential source. Secret tokens, SHA-256
digests, audit entries and session identifiers are **never returned**.
No new team credential mutation, endpoint or permission is invented.

The existing Python-vs-Rust API differential suite now exercises
`/v1/admin/members` with anonymous, administrator, moderator, managed,
browser-cookie and invalid-key precedence cases, plus post-rotation
and post-disable snapshots against one disposable SQLite instance.

This is an independent API completeness step (not an extra resident
service), not a full API/worker replacement, and does not authorize
deployment or claim production memory savings.

## RUST-03g: disposable SQLite nested-inline chapter write parity

This phase extends merged RUST-03f and remains **experimental**.
Only with `ERIDA_RUST_TEST_WRITES=1` and an independently validated
temporary SQLite root may the Rust chapter writer try the bounded
`clean_inline_paragraphs` policy after the previous plain-paragraph
checker rejects a source. The same neutral chapter-path, heading, domain
and nonempty-cleaned-body checks remain in place; ambiguous nested
markers, HTML entities, attributes, malformed markup, numeric chapter
contexts and unsupported structures must fail **before opening a write
transaction**.

`scripts/compare_rust_inline_writer.py` starts the experimental Rust
loopback API alongside the actual Python ASGI application and opens
one shared, newly initialized temporary SQLite database. It verifies
source HTML immutability, Python-cleaned SHA-256 (not RAW hash),
bidirectional writer replay, one-winner expected-revision CAS across
the two runtimes, stale accepted-translation preservation, rejected
input nonmutation, and rejection of unauthenticated mutation.

RUST-03g does not cover arbitrary BeautifulSoup DOM mutation,
production chapter writes, complete book APIs, queued translation
workers, or the replacement of either resident Python process.
No production RAM reduction may be claimed from these isolated tests.
Promote only after an exact-head Rust + Python/Web/Chromium PASS
and an independently reviewed merge of this integration.

## RUST-M2e-1: native diagnostic-only CJK QA on disposable SQLite

The first **CPU-only** Python `ChapterQaStore.scan()` subset is now implemented
natively in `erida-library::qa_scan`, not delegated to Python. The existing
`POST /v1/books/{book_id}/chapters/{chapter_id}/qa` is registered only while
both `ERIDA_RUST_TEST_WRITES=1` and a verified temporary disposable database
root are enabled. Default runtime remains read-only for this route.

Scope is intentionally strict:

- Current accepted-source, accepted-translation and glossary revision tuple,
  hashes and actor identity must agree with the Python-owned SQLite snapshot.
- Only an **empty** glossary and empty version map, with a target consisting
  solely of nonempty, attribute-free `<p>` and `<h1>` elements and plain text,
  may yield a report. No HTML entities, nested elements, control bytes,
  comments or implicit DOM repair. Unsupported material receives HTTP 400,
  before any mutation; a previously existing immutable report is replayed.
- CJK residue covers exactly Python's bounded Han, Japanese and Hangul Unicode
  ranges and produces the same `CJK_RESIDUE` count, sample, severity, and
  message. No source/target alignment proof is inferred.
- A zero-finding report is valid **only** within this proven subset and is
  diagnostic, not an approval of translation quality or production readiness.
- Diagnostic computation occurs outside the `BEGIN IMMEDIATE` writer lock.
  The transaction revalidates accepted text, glossary content, versions and
  source/accepted/glossary hashes, and persists report+findings atomically.
  Concurrent Python/Rust scans return one stable idempotent report.

`scripts/compare_rust_qa_scan_baseline.py` creates one disposable Python-owned
database, then runs both APIs on matching chapters. It checks authenticated
CJK and clean reports, 400/401/404/409/422 and immutable denial, Python/Rust
idempotent replay, concurrent scan receipt, restoration from Python read API,
and disabled production route. No Gemini, Google Drive or VPS is involved.

**Not yet migrated:** morphology, glossary-driven terms and entity names,
complex manuscript markup, coverage/alignment verification, real translation
work, persistent production SQLite writes, remote export and complete Python
API replacement. Python `ChapterQaStore.scan` remains authoritative for all
unsupported QA cases. The old VPS is unavailable; further Rust migration uses
GitHub Actions and disposable local data only.
