# RE-02 WEB SOURCE 01 — MED crawler ingestion contract

Scope: import the user's existing public-page parsing logic from `Lalalalendia/med` into the closed-team ReadEra server as a **read-only, transport-independent foundation**. Do not copy the entire Books Platform crawler ORM, account service, payment/monetization flow or bypass protected content.

## Source inventory

- `med/tools/novel-ingest/source_ingest.py` @ `5046ff53207ce2fc89fb83277d9ceec1fee30935`: Royal Road, WebNovel and UUKanshu HTML catalog/body parsers, book/chapter identity, chapter-number conflict detection. The CLI's direct HTTP, file writes and publication handoff are deliberately not reused.
- Uploaded `book_crawler.zip` (60 Python source files): profiles for Rulate, NovelGo, IFreedom, Boovell, RanobeLib and MirNovel; domain cooldown, bounded HTTP, SSRF-resistant transport patterns, chapter checkpoint/identity normalization, locking detection, staged imports. Most profiles depend on `app.core`, `app.repositories` or another application's ORM and **are not drop-in imports**.
- Other MED scripts (`narou_ingest`, `fanqie_ingest`, `xszj_ingest` and site-specific probes) are candidates for separate adapters only after license, source access, safety and corpus verification.

## Implemented in this PR

- `source_catalog_med.py`: ported catalog/body parsing for three public-site adapter families without its network/CLI layer.
- `source_collect.py`: explicit source catalog hash and per-chapter selection; bounded body extraction with SHA256 receipts, chronological assembly, duplicate-body detection, failures retained rather than silently omitted.
- Offline tests with only injected fake HTML fetchers. Importing these modules does **not** open a socket, save any book to the library, publish, enqueue Gemini, or change the original source.

## Remaining activation gates (MUST NOT claim completed)

1. Implement trusted `FetchHtml` using **pinned validated public DNS/IP**, HTTP(S) allowlisted hosts and paths, no redirect to other origins, TLS verification, per-domain limits, bounded streaming response and content-type checks. Reuse *principles* from uploaded `book_crawler/http_transport.py` rather than importing private Books Platform services.
2. Add authenticated `/v1/source-books/catalog` and `/v1/source-books/collect` to a **separate router**, bounded durable jobs, cancel/resume, explicit per-source permissions, and real browser UI for site URL, metadata, TOC, chapter selection, status and error explanations.
3. Hand off complete selected chunks to existing `ImportSessions` staging and `confirm`. Keep actor ownership, the two global roles, SHA/expected-revision CAS, explicit create/replace/skip, and accepted history. Large books must be chunked without representing a partial chunk as full book. For the 12 MiB upload / 24 MiB stage limits, explicitly batch with stable book identity and receipts.
4. Add corpus fixtures for each enabled site and adversarial tests for login HTML, paid chapters, CAPTCHA, pagination, duplicate chapters, chapter renumbering, redirects/rebinding, 429/5xx, non-text response, disconnected worker, crash/retry and source mutation during collection.
5. Verify current site permission and terms; do not bypass paywalls, access controls or rate restrictions. Test end-to-end source→preview→selected chapters→staging→confirm→translation on owned or otherwise authorized source material.

This is **one first engineering slice**, not user-complete site import. It should remain draft until the above application wiring is available and CI passes. No live crawling or Gemini billable calls are performed by this PR.
