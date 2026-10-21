# RE-02 — Timotxt quarantined originals and derived glyph candidates

Status: **new code pending exact-head CI**. No VPS rollout or live crawl.

## Purpose

A Timotxt chapter can be structurally complete but contain Hangul/Jamo or
private-use glyph substitutions. The existing worker rejected it, and the
only durable `source_collect_items` states were `ok` and `failed`.
That meant the unverified original disappeared after a run. Worse, writing
the FullCycle-normalized candidate into `ok.body` would make a derived,
possibly partial reconstruction look like publisher RAW.

This implementation keeps the established **complete** gate intact while
retaining bounded operator-readable RAW for investigation.

## Durable flow

1. The source worker fetches every **observed** page of a Timotxt chapter
   using the existing pinned HTTPS/robots/rate-limited transport. It first
   runs every structure, blocker, pagination, duplicate-part and size check.
2. With suspicious glyphs, and only for Timotxt, the worker passes the
   extracted unmodified UTF-8 chapter text to the review writer.
3. A valid `source_collect_jobs` lease, the matching frozen manifest index,
   a `BEGIN IMMEDIATE` transaction and strict byte budgets protect the write.
   The immutable `source_collect_review_raw` row stores source identity,
   extracted raw text, SHA and size. The **same transaction** records
   `source_collect_items.status='failed'` and releases the lease.
4. Normal `source_collect_items.ok`, source archive, Stage/Confirm, TXT
   assembly, Gemini and book library **never** read the review table.
   `review_snapshots` is informational; it is not a completion count.
5. Retry remains possible, but earlier review snapshots are retained until
   the operator deletes the terminal job. Deletion cascades raw and derived
   review rows without touching an existing verified shared archive.
6. If bounded quarantine storage is exhausted, the chapter is still marked
   failed with an explicit **RAW not saved** error. Quota failure never
   becomes `complete`, and never retries endlessly automatically.

The payload is **extracted source text**, not the HTTP response HTML. The
original source URL and catalog/chapter identity remain in every receipt.

### Review-only budgets (independent from verified source quota)

- 1 MiB maximum per raw snapshot.
- 16 MiB maximum total quarantined raw per job.
- 64 MiB per actor; 256 MiB global.
- Maximum three **derived** variants per raw snapshot and 128 MiB total
  derived candidate bytes globally.
- Maximum 200 short glossary rows in a submitted evidence corpus.

The worst-case total additional logical storage is bounded but must still
be included in VPS disk/WAL/backup capacity planning. No worker has been
enabled by this change.

## Actor-only HTTP workflow

Authenticated team owner, no caching:

- `GET /v1/source-books/jobs/{job_id}/reviews`: metadata, raw SHA and
  receipt count only; **no prose** in the list.
- `GET /v1/source-books/jobs/{job_id}/reviews/{snapshot_id}`: original
  extracted RAW and, if present, separate derived variants with SHA and
  mapping fingerprint. Cross-member access returns 404.
- `POST /v1/source-books/jobs/{job_id}/reviews/{snapshot_id}/candidates`:
  requires exact `expected_raw_sha256` and 2..200 structured glossary
  terms (`project_id`, `original`, `rus`). A pure FullCycle R15.1.27
  learner derives evidence-bound Hangul-to-CJK mappings. The database
  stores candidate and immutable provenance **separately**; the response
  returns receipt metadata only.

The glossary corpus for this endpoint is **submitted evidence**, not
automatically verified against a live team glossary database. It cannot
approve the candidate, even if all Hangul is resolved; the output always
has `candidate_only=true` and `exportable=false`. A future release
must bind evidence to trusted glossary revisions and add reviewer approval,
stale invalidation and explicit, source-preserving handback before it can
enter the translation pipeline.

## Fail-closed cases

- Missing/stale/expired lease, cancelled job or foreign team member.
- Unobserved URL, incomplete multi-part page, missing narrative node,
  CAPTCHA/login/429, dangerous target, repeated part body.
- Corrupt SQLite original SHA, byte count, manifest identity or derived SHA.
- Changed expected RAW digest; no mapping evidence; conflicting glyph proof.
- Quota exhaustion (explicit failed receipt; no silent success).
- Normal one-shot `collect_selected` with obfuscated body (still rejected).

Tests: `tests/test_source_timotxt_quarantine.py` plus the old source
worker/API/TXT/backup tests. Fully synthetic, no actual book prose.

## Release boundary

Feature is developer-preview only. Live Timotxt permission, current
`robots.txt`, permissible chapter text, VPS access, RAM >=512 MiB, free
physical disk and independently proved offsite restore remain **unmet**
per [release gate #65](https://github.com/Lalalalendia/ararra/issues/65).
No production activation, paid Gemini call or bulk scraping is implied.

Evidence spec:
[Notion RE-02-MED-EXPAND](https://app.notion.com/p/3fe32a84beec813cb650cab7f4dfe615),
[FullCycle R15.1.27](https://app.notion.com/p/3d232a84beec815097a0ef4f7ad123f2).
