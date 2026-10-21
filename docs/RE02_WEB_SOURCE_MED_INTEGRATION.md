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


## RE-02 additional reviewed site profiles (proposed, PR pending CI)

Three new opt-in source families extend the same existing actor-owned
preview/selection/temporary collection/explicit import-confirmation flow:

| Profile | Accepted root | Chapter discovery | Content |
|---|---|---|---|
| \`wikisource_en\` | \`https://en.wikisource.org/wiki/<work>\` | Same-work subpage links in visible lists/tables | \`.mw-parser-output\`, without navigation/edit widgets |
| \`wikisource_ru\` | \`https://ru.wikisource.org/wiki/<work>\` | Same-work subpage links in visible lists/tables | As above, only allowed host |
| \`ao3\` | \`https://archiveofourown.org/works/<digits>\` | Public chapter-index anchors on work page or an explicitly linked exact-work \`/navigate\` page | Work \`#chapters\` article only |

These profiles do NOT accept arbitrary domains, non-HTTPS requests, credentials,
URL query/fragment switches, subscriber content, adult-consent bypass,
redirects, private-address DNS resolution, or guess unpublished chapter URLs.

Some Wikisource works consist of a single transcluded HTML page or contain
non-subpage chapter links. Those are currently unsupported: the collector
reports \`catalog_missing\` instead of inventing chapters. Wikisource
text is generally free-licensed or public domain but the exact work's license,
attribution and translation conditions still matter.

AO3 prohibits bots/scraping for various disallowed purposes, including
collecting content for **commercial generative AI**. This adapter is designed
for private, user-directed handling of publicly readable stories only, not
automatic public hosting, resale, bulk AI harvesting, or distribution.
Restricted works are intentionally unsupported. Applicable source rights,
platform terms and robots policy must be checked before operational rollout.
See https://secure.ao3.org/tos_faq and
https://www.mediawiki.org/wiki/Wikimedia_APIs/Access_policy

The UI picker now derives its entries from the same authoritative Python
SITE_PROFILES registry used by URL validation. Adding a profile requires:
a reviewed host allowlist, book/chapter identity constraints, offline
catalog/body fixtures, a live robots/TLS/access gate, and full CI. A name
alone is not an enabled parser.

Explicitly NOT enabled:
- Narou HTML ingestion: Shosetsuka ni Narou terms prohibit automated
  access/collection except via developer-provided APIs; the official
  novel API does not expose chapter bodies.
- Kakuyomu: the existing MED workflow demonstrates page structures, but
  the use and rights conditions and server access need a separate review.
- Royal Road 403: this is an upstream block; adding other profiles does
  not fix it. The collector never attempts an anti-bot bypass.

Tests: \`tests/test_source_more_profiles.py\` and existing RE-02 security,
source collection, catalog identity and Chromium suites. No production
network access or credentials are used by these offline tests.
