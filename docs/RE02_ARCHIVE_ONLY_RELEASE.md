# RE-02 — Archive-only Erida release (Full Cycle ZIP is the source parser)

## Product contract

The desktop Full Cycle App parses sites and exports versioned
`full_cycle_app.chapter_exchange` v1 `kind=source` ZIP files. Erida acts as a
review/import/translation/library operator service, **not** as a web crawler.

The archive-only release keeps:

- `POST /v1/imports/{preview,stages}`, stage confirm, immutable original and
  working SHA-256 checkpoints, and authorised archive-original readback.
- TXT/HTML/EPUB/DOCX import compatibility, book library, editorial history,
  glossary/QA, translation jobs and personal Gemini quotas.
- Explicit consented batch-to-Google-Drive outbox and the separate export
  process. No implicit paid generation or Drive writes.

The release does **not** run or expose:

- Site discovery, TOC page traversal, source job creation/retry or web fetching.
- `/v1/source-books/*` crawler and source-history routes or the legacy
  browser source form. The old source archive tables and files are **not**
  deleted or migrated. Existing imported book chapters are still visible.
- `chaptera-server source-worker` when `CHAPTERA_ARCHIVE_ONLY=1`.
  The service must remain disabled/inert in systemd.

## Startup and build switches

- `CHAPTERA_ARCHIVE_ONLY=1` (strict boolean): the API does not import,
  initialise or register the optional crawler modules or its SQLite catalog/job
  objects. The browser only imports source-crawler JS in full mode.
- `CHAPTERA_ARCHIVE_ONLY_BUILD=1 python -m pip wheel --no-deps .`: produce
  the lean wheel without `chaptera_server/source_*.py`,
  `http/routers/source_*.py` and `web_ui/static/js/source-*.mjs`.
- Deploy **both flags together**. Regular builds and unconfigured development
  retain the former full mode. Do not start the slim wheel with the flag unset:
  omitted modules intentionally cannot provide the old crawler API.

Build and runtime flags are different by design. Runtime mode can also be
used with a full wheel to measure memory effects without changing a package.

## Memory and disk scope

Previous API start eagerly imported the crawler and initialised source catalog,
selection, job/review/archive and request-limiter objects, plus crawler routers.
These are now skipped entirely in archive-only runtime. The source-worker
is already inactive on the existing VPS, so disabling it again does not
recover any *currently resident source-worker process* memory.

The source/crawler Python modules in current main total roughly 221 KB of
uncompressed source (25 files), excluding UI and dependency overhead. This
is **not** a claim of 221 KB RSS saving. The public wheel build test reports
exact full/lean wheel bytes. Measure actual API RSS/PSS before/after on the
*same VPS* after each startup, with the same traffic and memory conditions,
before claiming a quantitative benefit.

## Acceptance gates

1. Exact-head Python 3.11/3.12 + Web + real Chromium tests pass unchanged
   in default full mode; archive-only tests prove no `/v1/source-books/*`
   routes or initialised crawler state.
2. A real Full Cycle App exporter ZIP fixture passes preview -> stage ->
   explicit confirm -> original/working readback in archive-only API.
3. CI builds both wheels, proves the site-crawler Python and JS modules are
   absent in the smaller wheel, and runs a new installed wheel in its
   isolated venv with ZIP preview and crawler-route denial.
4. Update only the **staged** chaptera-site release build to use both flags.
   Existing offline stage assumptions that require source-worker features
   must be replaced with archive-only assertions rather than skipped silently.
5. Run real VPS read-only resource preflight and isolated stage, verify exact
   SHA, backup/restore and rollback. Do not activate automatically. Last
   documented `MemAvailable` was below the installer 320 MiB floor.
6. Operator explicitly authorises installation, then smoke
   `/app/import` + ZIP upload + readback, and checks API/worker/Reader
   health and absence of any new source worker.

**No current data deletion**: the SQLite crawler tables, old source checkpoints,
shared original archives, registered content and original files remain intact
for offline recovery or a previous-version rollback. In archive-only UI they
are not browsable through the crawler's old endpoints; existing book-library
entries and freshly imported ZIP originals remain accessible through normal
book/import APIs.
