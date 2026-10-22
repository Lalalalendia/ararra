# RUST-M1h — moderator key administration under disposable write gate

Source: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac), Python team_credentials.py and http/routers/identity.py, [RE03_KEY_ONLY_AUTH](RE03_KEY_ONLY_AUTH.md).

## Admin-only HTTP actions

- POST /v1/admin/members — issue one 256-bit OS-random token to a new moderator (HTTP 201).
- POST /v1/admin/members/{member_id}/rotate — generate a new token; automatically invalidate previous tokens and browser sessions (HTTP 200).
- POST /v1/admin/members/{member_id}/disable — disable member and invalidate all sessions; never orphan an owned book (HTTP 200).

GET /v1/admin/members already exists in Rust and never includes secret digests. All new writes are registered only when `ERIDA_RUST_TEST_WRITES=1` and `ERIDA_RUST_DISPOSABLE_ROOT` points to an explicitly canonical temporary SQLite. Production DB, running Python API and VPS remain unchanged.

## Invariants

1. Only the configured global admin may manage moderator credentials; the administrator's own secret can only change in protected host configuration.
2. New IDs obey the Python regex: first ASCII letter, then 0–63 ASCII letters/digits/period/underscore/hyphen. No collisions with configured IDs or stored members.
3. Each issue/rotate returns the new token exactly once. SQLite stores SHA256 only; no raw keys in audit, URLs or logs.
4. Each issue/rotate/disable and its audit event share one BEGIN IMMEDIATE transaction.
5. Rotation of disabled/static moderators is permitted where Python permits it, reactivates a managed token, and makes old cookies invalid on next request.
6. Disable is refused if any book still has owner_member_id=member; explicit CAS transfer to another active moderator must happen first.
7. Disable in the same transaction cancels queued/retry_wait translation jobs, flags running jobs for cooperative cancellation and creates job_events with detail=member_disabled. Completed jobs and existing accepted translations stay untouched.
8. An already-disabled moderator returns the disabled projection without appending another disable event.
9. Admin cookie mutations require CSRF. Header API-key precedence remains enforced.
10. Default Rust process has no credential mutation routes; runtime remains loopback-only.

## Test gate

Run `python scripts/compare_rust_admin_credentials.py rust/target/release/erida-api`.

The script runs the real Python ASGI and Rust loopback processes against one disposable SQLite and covers: unauthorized/forbidden/reserved/invalid IDs; Rust issue to Python auth and Python issue to Rust auth; concurrent same-member creation; static and dynamic rotations; cookie/session invalidation; admin cookie CSRF; owner-handoff requirement; cancellation semantics for queued, retry_wait and running jobs; atomic events; idempotent disable and absence of raw credentials in persisted DB.

Full Rust unit tests, Clippy, release build, all previous cross-runtime tests, and Python 3.11/3.12 + Web + Chromium must all pass on the exact head before merge.

## Remaining blockers

This does not authorize production Rust logins or writer access. The trusted external-IP policy behind Nginx is not implemented, and Python-only source import, editor/QA workflows, full HTML parsing, paid worker, DR and memory acceptance still block a Python-less release. Do not count route parity as full production-readiness.
