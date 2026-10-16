# Atom JSON translation transport (server v0.4, opt-in)

The Windows `full_cycle_app` is **not modified**. The headless server now supports
an additional protocol, `atoms_v1`, alongside the original `html_v0` protocol.
The latter remains the default for existing clients and **all queued jobs whose
stored input has no `transport` key**.

## Request the new protocol

Use `"transport": "atoms_v1"` in a normal `POST /v1/jobs` JSON payload or in
`POST /v1/books/{book_id}/chapters/{chapter_id}/translate`. Other inputs and
output HTML remain unchanged. Results from completed atom jobs additionally
include `atom_provenance`, a hash-only audit per source atom.

The server builds immutable ordered source atoms with exact marker IDs and
source-owned render-domain IDs, serializes columnar JSON to the provider, then
accepts **only** explicitly declared `covers` associations:

```json
{"segments": [{"covers": ["000000"], "translation": "Мир горит."}]}
```

One segment must cover precisely one source-owned render domain (which may
contain several adjacent source atoms). The model supplies only text and IDs,
**never HTML**. HTML structure/attributes are reconstructed from the original
DOM by canonical Chaptera coverage/render logic. Unknown, duplicate and
non-contiguous IDs are rejected. Reordered complete model segments are accepted
only after reconciliation to original source order. HTML-looking/model-owned
markup is sanitized, and all declared source atoms must be accounted for.

## Long chapters and bounded repair

- Source-owned render domains are deterministically subdivided at <=24 atoms
  / <=7000 source characters; provider requests never split one *partition*.
- A missing or misaligned domain is re-requested, not the whole chapter.
- There are at most **two repair rounds**, with a fail-closed limit of **12
  damaged domains per round**. An incomplete chapter is never saved.
- Repaired domains use replacement, not append semantics. Already validated
  unaffected domains remain unchanged.
- Mixed Chinese/Japanese, 50k+ character chapters, escaped literal tags,
  truncated JSONL and out-of-order segments have simulated-provider tests.
- Reserved `TSEG_...`/`TSRC_...` marker-like content in the incoming source is
  currently rejected rather than silently stripped. It needs an independent
  escaping protocol before it can be supported safely.
- **Book-managed chapters over 50k visible characters** now use bounded source-complete windowed
  context preflight (up to 32 windows of 12,000 characters each), with an audited
  snapshot only after all responses pass. Out-of-bounds or incomplete coverage
  fails the job without saving a fabricated partial context. See
  `docs/WINDOWED_CONTEXT.md`.

## QA and provenance

The result is not committed until coverage, source-owned render domain layout,
render debts, reconstructed DOM text-slot structure and publication checks pass.
The persistence layer saves `atom_provenance` for completed atom jobs only:
source-unit ID, target-slot ID, target-fragment ID, DOM path, alignment/origin,
part index/count and full SHA-256 hashes of exact source/target text. **It does
not duplicate full source/target texts in the audit.** The job's translation HTML
is still stored as before.

Existing SQLite databases receive `jobs.provenance_json` via a small additive
migration on QueueStore startup. Old completed jobs expose `atom_provenance: []`;
old queued jobs and the `html_v0` idempotency-key semantics remain compatible.
No PostgreSQL or distributed worker migration is implied.

## Provider costs and diagnostics

Every `atom_batch` event contains the exact number of Python string characters
of `prompt` and serialized source `payload` (not a token estimate). The Gemini
adapter additionally reads official `usageMetadata` counters when present and
emits `gemini_usage` / `gemini_repair_usage` events with actual prompt, candidate
and total token counts. This data is **not available in mocked tests**; no live
Gemini call was performed. Full output-quality and token-overhead comparisons
against a real model require a later controlled, authorized trial.

## Known limitations

`atoms_v1` is opt-in during validation; it is not yet claimed to have complete
behavioral parity with the full Windows PromptBuilder. No semantic fact-checker
can prove that a linguistically plausible translation is truthful. Quality
checks reject demonstrable missing/reordered source units and structural
corruption, not every possible mistranslation. PostgreSQL, multi-worker delivery and the browser UI are later work. A real Gemini token/quality
comparison is still pending explicit authorization; see `docs/EVAL_REPORT.md`.
