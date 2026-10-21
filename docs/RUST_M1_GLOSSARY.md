# RUST-M1: bounded glossary revision and audit parity

References: [Notion M1 master](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac),
[migration issue #103](https://github.com/Lalalalendia/ararra/issues/103).

This PR adds one **test-only** user-facing book feature on Rust:
`PUT /v1/books/{book_id}/glossary`. It is available only when the Rust
loopback app is started with `ERIDA_RUST_TEST_WRITES=1` and an independently
verified `ERIDA_RUST_DISPOSABLE_ROOT` under the OS temporary directory. No
production SQLite writer, background translation worker or deployment is enabled.

## Contract

- Exact client revision CAS; `BEGIN IMMEDIATE`, one winner on cross-runtime
  races; mismatched revision 409 and missing book 404.
- Stable Python `json.dumps(..., ensure_ascii=False, sort_keys=True)`
  SQLite text encoding including spaces, nested object order and Cyrillic/CJK
  UTF-8. This is essential so a Python replay does not spuriously create a
  second immutable glossary revision.
- One monotonic glossary revision and one `book_glossary_revisions` audit
  record with authenticated actor and timestamp, committed with the book row.
  Exact same-state replay leaves revision/audit unchanged.
- For already stored chapters, matching scoped overrides that disagree in
  `rus` or `note` fail 422 without changing data. Disabled or unavailable
  authority skips application of those rules but preserves the supplied RAW
  evidence; canonical chapter source is untouched.
- Shared team member keys or authenticated browser cookie+CSRF required.
  Explicit invalid X-API-Key header never falls back to a valid cookie.
- Rust response shape is the existing full book object, compatible with the
  Python API for tested input grammar. Error mapping preserves the Python
  conflict detail including source term/chapter path.

## Offline proof

`scripts/compare_rust_glossary.py` starts the existing Python ASGI app plus
Rust loopback against **the same newly created test SQLite**. It executes
Rust→Python and Python→Rust no-op replay, exact JSON text checks, a real chapter
scoped conflict, a DISABLED-authority variant, concurrent writer CAS, audit
chain, stale revision, absent book, missing auth, cookie CSRF and invalid header
precedence. All inputs are synthetic, and **Gemini requests = 0**.

Existing Rust workspace tests, Clippy, Python 3.11/3.12, Web/Chromium and every
earlier Rust parity script must pass on the exact PR HEAD before merge.

## Scope not yet ported

The Rust API is still a research candidate. This change does not implement
production glossary migration, complete browser sessions, the Gemini
scheduler, translation context/atoms or any real source fetching. Any exotic
JSON scalar edge cases outside the proven cross-runtime corpus remain
subject to further differential tests; fail closed rather than allow silent
revision drift. Measured RAM savings or production-readiness are **not**
claimed. Off-host restore and actual VPS RAM gate remain separate.
