# RUST-M1n — read-only TXT import preview (strictly limited)

Source of truth: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac), `src/chaptera_server/book_import.py`, `src/chaptera_server/http/routers/imports.py`, and `src/chaptera_server/import_sessions.py`.

## Why this slice is separate

Python book import has **three distinct trust boundaries**:

1. Read-only multipart preview of candidates, duplicate chapter numbers, numbering gaps, body digests and unassigned preface.
2. Bounded actor-owned staging (`book_import_stages`), with TTL, input fingerprint, idempotency and private parsed chapter HTML.
3. Explicit actor-confirmed, revision-checked atomic import into `books` and `book_chapters`, with one action per detected source entry and explicit service/prefix decisions.

A preview is not a durable stage and must never silently mutate a book. M1n ports **only #1 for a proven UTF-8 TXT grammar**. Rust explicitly refuses unported cases instead of producing a plausible but incorrect book catalog.

## HTTP

`POST /v1/imports/preview` uses one authenticated multipart `file` field, no database writer, no stage creation, no files written to disk, no worker and no remote fetch. It is available even when `ERIDA_RUST_TEST_WRITES` is absent because it is provably read-only.

The whole multipart request has an approximately 12 MiB + 64 KiB hard Axum body cap; the file itself has a strict 12 MiB cap. Returned report is the Python `preview_book_import` JSON shape and private/no-store/nosniff response headers.

## Proved input subset

- Basename with `.txt` extension, strict UTF-8 or UTF-8 BOM, CRLF/LF.
- Either a single unnumbered plaintext chapter or unambiguous, standalone `Глава N` / `Chapter N` heading lines with 1–7 decimal digits.
- Plain text paragraphs, blank lines, quotation marks, escaped-looking text, Unicode letters and emoji.
- Duplicated chapter numbers and identical bodies are reported without silent renumbering. Gaps in detected numbers and unassigned preface are reported explicitly.
- Maximum 2,000 entries and two million characters per chapter.

Unsupported for now (HTTP 422, never a silent fallback): numbered headings with titles/suffixes, Markdown headings, extra/service/CJK entries, UTF-16 and cp1251, HTML, EPUB, DOCX, JSON metadata catalogs, ambiguous line separators and unsafe filenames. These remain available in Python, not Rust.

**This narrow subset is not import feature equivalence.** It is a deliberately fail-closed foundation for the first native source selection screen.

## Required acceptance

`scripts/compare_rust_import_preview.py` runs the real Python ASGI reference and Rust HTTP server on one disposable SQLite. It performs authenticated multipart upload of numbered, unnumbered, repeated, gapped, prefix-bearing and Unicode TXT fixtures and compares complete JSON, including hashes, samples and warning order. It checks unsupported inputs, unauthenticated request refusal, 12 MiB limit, no staging/confirmation route, and exact `books`, `book_chapters` and `book_import_stages` counts before and after. Runs with the Rust write gate **disabled**.

The repository's architectural route-inventory ratchet has exactly one reviewed exception to its non-GET disposable-writer rule: `POST /v1/imports/preview`, which is pure read-only. Adding any other mutating method outside the gate still fails the inventory test. The route-specific matcher and this allowlist are required by CI.

All existing Rust workspace unit tests/Clippy/release build, the previous Python/Rust differential suite, Python 3.11/3.12, Web and Chromium gates remain required on the exact PR HEAD before merging.

## Next independent deliverables, NOT implemented

- **M1o**: Rust actor-owned stage and GET/DELETE, TTL/idempotency, bounded saved payload and restart; only if parser corpus covers real TXT fixtures, still no book mutation.
- **M1p**: explicit create/replace/skip decision for every source chapter, CAS and owner permission, atomic confirmation and crash recovery, then real Chromium UI for upload/preview/confirm/book readback.
- **M1q**: parity for HTML, EPUB, DOCX, legacy encodings and service/CJK/extra headings; reject ZIP bombs and active HTML and preserve source provenance.
- Full browser UI to Rust and production Nginx/trusted IP acceptance, memory/RSS/PSS and disaster recovery are separate release gates.

No VPS, production SQLite, Nginx, Gemini key, paid provider or existing Python implementation is changed by this slice.
