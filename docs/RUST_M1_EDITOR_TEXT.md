# RUST-M1l — ordinary textarea text through acceptance, history and export

## Why this is a user-visible correctness fix

M1k proved the browser journey with one plain paragraph per chapter. The real `editor.mjs::textToHTML` emits multiple `<p>` elements separated by LF, internal `<br>` elements, and five escaped character forms. The old Rust validator rejected those outputs even though the UI itself generated them. A green one-paragraph test was not sufficient evidence for normal editing.

## Narrow supported contract, unchanged UI

`acceptance::editor_html` recognizes attribute-free `<p>...</p>` paragraphs, adjacent or separated by a single LF, internal `<br>`, plain Unicode text and exactly `&amp;`, `&lt;`, `&gt;`, `&quot;`, `&#39;`. Entities are decoded ONCE after structural recognition. Unknown entities, attributes, raw nested markup, malformed tags and control characters remain rejected. Existing previously proved plain-paragraph acceptance is retained as a fallback.

Acceptance, CAS and revision history preserve the exact input HTML bytes. They never rewrite escaped text or normalize the stored translation. The SHA-256 manifest remains a receipt for the original accepted HTML, not the exported normalized fragment.

For export only, the parsed text is serialized like the Python reference: `<br/>`, minimal text escaping of `&`, `<`, `>`, literal quote characters in text, original paragraph LF separators and the existing plain-text whitespace rules. Escaped `<script>` or `<b>` entered as text stays escaped and cannot become an element. Doubly escaped entity text is not decoded twice. Matching first chapter headings are removed exactly for the tested ASCII-number heading forms; non-ASCII numeric heading candidates remain explicitly unsupported rather than silently diverging.

No generic HTML sanitizer or new broad HTML permission is introduced. The existing export checks for source markers and external URLs run before both parser paths. Complex attributes, styles, malformed DOM recovery and general HTML entities still require separate parity work. Whitespace-only content remains invalid; visible punctuation and a literal ampersand now match the Python visible-text contract.

## Evidence gates

`tests/browser/editor-text-cases.mjs` executes the pure encoder functions extracted from the actual unchanged canonical `editor.mjs`. It produces 49 deterministic cases, including paragraphs, line breaks, both quote forms, ampersands, angle brackets, escaped script-like text, literal entity spelling, CRLF, Unicode, tabs, NBSP, headings and source-marker/URL refusals.

`python scripts/compare_rust_editor_html.py rust/target/release/erida-api` runs real Python ASGI and Rust loopback on one disposable SQLite. It verifies Python write -> Rust no-op replay, Rust revision -> Python read, Rust restore -> matching immutable history, exact raw HTML, identical preview manifests, byte-identical TXT and HTML files with matching headers, and fail-closed errors. Unknown active markup must leave state unchanged. Stale accepted revisions and restore from an old source remain blocked.

The required `Native Rust UI real Chromium` job now runs that comparison before the actual browser. The existing browser scenario is extended rather than replaced: it accepts two multi-paragraph chapters with line breaks, quotes, ampersands and escaped angle brackets, reloads the persisted chapter with the UI button, verifies textarea text, then downloads TXT/HTML with real buttons and checks exact literary text, inert markup and SHA-256 receipts. Existing login, key issuance, drafts, review notes, cookie restoration, logout, no injected API-key and mobile checks are preserved. Safe evidence adds `05-multiline-editor.png` and marks `evidence.json` as M1l.

The old RUST-02e negative cases for an ampersand and punctuation were restrictions, not unsafe HTML. They are now positive cases compared with Python; negative coverage retains unknown entities, attributes and actual nested executable markup. No security test is skipped.

## Release boundary

This slice adds no API methods, schema changes or production writer activation. It does not implement import, QA, Gemini worker, broad HTML compatibility, real Nginx deployment, memory/DR or full production cutover. Rust writes remain behind the existing temporary SQLite double gate. Browser source books are prepared via Rust HTTP before the UI scenario; import through UI is not claimed.

Merge only after the exact PR head passes Rust parity, the expanded native Chromium job, Python 3.11/3.12, Web and the original Python Chromium job.

Source: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac). This document describes the candidate and acceptance contract, not an unobserved PASS.
