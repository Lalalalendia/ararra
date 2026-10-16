# Web workspace (React + TypeScript)

The browser app lives **only** in `web/`, outside the published Python package.
This is an initial compilable shell (routes + health check), **not yet** an authenticated book UI.

```
web/src/
  app/          Application bootstrap and global styles
  routes/       Route ownership, URL registry, page composition, layout
  features/     Book / dashboard (later import, editor, jobs, glossary) features
  shared/api/   Single browser network and typed API boundary
  shared/ui/    Reusable presentation primitives; no direct API requests
```

Run `npm install && npm run dev` from `web/`. Before installing dependencies,
`node --test scripts/architecture.test.mjs` validates structural boundaries;
`npm run check` executes both structural tests and a production bundle. The Vite dev proxy forwards `/v1` and
`/healthz` to `127.0.0.1:8000`. Use `npm run build` for a production asset build.
Route fallback for SPA URLs must be configured in the eventual reverse proxy.

**Security:** the frontend deliberately makes no authenticated `/v1` requests in
this first architectural cut. Do not put team API tokens or Gemini secrets in
JavaScript bundles, browser storage or environment variables prefixed with
`VITE_`. Before enabling private pages, implement server-managed login/session
cookies, CSRF protection and role checks. The open `/healthz` endpoint returns
only `{"status":"ok"}`.

`tools/check_architecture.py` and `tests/test_architecture.py` guard dependencies,
router ownership and module/file boundaries. Any new route/feature must update
those tests in the same PR; do not mix Python routes with frontend route files.
