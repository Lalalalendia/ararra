# RE-02-TOC-EDITOR-01: two-stage chapter collection

Issue: https://github.com/Lalalalendia/ararra/issues/96

## Workflow

1. Authorized discovery: POST /v1/source-books/catalogs reads **TOC only**
   via existing bounded HTTPS fetcher and saves an actor-owned, expiring catalog.
2. Browser editor: inspect all ≤2000 catalog entries (80 per page), checkbox
   a sparse subset, reorder via ↑/↓ or drag, filter by title/source ID without
   dropping hidden selections, and explicitly review extra/service entries
   when adapter metadata supplies their types.
3. Save: PUT /v1/source-books/catalogs/{catalog_id}/selection with exact
   ordered_ordinals, expected_catalog_sha256 and optional CAS
   expected_selection_sha256. The server accepts only ordinals belonging
   to the immutable actor-owned catalog; SHA256 covers version, catalog
   fingerprint and exact **ordered** selection. The 30-minute catalog expiry
   also bounds the plan. GET /v1/source-books/selections/recent can restore
   the last live plan after refresh without browser-side credential storage.
4. Confirm: POST /v1/source-books/jobs using selection_id and
   expected_selection_sha256. A normal range request continues to work.
   The accepted selection becomes a **durable, immutable ordered manifest**
   protected by the existing manifest_sha256, quotas, actor fences and worker
   checkpoints. Deselected entries are not enqueued and not counted missing.
5. Materialize: POST /v1/source-books/jobs/{job_id}/stage with
   start_position/end_position (1-based position in the selection; ≤20 entries)
   keeps ordering and provides destination_position hints in the existing
   ImportSessions durable preview; source ordinal/source ID/provenance remains
   unchanged. Actual BookLibrary writes still need explicit Stage/Confirm.
   Legacy start_ordinal/end_ordinal still works for old range jobs.
6. Export: the existing verified-source TXT digest/checkpoints are scanned in
   immutable manifest order; scope is `selected_catalog_selection_only` for
   ordered selections and unchanged `selected_catalog_range_only` for legacy
   jobs. Neither proves whole-site corpus completeness.

## Fail-closed gates

No arbitrary client URL, cross-book link, mixed range/plan parameters,
stale catalog, changed plan SHA, duplicate ordinal or empty selection can
produce a job. Side-by-side browser tabs use CAS rather than last-writer-wins.
A worker restart may resume only unsaved chapters, without re-fetching
completed bodies. Same selection with a different permutation is a different
SHA and a different idempotency key.

No live source-worker enablement or deployment follows from this PR. Legal
site/robots/TLS gates (#65) and source-worker RAM/backup acceptance (#83)
remain unchanged. No Gemini, Google Drive or translation side effects.
