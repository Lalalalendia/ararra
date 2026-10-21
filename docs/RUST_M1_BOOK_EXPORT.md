# RUST-M1f — native accepted-book TXT/HTML export (conservative subset)

Source of truth: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac) and Python VerifiedBookExport in src/chaptera_server/book_export.py.

## Delivered HTTP candidate

- GET /v1/books/{book_id}/export/preview?expected_last_chapter=N
- GET /v1/books/{book_id}/export?expected_last_chapter=N&expected_manifest_sha256=<64 lowercase hex>&format=txt|html

Both routes use per-request team token or authenticated cookie-session checks. They open SQLite read-only, perform no schema migration, no Gemini calls and no output when an original or accepted revision is stale.

The Rust exporter:
1. Captures book and chapter rows under one SQLite read transaction.
2. Requires an explicitly supplied final chapter number, 1 to 2000.
3. Enforces 32 MiB input and 36 MiB output boundaries, ordered chapter indices and no missing or extra chapters.
4. Refuses not-accepted, old-source accepted, unsafe or unproven HTML.
5. Computes Python export-manifest v1 SHA256 from chapter IDs, paths, source hash/source blob hash, accepted hash, source/accepted revisions, title and glossary revision.
6. On download recomputes the entire snapshot and requires the preview SHA256. A stale receipt returns HTTP 409 with a diagnostic preview, not partial bytes.
7. Produces TXT/HTML, X-ReadEra-Manifest-SHA256, X-ReadEra-Content-SHA256, ETag, Content-Disposition, nosniff and no-store/private response.

## Critical limitation — not broad HTML parity

M1f deliberately restricts accepted HTML to a well-formed, lowercase, attribute-free, entity-free tag subset: p, div, span, strong, b, em, i, u, blockquote, ul, ol, li, h1, h2, h3, and HTML void br/hr serialized as br/ and hr/. A leading exact "Глава N" or "Chapter N" heading is stripped as in Python. Uncertain fragments, arbitrary attributes, entities, malformed nesting, comments, source markers, service annotations and links are NOT exported.

Python BeautifulSoup can safely sanitize a broader grammar, including harmless attributes and HTML entities. For those inputs Python preview may report ready=true while this Rust candidate reports ready=false with reason rust_unsupported_fragment. This is fail-closed behavior, not full functional parity. Do not promote Rust exporter as a full Python replacement until expanded golden corpus and strict byte/status/error/header comparisons cover the existing accepted formats.

## Integration and check

scripts/compare_rust_book_export.py starts the real Python ASGI application and Rust loopback against the same disposable SQLite. It checks exact preview JSON, full TXT and HTML bytes, both SHA256 receipts and response headers, missing source, obsolete acceptances, stale manifest, unauthenticated requests, source/draft isolation, and an attribute-bearing accepted fragment that Python permits but Rust refuses.

rust/crates/erida-library/src/book_export.rs has unit tests of the manifest, heading removal, unsafe HTML and stale-source blocking. Rust CI runs those plus the cross-runtime script and prior parity tests. Python 3.11/3.12, Web and Chromium CI remain required. No merge on an untested HEAD.

## Dependency coordination

PR #127's composite E2E test currently asserts Rust export preview is unavailable. Once M1f is merged, that assertion must be replaced with Rust/Python equality for supported fragments. Until then M1f is a separate Draft PR based on main; it does not depend on the unmerged #126 editor-notes PR.

Still absent in Rust: browser login/logout/session issuance, full BeautifulSoup-compatible exporter, complete chapter/source grammar and Gemini worker. Full M0 RSS/PSS, memory and disaster-recovery acceptance remain open. No VPS deployment.
