# Chaptera Server Translator — enforceable architecture boundaries

This contract applies to **Lalalalendia/ararra**. Neither the Windows
`full_cycle_app` nor `chaptera-site` is a runtime dependency or a write target.

## Owned layers and directory layout

```text
ararra/
  src/
    chaptera_translation_core/       portable translation and QA (no FastAPI or Qt)
    chaptera_server/
      api.py                       FastAPI composition only
      auth.py                      transport-independent key validation
      book_import.py               document parsing (no routes)
      library.py                   SQLite books, chapter revisions, glossary
      editorial.py                 SQLite drafts, notes, accepted history
      storage.py / worker.py       durable jobs and retry
      http/
        schemas.py / dependencies.py
        routers/
          health.py, identity.py, books.py, imports.py,
          editorial.py, jobs.py, ui.py
      web_ui/
        templates/shell.html
        templates/partials/chapter_editor.html
        static/js/readera.mjs           authenticated UI controller and transport
        static/js/import-model.mjs      deterministic import decisions
        static/js/library-view.mjs      live table, lazy chapter TOC
        static/js/editor.mjs            original and RU editing
        static/js/editorial-panels.mjs  review notes and accepted history
        static/css/                    local UI themes and vendored import CSS
  tests/browser/                    isolated Playwright real-Chromium harness
  tools/check_architecture.py       entire-repo Python AST/asset/JS ratchet
```

The **only maintained browser interface** is FastAPI-served Jinja2 + vanilla
JavaScript under `/app` and `/app-static`; API resources remain under
`/v1`. The obsolete top-level React/Vite `web/` workspace was removed
after [RE-05 four-screen engineering acceptance](READ_READERA_RE05_VISUAL_ACCEPTANCE.md).
The browser uses an **in-memory** `X-API-Key`, never cookie login or
local/session storage. The HTML route does not expose private data.

## CI invariants

1. **HTTP endpoint decorators** belong to `http/routers/` only. `api.py`
   composes each owned router exactly once; routes and OpenAPI are tested.
2. **Business services** never import the HTTP/ASGI layer. Core never
   depends on server, Qt or FastAPI/Starlette.
3. All private `/v1` methods require team authentication. Only `/healthz`
   is public. Exactly two global roles: admin and moderator. No implicit admin.
4. `web_ui/templates/` and all required CSS/JS modules **must exist**. The
   obsolete `web/` path must remain absent. JS files have per-module size
   budgets; use small feature modules rather than growing `readera.mjs`.
5. **No client persistence of API credentials**, Vite-secret bundles or
   import from the Windows application. Direct network `fetch` is owned
   by `readera.mjs`; feature modules are given the authenticated request
   callback. Static scans are guardrails, not formal security proofs.
6. Python ownership/size ceilings: `api.py` 90 lines, each router 240,
   `schemas.py` 200, `dependencies.py` 160.
7. Python 3.11 and 3.12 suites + two offline transport smokes, Node 22
   syntax/runtime import-model tests and **real Chromium** desktop/mobile
   E2E are mandatory. Protected CI retains the historical
   `Web boundaries and production build` check name while testing Jinja
   without npm install or a React bundle. Playwright stays in
   `tests/browser` and still uses its npm dependencies.

## Adding features

- New server endpoint: extend or create an `http/routers/` owner, Pydantic
  schema and framework-independent service; update route contract tests.
- New UI feature: add a focused module under `web_ui/static/js/`, wire it
  through the authenticated controller, change Jinja template and add both
  `node --check` and real Chromium test coverage; never store bearer keys.
- New translation behavior: modify core or worker and expose it through an
  authenticated router. The UI must not directly import Python or receive
  secrets embedded in templates.
- Schema edits must preserve source/draft/accepted CAS and history; do not
  equate an accepted revision with publication.

## Local regression commands

```sh
python tools/check_architecture.py
pytest -q tests/test_architecture.py tests/test_jinja_ui.py
node --check src/chaptera_server/web_ui/static/js/readera.mjs
node --test src/chaptera_server/web_ui/static/js/import-model.test.mjs
```

To run the real Chromium tests, install Python dependencies and the
`tests/browser` package, start a **disposable** local API with test credentials
and execute `npm test` in `tests/browser` as documented in the workflow.
Do not run against production data. This project is **not deployed** by
GitHub Actions; VPS bootstrap/HTTPS remains a separate gated action.

## Provenance

The policy pattern was informed by
`Lalalalendia/rar2` CI/fanout boundary guards, not copied as a monolith.
Limits and owned paths above are specific to ararra.
