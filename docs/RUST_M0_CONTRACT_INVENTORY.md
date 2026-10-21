# RUST-M0 — executable HTTP migration gap report

Owner issue: https://github.com/Lalalalendia/ararra/issues/103
Notion: https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac

Status: diagnostic, not Rust cutover and not feature equivalence.

## Run offline

```sh
python scripts/rust_contract_inventory.py --check --json /tmp/erida-rust-m0.json
```

This script instantiates FastAPI against a disposable SQLite QueueStore and
synthetic identity. It never reads production settings or credentials, calls
Gemini, downloads novel chapters, or starts a server.

FastAPI introspection collects exact method/path/handler, nested member/admin
authentication, manually implemented browser-session routes, cookie-CSRF
classification, request field types/names, and declared success status.
The existing static ROUTE_CONTRACT in tests/test_architecture.py remains the
approved inventory, so unreviewed additions/duplicates cause a hard error.

The Rust Axum Router is inspected for registered GET routes and explicitly
disposable-writes gated by state.test_writes_enabled. Exposing a Rust write
outside its test fence or declaring a route absent from Python fails CI.
Missing Rust methods are presented as migration gaps, not blockers during M0.
UI shell routes and static files are listed separately from JSON API methods.

## What this does not prove

HTTP route declarations do not prove dynamic error precedence, privacy headers,
side effects, request schema parity, response semantics, or actual auth behavior.
The output marks behavioral parity unproven even for Rust registered routes.
Likewise the old GET-only Rust RSS reading cannot be compared to full Python
API+Gemini worker as a like-for-like production performance improvement.

## CI triggers and stale branches

The Rust parity workflow now also runs when Python API/HTTP/authorization,
storage, book, or source translation reference contracts change. Superseded
branch checks are cancelled; main acceptance never is. A metadata-only JSON
artifact documents missing routes on each commit; no secrets or chapter text.

- PR #115: OPEN Draft, merge conflict; previous CI green on old HEAD only.
  Reconcile main or close as superseded; do not merge conflicting code.
- PR #116: OPEN Draft, mergeable but CI from its old HEAD. Refresh on main
  with exact-head CI before taking any result into production consideration.

## Remaining M0 gates

1. Differential HTTP status/headers/response/errors and access boundary matrix.
2. Same-machine equal-feature Python API/worker and Rust candidate workloads,
   measuring RSS/PSS/cgroup peak, CPU and p95, not GET-only-vs-full comparison.
3. Golden anonymized SQLite snapshots and full book/atom/translation fixtures.
4. Proven encrypted off-host restore, adequate real VPS RAM, rollback: separate
   blockers. No production API writer or active Timotxt source worker.

Owner tracker: RUST-01 #103. No deployment, production database writes,
provider charges or external source fetch in this PR.
