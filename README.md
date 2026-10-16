# Chaptera Team Translation Server (independent project)

Server-first, closed-team translation backend based on the existing Chaptera literary translation algorithms. **This repository is independent of `full_cycle_app`: it does not import, modify, or depend on the Windows application.** The original desktop translation engine is deliberately left untouched.

## What is implemented (v0.5 development)

- Standalone `chaptera_translation_core` package with source-unit segmentation, structural/provenance markers, deterministic completeness checks, request packing, source preparation and HTML finalization from the prior extraction.
- Async `TranslationCore` abstraction: transport-independent translation provider, glossary/context inputs, status events, cooperative cancellation.
- `chaptera_server` backend: authenticated REST API, SQLite-backed durable job queue, separate async worker, Gemini `generateContent` REST adapter, finite backoff for 429/5xx, `Retry-After`, event log, idempotency and lease ownership tokens.
- Every team member has a separate API token but uses one shared workspace/queue. There is no public signup.
- Translation is only marked completed after deterministic QA. Partial or missing segment responses are rejected.
- Chapter-scoped glossary bindings and version-map overrides (`scope` + `override`), separate required/session/advisory authority, terminology enforcement classes and source-only matching.
- Verified source-derived chapter context snapshots (schema v3/prompt v6 server policy); stale source/glossary/predecessor snapshots are **ignored with an event**, not silently trusted. Predecessor translation tails are capped and advisory only.
- Custom literary style is filtered to exclude machine/output contract instructions. Existing queued jobs without these new inputs remain readable.
- **v0.3 team library:** persistent books/chapters, optimistic source/glossary and accepted-translation revisions; accepted-translation history; automatic context generation and cache reuse before managed translations. Clients do not calculate context fingerprints. See [book memory contract](docs/BOOK_MEMORY.md).

- **v0.4 opt-in `atoms_v1`**: source-owned atom JSON transport, bounded packets,
  targeted repair, deterministic DOM reconstruction, and persisted hashed
  provenance. The existing `html_v0` jobs remain readable and continue to be
  the default. See [atom transport contract](docs/ATOM_TRANSPORT.md).

- **v0.5 complete-window context preflight:** bounded, ordered source windows for 50k+ visible-character chapters, deterministic fail-closed merging, conflict-safe terminology, source/glossary/predecessor fencing and fingerprinted source-window audit. See [windowed context](docs/WINDOWED_CONTEXT.md).
- **Opt-in, content-redacted evaluation:** offline simulated `html_v0` versus `atoms_v1` structural/cost-proxy report; explicitly gated, *not yet executed* billable Gemini pilot. See [evaluation checkpoint](docs/EVAL_REPORT.md).

**Status:** local backend prototype, not a deployed multi-user production server. The original Windows program is unchanged. Provider integration uses a new native Gemini adapter; it has **not** been exercised against the live Gemini API. A server-specific prompt policy now supports scoped glossary versions, enforcement classes, literary style guidance and fingerprint-verified chapter context snapshots, adapted from the desktop source. **The server now has atom transport and bounded targeted repair, but full
Windows PromptBuilder parity is not yet demonstrated.** Automatic context extraction is now available only for book-managed jobs; the standalone `POST /v1/jobs` remains backwards compatible. No browser UI, Rulate publishing or EPUB import yet.

## Install locally (Python 3.11+)

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
```

On Windows activate with `.venv\\Scripts\\activate` instead of the POSIX command. Configure environment variables (example `.env.example`):

```bash
export CHAPTERA_DB_PATH='./data/jobs.sqlite3'
export CHAPTERA_TEAM_TOKENS_JSON='{"admin":"generate-a-real-strong-random-token"}'
export CHAPTERA_GEMINI_API_KEY='your-google-api-key'
export CHAPTERA_GEMINI_MODEL='gemini-2.5-flash'
```

Do **not** commit real credentials. For now bind the API to `127.0.0.1` and test through local access or a secure tunnel. The HTTP header `X-API-Key` is required for every job endpoint; without configured team tokens, the API fails closed. The health check is public and returns no private data.

In separate terminals with the same environment:

```bash
chaptera-server serve
chaptera-server worker
```

For a translation job:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/jobs \\
  -H 'Content-Type: application/json' \\
  -H 'X-API-Key: YOUR_TEAM_TOKEN' \\
  -H 'Idempotency-Key: my-book-chapter-001' \\
  -d '{"chapter_id":"1", "source_html":"<body><p>世界正在燃烧。</p></body>", "target_language":"Russian"}'
```

Inspect the job with `GET /v1/jobs/{job_id}` or its events with `GET /v1/jobs/{job_id}/events`, using the same `X-API-Key`. List jobs with `GET /v1/jobs`. Cancel with `POST /v1/jobs/{job_id}/cancel`. A cancelled in-flight provider request may continue until its current response arrives; it will not save a translation as completed after the cancel flag is set.

## Book-managed translation (new in v0.3)

Create a shared book and store a source chapter with optimistic revision checks:

```bash
curl -X POST http://127.0.0.1:8000/v1/books -H 'X-API-Key: YOUR_TEAM_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"book_id":"demo","title":"Example"}'
curl -X PUT http://127.0.0.1:8000/v1/books/demo/chapters/1 \
  -H 'X-API-Key: YOUR_TEAM_TOKEN' -H 'Content-Type: application/json' \
  -d '{"chapter_index":1,"chapter_path":"chapters/1.xhtml","source_html":"<p>世界正在燃烧。</p>","expected_revision":0}'
curl -X POST http://127.0.0.1:8000/v1/books/demo/chapters/1/translate \
  -H 'X-API-Key: YOUR_TEAM_TOKEN' -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' -d '{}'
```

Worker performs asynchronous JSON context preflight (or complete bounded windows for long chapters) before the translation call,
then caches source-grounded context against source, glossary and predecessor
revision fingerprints. See [docs/BOOK_MEMORY.md](docs/BOOK_MEMORY.md) for
scoped glossary, acceptance and multi-editor conflict behavior.

## Architecture

```text
                   [ future web UI / Windows integration ]
                                   |
                                HTTPS API
                                   |
                    chaptera_server.api (team tokens)
                                   |
                  QueueStore (SQLite, durable for dev)
                                   |
                      chaptera_server.worker
                                   |
             chaptera_translation_core.TranslationCore
                     |                       |
       canonical Chaptera algorithms     GeminiProvider
        segmentation / completeness         (HTTPS)
```

The server does not embed or change the desktop app. The source-algorithm baseline comes from the earlier isolated `chaptera_translation_core` extraction; it is now part of **this independent repository**, so subsequent server changes cannot unexpectedly affect the Windows build.

## Safety and limits

- **Run one worker with SQLite.** Atomic claims and lease tokens are already implemented, but SQLite is a development backend, not the planned multi-host production queue. PostgreSQL is the next persistence milestone.
- Book/chapter library, persisted versioned glossary/context management (the job API currently accepts immutable client-side snapshots), proactive context extraction, proper roles/invites, audit trails, budgets/quotas, long-chapter chunking, editor and publication are not implemented yet.
- Do not directly expose the development server to the internet. Deploy behind HTTPS with restrictive ingress and rotate member tokens. Use an actual secret manager for Gemini keys in production.
- `idempotency_key` applies per member; requests with an existing key but different body return HTTP 409.
- Source chapters are stored in the local queue DB. Plan backups and suitable disk encryption for production.
- `GET /v1/jobs` shares jobs within the team by design; there is no per-book ACL in this first version.

## Development

```bash
pytest -q
python -m compileall -q src
python -m pip wheel --no-deps --no-build-isolation . -w dist
```

See `docs/PROMPT_POLICY.md` for versioned glossary and context snapshot examples, `docs/ROADMAP.md` for milestones, and `docs/PROVENANCE.md` for extraction boundaries.

## Protocol quality/cost evaluation (v0.5)

Run `chaptera-eval --output offline-report.json` for three synthetic, **offline**
transport fixtures. The generated JSON contains only structural metrics and
character counts; it does not contain chapters or request/response text. No
Gemini key, network access or charges are needed. Actual token counts are
reported only when a provider supplies `usageMetadata`. See
[docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) for the independent explicit opt-in
paid pilot, safeguards and no-go decision for changing defaults.
