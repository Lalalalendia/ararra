# Windowed context preflight (server 0.5.0)

This implementation is **server-only**. The existing Windows application remains unchanged.

## Source completeness contract

- The source is read as visible HTML text with BeautifulSoup, using the same normalization for window planning and auditing.
- Visible source is partitioned into **contiguous, non-overlapping** windows of up to **12,000 Unicode characters**, preferring boundaries near sentences. The concatenated windows must exactly equal the original visible text. A chapter may require up to **32** windows; larger chapters fail before any context-model request. These are character bounds, **not** model token counts.
- Each window receives its own source-only `c0` request, with exact index and offsets. A future window is **never** supplied to an earlier extraction call. Predecessor anchor is supplied only to the first window.
- Canonical glossary terms are matched **against the current window**, not the whole book/chapter, preventing later-only names from leaking into previous windows.
- Every response must be a complete, bounded JSON object with the explicit `c0` key and all context fields. Entity source names/aliases and semantic source terms are accepted only when supported by the current source window (or the immediate predecessor as a permitted entity-name anchor in the first window).
- The server merges **only after all windows pass**. Summaries and point-of-view/tone observations keep ordered window labels; list items are de-duplicated without synthesizing new plot facts. Contradictory *suggested* translations for one term are **excluded** rather than promoted into a canonical glossary.
- The aggregate context JSON is limited to **24,000 characters**. Exceeding this limit fails closed rather than silently dropping late windows.

## Failure and persistence

Malformed/missing provider responses cause a bounded job retry; **no partial context is saved**. Impossible sizes/aggregation limits cause an immediate `context_unserviceable` + failed job **before translation**, rather than claiming a partial context is complete. Partial Gemini costs may already have been incurred before a later window fails; retries re-run all windows because partial observations are not trusted/persisted.

Successful snapshots contain `window_coverage` with an ordered list of `index`, source character offsets and SHA-256 of each window. `window_coverage` is included in the snapshot fingerprint, but **not** sent to the model in translation prompts. Source/glossary/predecessor revision fences remain the same; book edits before snapshot save or before final translation persistence fail the job.

**Context prompt version is increased from 5 to 6.** Old cached snapshots are not deleted, but are intentionally considered stale and rebuilt on demand. No SQLite table migration is required for this change. A legacy `html_v0` translation job stays on its original translation protocol; the preflight cache policy is updated for new worker executions.

## Boundaries and limitations

- Source-complete **analysis input** and deterministic ID/alias gates are verifiable. Model statements about plot facts, relationships and character motives are **not automatically semantically verified**. A linguistically plausible hallucination remains possible.
- Prompts for long chapters incur additional requests (one per window), before translation; actual model token usage is logged only when `usageMetadata` is available. Windowing is **not** a cost reduction claim.
- Even valid 24,000-character context can be repeated across atom-batch prompts. Further token-oriented context compaction must be designed and benchmarked against real authorized model outputs.
- `32 * 12,000` is an upper bound, not a guaranteed accepted size: natural sentence boundaries can cause more than 32 windows and trigger an explicit failure.
- A single-node SQLite worker remains the supported development deployment. PostgreSQL/multiple workers are independent milestones.
