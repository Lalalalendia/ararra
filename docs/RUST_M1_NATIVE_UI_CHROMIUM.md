# RUST-M1k - existing UI served by Rust, real Chromium acceptance

Source: ERIDA-RUST-MASTER-01 (Notion page 40032a84beec81d09c63f735a37a3eac).

## Scope

A separate `erida-ui-preview` binary combines the existing Rust API with native
`/app` and `/app-static` routes. The normal `erida-api` executable and its default
startup remain unchanged. The preview requires BOTH `ERIDA_RUST_UI_PREVIEW=1` and
`ERIDA_RUST_TEST_WRITES=1`; AppState still requires a canonical, existing SQLite
file directly under the declared temporary root. Binding outside loopback fails.
No production SQLite, VPS, reverse proxy or paid provider is changed.

`build.rs` compiles the canonical Python-owned UI source files into the binary.
The existing shell and chapter partial have exactly two supported directives:
the fixed chapter-editor include and whitelisted active_page. New unknown Jinja
directives fail the build, rather than silently rendering a corrupted interface.
The static asset registry embeds only .css/.js/.mjs files and excludes tests and
symlinks. HTTP requests never read arbitrary host files or select file paths.
The native shell preserves the existing CSP, no-store, nosniff and frame-denial
headers; a runtime marker identifies the Rust preview in browser evidence.

## Honest capability boundaries

Supported preview pages: overview, books, editor, team. Import, jobs and cabinet
return explicit HTTP 501 and their navigation is marked unavailable. A visible
banner explains that import, QA, quotas, Gemini and general HTML compatibility
are incomplete. The QA module has a named Rust-only capability adapter: it shows
that QA was NOT performed and disables the scan. It never fabricates a QA report
or intercepts API fetches. The remaining canonical JS/CSS files are unchanged.

The first browser scenario deliberately accepts a single plain paragraph per
chapter. The current Rust acceptance/export grammar does NOT support every
output of the production textarea serializer (including multi-paragraph/newline,
entity and richer HTML cases). Expanding and oracle-testing that grammar is a
separate gate; passing this test does not establish full editor compatibility.

## Actual browser journey

`scripts/verify_rust_ui_browser.py` bootstraps the empty SQLite schema with store
constructors (no FastAPI application or Python HTTP server). It then starts the
Rust preview and creates a fixture book with two source chapters THROUGH RUST
HTTP. This is test setup, NOT proof that UI import/book creation works.

`tests/browser/rust-native-ui.mjs` launches real installed Chrome via Playwright:

1. Load the existing anonymous shell from Rust and verify security headers.
2. Log in the administrator through the actual form, issue a moderator using
   the team UI, hide the one-time key field, log out and log in that moderator.
3. Open the fixture book through the library UI. Edit and save a private draft,
   add/resolve a separate note, accept a different text and navigate to chapter 2.
4. Accept chapter 2, return to the library and restore the HttpOnly session on
   browser reload. No key/cookie/CSRF is injected by the test client.
5. Open the actual export dialog, preview the book, click both download buttons,
   and verify saved TXT/HTML bytes against response SHA256 headers. The output
   must contain accepted text, never source text, private draft or review note.
6. Log out, reload and prove session revocation. Repeat login/library on a narrow
   viewport and check horizontal overflow.

Every browser request targets the single Rust origin. External requests are
blocked, HTTP failures on the exercised API/static routes fail the test, and
JavaScript page errors fail acceptance. No mock HTTP responses or route.fulfill
are permitted. The original Python UI browser suite still runs independently.

## CI and evidence

`.github/workflows/rust-ui.yml` adds the distinct required acceptance job
`Native Rust UI real Chromium`. It runs renderer tests, builds the preview,
installs the existing browser harness and executes the scenario. Evidence:
four screenshots taken only after secrets are hidden; downloaded synthetic
TXT/HTML; and a sanitized evidence.json containing paths/status results and
hashes, not request bodies, headers, cookies or traces. Evidence retention: 7 days.

Existing Python 3.11/3.12, Web/Chromium and Rust parity checks must remain green.
No merge until the new browser job also passes on the exact PR head. A queued,
skipped or unrun job is not acceptance.

## Remaining release gates

Real book import/creation UI, QA and paid translation workflows; full textarea
HTML acceptance/export parity; all remaining API routes; live HTTPS/Nginx
trusted client-IP proof; backup/restore and memory gates. This preview does not
replace the production application and must not be described as doing so.
