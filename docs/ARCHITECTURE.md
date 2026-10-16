# Chaptera Server Translator: enforceable architecture boundaries

This is the architecture contract of **Lalalalendia/ararra**. The original
Windows `full_cycle_app` is neither a runtime dependency nor an update target.
The separate `chaptera-site` repository is likewise not modified.

## Ownership and directory layout

```text
ararra/
  src/
    chaptera_translation_core/     pure translation and QA; no HTTP, Qt or app imports
    chaptera_server/
      api.py                     create_app() composition and compatibility exports only
      auth.py                    pure token verifier (transport-neutral failure)
      book_import.py             document parsing, no endpoints
      library.py                 shared books, revisions and glossary data
      storage.py                 durable translation job queue
      worker.py                  translation jobs and retry
      http/
        schemas.py               Pydantic HTTP requests
        dependencies.py          team auth, dependency and exception mapping
        routers/
          health.py              public liveness only
          imports.py             authenticated preview
          books.py               book and chapter endpoints
          jobs.py                translation queue endpoints
  web/                            independent React/TypeScript/Vite workspace
    src/
      main.tsx                    browser entry
      app/                        composition and application-level styling
      routes/                     single browser route registry and layout
      features/                   feature-owned pages/components
      shared/api/                 browser request transport (no client tokens embedded)
      shared/ui/                  reusable presentation primitives
    scripts/architecture.test.mjs dependency-free boundary checks
  tools/check_architecture.py     Python AST and structural guard
  tests/test_architecture.py     Python regressions, routes and auth contracts
```

## Invariants checked in CI

1. **HTTP endpoints live only under `http/routers/`.** `api.py` constructs
   FastAPI and includes routers; it cannot grow endpoint handlers again.
2. **Business logic never imports the HTTP layer.** `auth.py` emits an ordinary
   exception; only `http/dependencies.py` translates it to HTTP 401.
3. **Translation core is framework-agnostic.** No imports of server, Qt,
   FastAPI or Starlette.
4. **Every HTTP method/path is unique.** A snapshot test checks the v0.6 route
   inventory; every non-health route has a team-auth dependency.
5. **Web is a separate workspace.** Browser routes are in `web/src/routes/`,
   requests in `web/src/shared/api/`, features do not bundle server packages.
   Shared web code must not import features. No team/Gemini key is shipped or
   persisted in JavaScript; authenticated views await a proper session layer.
6. **Growth limits are enforced:** composition root ≤90 lines, each API router
   ≤240, shared HTTP schemas ≤200, dependencies ≤160, frontend TS/JS module
   ≤300. Split a module by ownership rather than increasing ceilings casually.

The guards use Python AST for Python import/decorator checks and Node built-ins
for browser boundaries. They intentionally do **not** claim to prove every
possible dynamic import or semantic dependency; review remains necessary.

## Extending the application

- New feature endpoint: add a router module in `http/routers/` or extend an
  existing owner, with its DTO in `http/schemas.py` and service logic in a
  framework-agnostic module. Add its `include_router` and update route
  contract tests together.
- New browser screen: create `web/src/features/<feature>/`, register the path
  in `web/src/routes/paths.ts` and `router.tsx`, use `shared/api/client.ts`
  or an adjacent module inside `shared/api/` for HTTP calls.
- New background translation behavior: implement in `worker.py`/core, expose
  over a router; never import React or route handlers into the worker.

## Commands

```sh
python tools/check_architecture.py
pytest -q tests/test_architecture.py
cd web
node --test scripts/architecture.test.mjs
npm install
npm run build
```

`node --test` and Python tests require no npm registry access; `npm install`
and React/TypeScript production bundle require package downloads. GitHub
Actions runs the Python checks under 3.11 and 3.12 and a separate Node 22 web
job. The browser shell is **not** a production authenticated interface.

## Provenance

Architecture guard design borrows the **policy and CI-test pattern**, not
monolithic scripts, from `Lalalalendia/rar2`'s `tools/ci/source_fanout_budget.py`,
`tools/test_public_boundary_guard.py`, and `tools/ci/test_pub_editor_test_path_ownership.py`.
The line ceilings and route owners here are defined for ararra and are not
asserted to be enforced by the other repository's workflows.
