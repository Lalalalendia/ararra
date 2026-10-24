# M1p-Hash — Python oracle for numbered TXT source HTML

Status: **proof-only; not called by any write endpoint, stage confirmation, or production database code**.

## Why this is required

M1n/M1o stores canonical source records as `<h1>Глава N</h1>\n<p>...</p>` (or English Chapter N). Python's canonical `BookLibrary._normalized_source_hash` hashes the output of `strip_source_boilerplate(html, current_chapter_path=...)`, **not** a hard-coded Rust simplification. The Python `source_units` analyzer classifies the matching current `<h1>` as `CURRENT_CHAPTER_TITLE`, which is **translatable**, so for the strictly accepted canonical subset it keeps that H1 and the exact original HTML. An earlier Rust assumption incorrectly removed this title: Python/Rust byte parity CI rejected it. Other HTML contexts may still be modified by the Python cleaner, and must fail closed until separately proven.

Existing Rust `put_chapter_conservative` supports only verified source-cleaner subsets and refuses most chapter-context HTML. **Do not bypass that policy or treat a successful staging preview as permission to write the book.**

## First independently testable candidate

Rust `erida-source-clean::import_txt::clean_numbered_import_txt` accepts only:
- Exact `<h1>Глава N</h1>\n` or `<h1>Chapter N</h1>\n` with bounded decimal N, followed by complete no-attribute plaintext `<p>...</p>` paragraphs.
- Canonical single LF only between blocks; no arbitrary HTML, entities, attributes, inline nodes, embedded newlines or unknown heading forms. It delegates validation to the prior proven `clean_plain_paragraphs` source policy and accepts only bodies with zero removed nodes.
- Nonempty relative chapter paths without traversal; paths containing a numeric chapter index are included in the Python oracle test. Rust never opens or resolves the path.
- Otherwise returns `unsupported` without any hash, cleaned bytes or fallback; this includes the common `<br/>` and `&amp;` cases until exact Python DOM normalization is proven.

For matching cases, candidate output is the **unchanged canonical H1 + LF + paragraph HTML**, and SHA-256 of those exact UTF-8 bytes. The Python differential oracle must agree that `strip_source_boilerplate` removes **zero** nodes. Do not generalize this to arbitrary HTML or broaden any writer based on the proof.

## Test contract

`scripts/compare_rust_import_hash.py` builds canonical source from actual Python `parse_book_import` results, not handwritten stand-ins. It runs the production Python oracle `strip_source_boilerplate` and a standalone Rust JSONL probe `source-import-probe`, and requires:

1. All safe numbered Russian/English/Chinese narrative fixtures, multiple paragraphs, multiple chapters, and numeric/non-numeric destination paths produce **byte-identical cleaned HTML** and exact SHA-256; the current H1 must remain present and the oracle must remove zero nodes.
2. Proof-only inputs with untested suffix headings, entities, `<br/>`, scripts, footers/boilerplate, unbalanced/extra HTML, path traversal and zero chapter numbers return `unsupported` without source hash.
3. No SQLite access, external network requests, Nginx, Gemini or production writes.

CI builds the release probe and executes the differential test **after** regular Rust workspace unit tests and Clippy. A failed parity test is an actionable mismatch; do not weaken expected values or delete fixtures to make it pass. The output count and representative fixture coverage remain explicit in the test.

## Remaining prerequisites before M1p confirmation

- Expand the proof to the **actual widely-used** TXT grammar with multiple text lines/BR nodes, escaped entities and short paragraphs. Include realistic noisy scraper tails, extra/service chapters and numbered destination paths before enabling any writer.
- Implement `create / replace / skip` for every staged entry in one BEGIN IMMEDIATE transaction, with actor stage ownership, TTL, idempotent receipt, active moderator/owner checks, strict chapter id/index/path, old source CAS, and no partial state.
- Establish cross-runtime Python stage -> Rust confirm and Rust stage -> Python confirm, including byte-exact hashes, header CSRF, conflict handling and injected SQL failure.
- Run a real Chromium user import flow end-to-end, instead of claiming that the existing preview or source hash proof is full import support.

Project specification: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac).
