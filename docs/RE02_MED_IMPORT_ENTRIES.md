# RE-02-MED-01: structurally typed book-entry preview (offline)

Source: \`Lalalalendia/med/tools/novel-ingest/extract_chapter.py\` at MED
\`5046ff53207ce2fc89fb83277d9ceec1fee30935\`.

The source import preview was previously one *candidate = one chapter*. For Chinese
web-novel TXT dumps this silently conflated numbered story chapters with
\`番外\` extras and \`上架感言\` / \`请假条\` service entries. This additive
implementation classifies only anchored, conservative known headings in
TXT/DOCX and structural HTML/EPUB header tags; arbitrary prose is not parsed
as a service heading.

## Contract

- \`chapter_count\` remains the **total candidate count** for compatibility.
  New \`story_chapter_count\`, \`extra_entry_count\`, \`service_entry_count\`
  distinguish candidate types; \`entry_type=chapter|extra|service\` is per row.
- \`catalog_ordinal\` is the actual 1-based entry order (including extras).
  \`author_number\` / \`detected_number\` is the numbered story label if
  present, or null. Never infer a missing author number from entry order.
- \`numbering_gaps\` contains internal, inclusive ranges only. A book beginning
  at chapter 517 does *not* imply 516 missing chapters; very large numbers never
  allocate an unbounded missing-number array.
- \`duplicate_body_pairs\` is a bounded (32 pairs) preview-only SHA-256 signal.
  Gaps or duplicate bodies remain **warnings**, not claims of complete source
  coverage or automatic source edits.
- In the browser each \`extra\` / \`service\` row begins as \`review\`.
  It cannot pass confirmation until the user explicitly selects
  \`create\`, \`replace\` or \`skip\`. Unnumbered destinations must be entered
  manually. Confirmed auxiliary headings are included in the stored HTML.
- All existing stage CAS, one-moderator book ownership, idempotency, source
  revision and atomic commit rules remain unchanged. Stage does not call
  Gemini, source-worker, Google Drive or remote sites.

This is **not** the next live-site adapter: Narou and multipart HTTP chapters
remain separate, gated by rights/robots/TLS and the release requirements
in \`docs/RE02_WEB_SOURCE_MED_INTEGRATION.md\` and ararra issues #65, #83, #85, #94.

Offline tests: \`tests/test_med_entry_import.py\`,
\`web_ui/static/js/import-model.test.mjs\`.
