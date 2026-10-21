# RUST-M1g — key-only browser sessions (disposable safety gate)

Source: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac), [RE03_KEY_ONLY_AUTH](RE03_KEY_ONLY_AUTH.md), Python session.py and team_login_throttle.py.

## Routes and observable user flow

- GET /v1/session: read-only session introspection, requiring a valid readera_session cookie. A bare X-API-Key must NOT authorize this route.
- POST /v1/session: member key exchange for a 256-bit random Secure-or-nonsecure (configured), HttpOnly, SameSite=Strict session cookie, role and purpose-separated CSRF digest; status 201. Registered only with the existing canonical disposable SQLite test-write gate.
- POST /v1/session/logout: CSRF-fenced revocation for active sessions and cookie deletion, 204 even for a missing, expired or already revoked cookie. Also test-write-only.

Existing Rust authentication already validates the Python session table on every request and rejects sessions tied to rotated or disabled member credentials. Rust now also issues compatible sessions in a disposable test runtime. The real Python API and VPS do not change.

## Rate-limit contract and the production boundary

Like Python BrowserLoginThrottle, Rust charges the shared SQLite browser_login_rate IP bucket before attempting key authentication, using BEGIN IMMEDIATE. It preserves attempts across successful logins, locks after 8 failures in 15 minutes, and limits to 30 attempts in the window. Database stores SHA256 of the domain-separated peer identifier, not plaintext IP.

IMPORTANT: This candidate explicitly ignores X-Forwarded-For and uses the immediate kernel TCP peer from Axum ConnectInfo. A loopback-only Rust process behind Nginx would see Nginx's local peer rather than the external client. Therefore **production Rust login must remain disabled** until a separately verified trusted reverse-proxy source-IP contract and live 429 test are complete. Do not loosen the disposable-write gate or infer release readiness from these tests. The systemd/Nginx listener and actual VPS are unchanged.

Session tokens are generated from 32 bytes of OS entropy with a URL-safe base64 representation and stored only by SHA256; the member credential is stored only by SHA256 as in Python. No passwords, user-provided keys, remote calls, Gemini or raw token logging.

## Required acceptance

Run the real Python ASGI and Rust loopback on the same disposable SQLite:

    python scripts/compare_rust_browser_sessions.py rust/target/release/erida-api

The test covers:
1. Two-way cookie compatibility: Rust login -> Python session read; Python login -> Rust read; both logout directions.
2. GET /session requires cookie even if an API key is present; invalid X-API-Key does not replace a valid session cookie on this special route.
3. Mutations require Python-compatible HMAC CSRF; logout is idempotent.
4. Key rotation and disable revoke old sessions without reissuing secrets.
5. Shared SQLite IP lockout after 8 failures, alternating Python/Rust login and ignoring spoofed X-Forwarded-For values.
6. No raw key, cookie or IP in persisted SQLite.
7. Rust unit tests + clippy, Python 3.11/3.12, Web/Chromium and all previous book/export parity jobs on exact PR HEAD.

## Remaining work

This does not implement admin member create/rotate/disable Rust routes; it relies on Python reference routes in the test. Browser rate-limit real client-IP provenance behind Nginx, 429 on actual VPS, full UI screenshot acceptance, and a complete Python-less book flow remain open. Rust write methods remain entirely behind disposable SQLite safety gate.
