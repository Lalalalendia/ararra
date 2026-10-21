# RUST-M1b — editor drafts, real Python/Rust SQLite parity

Source: [Notion ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac) and [migration tracker #103](https://github.com/Lalalalendia/ararra/issues/103).

## Concrete user scenario

After the existing book creation, chapter source save, glossary CAS, and
accepted-translation gates, an authenticated editor can **open a shared chapter
draft, change it, safely rebase it after source/accepted changes and reopen the
exact durable result**. This is not a production UI replacement.

Implementation:
- `erida-library::editorial` reads the *existing* Python SQLite
  `editor_drafts` table and writes the same CAS transaction and append-only
  `editor_draft_revisions` audit. No table migration.
- `GET /v1/books/{book_id}/chapters/{chapter_id}/draft` is an authenticated
  read; no draft returns revision 0 plus current source/accepted revisions.
- `PUT` is registered **only** with `ERIDA_RUST_TEST_WRITES=1` on a database
  inside an explicitly declared, canonical, disposable temporary directory.
  Member/API-key and cookie-session/CSRF policies match existing Rust gates.
- Preserve Python conflict precedence: chapter revision mismatch before draft
  CAS; an old draft requires explicit `rebase=true`. Identical replay with
  `rebase=false` neither changes timestamps/actor nor adds an audit revision.
  Even `rebase=true` with unchanged text records the new source/acceptance
  base in a new revision.
- Source HTML, accepted translation, translation jobs and shared book metadata
  **never change** when writing editor drafts. No Gemini calls.

## Acceptance tests

`scripts/compare_rust_editor_drafts.py` creates a **single temporary SQLite**,
runs the actual Python ASGI app and Rust loopback against it, and proves:
1. Empty draft serialization (including null author/time) is identical.
2. Rust save -> Python read; Python save -> Rust read.
3. Idempotent replay and Python/Rust concurrent CAS (one winner, 409 loser).
4. Accepted revision change refuses a stale draft until explicit rebase;
   source revision change also fails closed.
5. Team-key auth, browser cookie CSRF, invalid-header precedence and missing
   chapters; no stale data leaked through response.
6. Actual immutable `editor_draft_revisions` rows survive re-instantiating the
   Python store. Source HTML and already accepted RU stay intact.
7. Rust native unit tests for revision fencing, replay and transaction history.

The workflow runs all established Rust/Python parity tests, Rust clippy and the
new script. Existing Python 3.11/3.12/Web/Chromium gates remain mandatory.

## Not yet in Rust

Reviewer notes, draft UI shell rendering, historical accepted revisions,
restoration, QA workflow, full export and session/login endpoints. This PR
adds a meaningful part of M1, not end-to-end functional parity.

No production SQLite writes, worker switch, cloud deployment, live source
crawl or paid translation. Real VPS memory, encrypted off-host backup/restore
and source consent gates are unchanged.
