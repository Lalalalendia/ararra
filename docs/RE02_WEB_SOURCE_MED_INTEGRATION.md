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

## Current integration slice (PR #53)

- Protected routes: `GET /v1/source-books/sites`, `POST /v1/source-books/catalogs`,
  `GET /v1/source-books/catalogs/{id}`, `POST /v1/source-books/catalogs/{id}/collect`,
  and actor-only `GET /v1/source-books/stages/{stage_id}/provenance`.
- All book URLs and discovered chapter URLs must match explicit MED supported public HTTPS sites. The
  network layer uses the code principles of the supplied book_crawler pinned-IP transport:
  addresses resolved and rejected if private/non-global **at connect time**, disabled environment proxies,
  strict TLS, no redirects, bounded streaming/decompression, content-type check, robots policy,
  and process-wide per-host pacing. No credential, JS challenge or paywall bypass.
- Actor-scoped catalog in SQLite has immutable SHA-256 of ordered (ordinal, source_id, URL)
  and short expiration. Changing the catalog requires another explicit preview.
- Up to 20 contiguous selected chapters per collection; every chapter must exist in the
  observed catalog. Partial or duplicate-body replies return an explicit failure receipt
  but **do not** create an import stage. A successful collection uses escaped `<h1>` headings
  and `<p>` bodies to prevent false chapter delimiters and is staged with existing
  `ImportSessions.stage`, which still requires `confirm` from the moderator.
- URLs, IDs and content digests are recorded in `source_book_stage_receipts`, without
  duplicating source prose. Stage actor ownership and ordinary source revision CAS stay in force.
- UI in `/app/import`: public site picker, URL, observed TOC, explicit start/end,
  partial failure diagnostics, handoff to existing import mapping and confirmation.

## Known limitations and follow-ups

1. This remains a Draft **until the exact final head passes Python 3.11/3.12,
   architecture check, Node and real Chromium**, with manual code/security review.
   No permanent deployment, no live crawling claim, no paid Gemini operation.
2. Each collection is synchronous, capped at 20 chapters and has no durable individual-chapter
   resume on crash. For whole books with thousands of chapters, add a bounded asynchronous
   worker/checkpoint layer; reuse MED/ZIP durability concepts without its unrelated ORM.
3. Library metadata and selected source chapter identities are durable, but only the
   staged and human-confirmed original chapters enter the existing BookLibrary;
   **the capture does not auto-translate or auto-publish**.
4. The HTML-only three-site adapters parse the **currently observed page**. They do not
   guarantee complete cross-page catalog coverage on every site. Explicit TOC gaps are shown.
   More profiles (including `book_crawler.zip` Rulate/NovelGo/IFreedom/Boovell/
   RanobeLib/MirNovel, Narou/Fanqie from MED) need independently proven parsers,
   per-site access/robots checks and corpus fixtures; they are not considered enabled.
5. SSRF tests include private/mixed DNS, redirect and over-budget bodies, but also add
   a production-network/TLS certificate integration gate before deployment. On
   httpx/httpcore transport internals change, the code refuses startup rather
   than quietly disabling pinned DNS.
6. Source site access restrictions, publisher rights and robots must be respected.
   A detected block is a visible failure, not an invitation to change UA or bypass it.

**Supported first slice:** public pages with numbered same-book chapter links on
Royal Road, WebNovel and UUKanshu. Existing file import remains unchanged.

