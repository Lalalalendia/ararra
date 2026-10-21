# RUST-M1e — composite book workflow, exact Python export boundary

Source: [Notion ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac), issue #103.

## Why this gate

Independent route tests are necessary but do not establish a user's end-to-end book outcome. The M1e gate crosses actual HTTP APIs and shared SQLite with one synthetic book, then consumes the result through the existing release-grade Python export contract.

This gate **does not claim complete Rust M1, browser-login parity or Rust export**. It makes missing pieces explicit rather than assuming accumulated routes form a product.

## Concrete user path

1. Authenticate two distinct team users against Python and Rust.
2. Create book using Rust POST; compare Python and Rust persisted directory reads.
3. Save glossary using Rust PUT with version fencing and compare Python book read.
4. Save source chapter A with Rust; save chapter B with Python; compare chapter and list reads between both runtimes.
5. Save a shared editorial draft through Rust, verify Python read and keep private draft out of book exports.
6. Accept chapter A in Rust, chapter B in Python; refuse stale acceptance and update chapter A in Python.
7. Read accepted history and restore a previous accepted revision in Rust. Assert the new revision is visible through Python.
8. Preview and download exact accepted TXT and HTML through **Python VerifiedBookExport**. Check source/draft exclusion, response security headers, content SHA256, pinned manifest SHA256 and correct two-chapter sequence.
9. Refuse stale pre-restore preview after restore, refuse missing trailing chapter, and refuse an accepted translation tied to an old source after a Rust source edit.
10. Prove cookie-session GET, mutation CSRF and header API-key priority.
11. Check real SQLite accepted/draft/glossary immutable revision count.

The test starts actual Python ASGI and Rust loopback against one pre-created disposable SQLite. No fake HTTP transport stubs for the Rust side, no production deployment.

## Source-of-truth limitations

- The Python exporter remains the reference oracle. If Rust export routes are still absent the script checks this explicitly. If native Rust M1f routes are present, it requires matching preview JSON and byte-identical TXT/HTML for the supported fixture, not merely route availability. Wider BeautifulSoup HTML sanitization is still a separate production-parity gate.
- Token-based member authentication and Python-issued cookie sessions work. Rust does **not** provide complete login/logout/session endpoints yet. That is another M1 gate.
- A translation is explicitly accepted from deterministic fixture text, not produced by a live Gemini worker. Gemini worker migration remains a separate gate.
- Editorial notes are covered in M1d (#126); M1e starts from the already accepted M1c main, so it does not depend on an unmerged PR.
- No source website crawler, remote imports, paid translation, Drive or VPS changes.

## CI and merge rules

Run the composite test after Rust release-mode candidate compilation and alongside the existing comparison suite:

    python scripts/compare_rust_book_vertical.py rust/target/release/erida-api

The Python 3.11/3.12, Web and Chromium checks and Rust tests, Clippy and all prior cross-runtime scripts must also PASS on exact PR head. Passing this script is not proof of full API parity, but failure reveals a real broken user path crossing independently ported pieces.
