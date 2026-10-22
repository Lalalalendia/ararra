# RUST-M1i — single-runtime cookie-authenticated book journey

## Evidence goal

The previous composite M1e test used Python for part of the workflow, so its success could conceal cross-service dependencies. M1i uses the **Rust HTTP server exclusively**, with one disposable SQLite and two independent cookie-authenticated user sessions. It is a strict vertical API check, not a live browser/Chromium UI check.

The Python library is used **only before Rust starts** to initialize the existing SQLite schema via QueueStore, BookLibrary, EditorialStore, TeamCredentials, TeamSessions and BrowserLoginThrottle. No FastAPI app, Python HTTP request, Gemini call, remote source, external host or production SQLite is used in the tested journey.

## Scenario and acceptance

1. Refuse anonymous access, log in the administrator using the Rust session endpoint and restore its cookie.
2. Show that an administrator operation without cookie CSRF is refused. Use Rust admin POST to issue a one-time moderator token; reject moderator access to admin management.
3. Log in the new moderator against Rust; create an owned book with a valid cookie and CSRF, and refuse a missing-CSRF book creation.
4. Save a glossary with revision CAS, create two source chapters in the proven plain-paragraph grammar and read both back.
5. Create a private editorial draft; create and resolve a review note with per-note CAS; verify that both stay out of downloaded content.
6. Accept both translation fragments through Rust, reject stale accepted revision, create a new accepted revision and restore the earlier one; verify immutable history [3,2,1].
7. Get the Rust-native TXT and HTML preview/download for declared final chapter 2. Check 64-char manifest SHA256, bytes SHA256, ETag, Content-Disposition, nosniff/no-store, two-chapter ordering and exclusion of original text, draft and note.
8. A missing third chapter and a changed source after acceptance must cause fail-closed preview and HTTP 409 download. After reaccepting the new source, only the newly pinned manifest authorizes download.
9. Rotate the moderator token through Rust admin; the previously valid Rust cookie immediately fails. The newly issued token authenticates and can read existing book state.
10. Disable is rejected while the moderator owns the book; perform Rust admin CAS owner transfer to a different active moderator, then disable and prove all its cookies can no longer access the book.
11. Logout administrator through Rust and verify revocation. Inspect SQLite records directly for book ownership, accepted revision history, editorial note/draft, management audit and absence of raw credentials and cookies.

The code is in `scripts/verify_rust_native_cookie_book_e2e.py`, invoked as a required step in `.github/workflows/rust-api.yml` after previous Rust/Python parity suites:

    python scripts/verify_rust_native_cookie_book_e2e.py rust/target/release/erida-api

All checks must PASS on the exact PR HEAD, along with Rust unit/Clippy and Python 3.11/3.12, Web and Chromium. The same disposable root + explicit Rust test-write environment gates remain active; this test does **not** make Rust write routes available in production.

## Limitations: no false claim of readiness

- This is an HTTP cookie/session workflow, **not an actual Chromium browser using the production UI**. The production HTML/Jinja/JS shell is still served by Python.
- Only the proven conservative Rust source and accepted HTML grammar is exercised; arbitrary source HTML, BeautifulSoup normalization, style attributes and complex formats can fail closed.
- The accepted translations are deterministic fixtures; paid Gemini worker, source parsing/import, QA and automatic translation are not part of this gate.
- Trusted end-user IP attribution through Nginx has not been implemented for Rust browser login. The current Rust per-TCP-peer rate limiter would treat the proxy itself as the client.
- Before a production cutover, require a real installed/VPS UI-to-Rust browser test, trusted reverse-proxy IP and 429 test, backups/restore and memory/RSS/PSS gates, plus all missing Python method contracts.

This test is deliberately composed only of already merged Rust M1b–M1h components. It adds no production routes, schemas or server settings and must never be treated as proof of production readiness by itself.

Source: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac).
