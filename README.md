# Chaptera Team Translation Server (independent project)

Server-first, closed-team translation backend based on the existing Chaptera literary translation algorithms. **This repository is independent of `full_cycle_app`: it does not import, modify, or depend on the Windows application.** The original desktop translation engine is deliberately left untouched.

## What is implemented (v0.6 development)

- Standalone `chaptera_translation_core` package with source-unit segmentation, structural/provenance markers, deterministic completeness checks, request packing, source preparation and HTML finalization from the prior extraction.
- Async `TranslationCore` abstraction: transport-independent translation provider, glossary/context inputs, status events, cooperative cancellation.
- `chaptera_server` backend: authenticated REST API, SQLite-backed durable job queue, separate async worker, Gemini `generateContent` REST adapter, finite backoff for 429/5xx, `Retry-After`, event log, idempotency and lease ownership tokens.
- **ReadEra team roles:** exactly two GLOBAL roles: one explicitly configured administrator and all other authorized members as moderators. Both work in the same book library and translation queue; there are no per-book owner/editor/reviewer/viewer roles. Team tokens remain separate. No public signup.
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
- **v0.6 read-only book import preview and opt-in RE-02 import stages:** authenticated `POST /v1/imports/preview` accepts TXT, HTML, EPUB, DOCX and the existing Windows Rulate-account metadata JSON. Detects chapter titles/numbers and exposes bounded samples, archive safety warnings and unknown/pre-chapter text. Read-only preview does **not** commit source chapters; a separate authenticated stage-and-confirm API now saves selected full source chapters atomically, never during preview. See [frozen API contract and import limits](docs/CORE_API_CONTRACT.md).

**Status:** server core has passed a real VPS live Gemini smoke (`gemini-3.8-flash`); permanent multi-user production deployment remains a separate acceptance gate. The original Windows program is unchanged. Provider integration uses a new native Gemini adapter; a synthetic live worker request has succeeded on the VPS. A server-specific prompt policy now supports scoped glossary versions, enforcement classes, literary style guidance and fingerprint-verified chapter context snapshots, adapted from the desktop source. **The server now has atom transport and bounded targeted repair, but full
Windows PromptBuilder parity is not yet demonstrated.** Automatic context extraction is now available only for book-managed jobs; the standalone `POST /v1/jobs` remains backwards compatible. No full browser UI or Rulate publishing yet. EPUB/TXT/HTML/DOCX source chapters can be staged and explicitly confirmed, but Rulate metadata JSON remains preview-only. The new import endpoint provides preview only, not canonical book creation.

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
# Must name an EXISTING member ID, not a secret; no implicit admin by default.
export CHAPTERA_ADMIN_MEMBER_ID='admin'
export CHAPTERA_GEMINI_API_KEY='your-google-api-key'
export CHAPTERA_GEMINI_MODEL='gemini-3.8-flash'
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

## ReadEra Jinja2 browser UI (books_platform migration)

The preferred server-delivered team UI is available at `/app` (Overview,
Books, Import, Jobs, Team). FastAPI renders Jinja2 templates that use
vanilla JavaScript and directly reused `books_platform` chapter-import
CSS and adapted job-status JavaScript. **React is not required at runtime**
for this route, and no npm build is needed to serve it.

The existing `/v1` API and its X-API-Key security model stay unchanged.
Team keys remain only in browser memory on the page, never in browser
storage or URLs. Use TLS and a trusted network boundary in deployment.
The old React/Vite frontend is retained as a temporary comparison and
rollback option until real-browser functional and Canva visual checks pass.
Source mapping, test evidence, and remaining acceptance gates:
[docs/READ_READERA_JINJA_MIGRATION.md](docs/READ_READERA_JINJA_MIGRATION.md).

## Two global roles for the ReadEra team

**Administrator** (single explicit `CHAPTERA_ADMIN_MEMBER_ID`, matching a key in
`CHAPTERA_TEAM_TOKENS_JSON`) can inspect the team roster via
`GET /v1/admin/members` (member IDs and role labels only; never API keys).
**Moderator** is every other authenticated team member and receives HTTP 403
for admin-only endpoints. `GET /v1/me` reports the caller's member ID
and role. `GET /v1/admin/members` lists active and disabled members without
revealing bearer tokens. `POST /v1/admin/members` issues a new random
moderator token once; `POST /v1/admin/members/{member_id}/rotate` replaces a
token, while `POST /v1/admin/members/{member_id}/disable` revokes it and
cancels pending jobs. These APIs are available through the `/team` React
admin screen; an administrator pastes their protected key into memory only,
not browser storage or server logs. Without a configured administrator **nobody is implicitly promoted**:
all members are moderators, including the first member. An unknown admin ID
fails startup rather than falling back to arbitrary credentials.

The book library, import sessions, glossary, chapter edits, accepted versions,
and translation queue remain a **shared technical workspace** for both roles.
Book access is still shared, but each newly created book records **one
accountable moderator owner** and an independent owner revision. This is not
a third role and does not restrict editors from other books. New books created
by a moderator default to that moderator as owner; an administrator must
explicitly select a moderator owner. Legacy books stay ownerless until an
administrator assigns a moderator. The administrator can transfer ownership
through `PUT /v1/books/{book_id}/owner` using optimistic owner revision checks;
`GET /v1/books` lists the shared catalogue with ownership metadata.
Admin/member credentials are separate from this responsibility field.
The administrator's own key and identity are managed by trusted host
configuration, while moderator key issuance/rotation/disable use the
authenticated admin API described above.
The administrator can manage legacy configured credentials through that
operational process; new moderator credentials are stored as one-way SHA-256
digests in the same durable SQLite database and are never recoverable via
the API. Existing static moderator secrets are immediately invalidated when
rotated or disabled through the admin API. The administrator credential remains
host/operator-managed and cannot be replaced via HTTP.
Moderator invitation delivery is manual via a secure channel, not email.
Before disabling a moderator who owns books, reassign ALL their books explicitly
to an active moderator; API returns 409 until the owner ledger is clear.
No RBAC or admin UI rollout occurs on the VPS until an explicitly reviewed deploy.

## Import-file preview (new in v0.6)

The source import starts with a safe inspection request. It does **not** modify the library or create translation jobs:

```bash
curl -X POST http://127.0.0.1:8000/v1/imports/preview \
  -H 'X-API-Key: YOUR_TEAM_TOKEN' \
  -F 'file=@novel.epub'
```

The response shows the title, order, detected chapter numbers, bounded text samples, metadata and any warnings about unmapped preface or duplicate numbers. DOCX, TXT, HTML and EPUB produce a `book` preview. A JSON exported by our Windows Rulate account metadata tool produces a `metadata_catalog` of books and links **without chapter texts**. To store original chapter texts, call the separate authenticated stage-and-confirm API described in `docs/CORE_API_CONTRACT.md`. A preview response is never a completed import.

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

## Enforced module boundaries

Python transport is composed in `chaptera_server/api.py` from separately owned
`http/routers/{books,jobs,imports,health}.py`. Request DTOs live in
`http/schemas.py`, HTTP error mapping/dependencies in `http/dependencies.py`;
the library, queue, worker and `chaptera_translation_core` do not import web
frameworks. The `auth.py` token validator raises a transport-neutral exception;
the HTTP adapter converts it to 401. Legacy endpoint paths/status codes are
covered by regression tests.

The **independent browser app** is in top-level [`web/`](web/), not inside the
Python wheel. Browser routes are owned by `web/src/routes/`, features under
`web/src/features/`, and outgoing HTTP by `web/src/shared/api/`. The web shell
currently provides navigation and public health status only; team sign-in and
private book pages are intentionally not implemented yet.

Architecture contracts run on every GitHub PR and push: `python
tools/check_architecture.py`, `pytest tests/test_architecture.py` and `cd web
&& npm run test:architecture`. GitHub Actions also builds the TypeScript/React
app. See [architecture decisions and extension rules](docs/ARCHITECTURE.md).

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


### Gemini smoke test on the worker host

Run `chaptera-smoke` for an offline REST/worker/SQLite test; real Gemini access requires an explicit on-host opt-in and a securely configured API key. A passing CI test alone does **not** prove server-to-Gemini connectivity.

On the host running the worker, use `CHAPTERA_SMOKE_ALLOW_LIVE=YES chaptera-smoke --live --authorize-billable --transport html_v0` after configuring `CHAPTERA_GEMINI_API_KEY` in the environment (never in command arguments or Git). The smoke test uses only a synthetic Chinese phrase, a temporary SQLite DB and at most one provider request by default. For `docker compose -f compose.dev.yml`, run `docker compose -f compose.dev.yml exec -e CHAPTERA_SMOKE_ALLOW_LIVE=YES worker chaptera-smoke --live --authorize-billable --transport html_v0`. The report never includes the key, prompt or translated text. To test the atom protocol separately, pass `--transport atoms_v1`.

### Server deployment and systemd credentials

The verified default Gemini model is `gemini-3.8-flash`; override it explicitly
using `CHAPTERA_GEMINI_MODEL` only after testing a candidate on your own key.
Under systemd, `Settings.from_env()` reads `gemini_api_key` and
`team_tokens_json` through `CREDENTIALS_DIRECTORY` when their files exist, and
rejects conflicting environment secrets. The API must have a valid member token
credential and the worker must have a Gemini credential. Never include these
values in unit files, shell arguments, GitHub commits, or diagnostic logs.

See `chaptera-site/deploy/translator/` for separate rootless release delivery,
root-required one-time service provisioning, read-only localhost checks, and
explicitly authorized paid smoke tests. Do not expose the closed-team API to a
public host until browser authentication and access-control work is complete.
