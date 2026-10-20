# RUST-01: modular Erida migration with measurable memory targets

## Current state

ReadEra/Erida runs Python FastAPI + a separate Python Gemini worker.
The source worker is opt-in and currently inactive. Moving it first cannot
reduce **current** idle memory. The continuously active API and translation
worker are the useful first targets.

The live VPS resource probe (20 Oct 2026) reported 891 MiB total and
427 MiB available; this is **not** the RSS of a Python process. The host
preflight in chaptera-site PR #77 adds read-only process RSS, PSS (when
accessible), VmHWM and cgroup memory to establish the missing baseline.

## Rust workspace implemented here

- `rust/crates/erida-auth`: team-key and browser-session lookup, immediate
  managed-token revocation and header-over-cookie precedence. No new auth.
- `rust/crates/erida-store`: read-only SQLite connections, existing JSON
  translation-job/event projections, bounded list results. No schema changes.
- `rust/crates/erida-api`: loopback-only Axum server with **GET-only**
  `/healthz`, `/v1/me`, `/v1/jobs`, `/v1/jobs/{id}`,
  `/v1/jobs/{id}/events` endpoints. Every private read rechecks the team
  credential; no unsafe unauthenticated fallback.
- `scripts/compare_rust_api.py`: Python ASGI reference vs Rust JSON/status
  comparison for static/managed/browser users, disabled/rotated keys, missing
  authentication, header precedence, job listing/detail/events and no writes.
  CI then starts both disposable HTTP processes and records /proc RSS/PSS.
  **This comparison is not equal-feature**: Python still has books, writes,
  editor, ImportSessions, sources, quotas, Gemini integration and more.

### Safety and rollout

This is a **research-only** Rust replacement candidate, not an installed API.

- Rust binds only to 127.0.0.1 (or IPv6 loopback) on a distinct port.
- Existing SQLite must exist, and Rust opens it READ ONLY. No migrations,
  WAL ownership changes, source downloads, credential issuance, Gemini calls,
  OAuth writes, or BookLibrary edits.
- Stop, fail closed on schema mismatch; do not treat successful `/healthz`
  as production readiness.
- The site deployment repo and VPS units are **not modified** by this PR.
- Never switch the live `/opt/chaptera-translator/current` symlink or Nginx
  to Rust until all existing Python API routes, sessions, error codes,
  cancellation, jobs and accepted translations have byte/semantic parity.
- Rust candidate memory is compared to **full Python API** only as an
  indicative idle/small-read lower bound, NOT a promised proportional saving
  or a justification for weakening memory gates.

## Roadmap and module boundaries

1. **RUST-01**: obtain measured Python API/Gemini worker RSS and PSS on
   actual VPS; compile this Rust read-only API slice; confirm auth and JSON
   parity on disposable SQLite and profile its idle memory.
2. **RUST-02**: port write paths for book and job storage in the same Rust
   process with transactional revision checks, leases, idempotency and
   cancellation. Prove concurrent Python/Rust behavior only on a cloned DB.
3. **RUST-03**: port Gemini HTTP transport, quotas and durable worker
   scheduler. Compare paid-attempt counters, source hashes, retries, fencing
   and checkpoint recovery against existing Python fixtures.
4. **RUST-04**: port Chaptera translation algorithms and quality gates
   incrementally with golden manuscript fixtures. Preserve resulting output,
   incomplete/partial conditions and manual import constraints.
5. **RUST-05**: migrate site adapters, parser and Drive integration once the
   always-running Python processes can retire. UI remains current JS/CSS.

Prefer Rust crates in **one API process** and a distinct credential-isolated
translation worker when needed. Do not create a microservice per crate.

### Memory target and measurement rule

Targets for discussion, **not achieved measurements**: API idle < 40 MiB RSS,
translation worker idle < 60 MiB RSS. Compare same machine, same warmed
workload, process tree, PSS, RSS, cgroup memory, and peaks under long chapter
processing. The goal is lower always-on RAM without degrading chapter quality.
