# Roadmap: standalone team translation service

## Delivered in first iteration

- Repo independent of full_cycle_app; source algorithms transplanted as self-contained Python package.
- Native Gemini REST transport, no desktop runtime or PyQt dependencies.
- Team token authentication, durable SQLite worker queue, error classification/retry, cancellation and basic job log.
- Validation gate prevents incomplete translations being marked completed.
- Unit, API and simulated worker tests.

## Next development milestones

### M1. Fidelity/parity against desktop (priority)

Completed server-only slice: desktop chapter-context snapshot/validation policy (schema v3), glossary enforcement, exact chapter-scoped version overrides, required/session/advisory binding selection, filtered literary style preferences, context conflict warnings, and worker/API wiring. Local tests use deterministic fake providers; no live Gemini output is claimed.

- Build a golden fixture corpus: single HTML chapters, nonadjacent chapters, fragmented chapters, mixed scripts, glossary ambiguity and truncated model answers.
- **v0.3 delivered:** a persistent shared book/chapter library, optimistic glossary revisions and acceptance history, source-grounded automatic async context preflight, dependency-verified snapshots and predecessor-tail continuity. A single SQLite worker remains the development limit; desktop unchanged.
- **v0.4 delivered:** opt-in source-owned atom JSON transport, bounded repair,
  complete coverage/provenance gate, 50k+ chapter translation and additive SQLite
  audit migration. `html_v0` remains the default for old clients and jobs.
  **v0.5 delivered:** complete-window context extraction (prompt v6 with fingerprinted
  offsets, fail-closed aggregation and bounded glossary visibility), synthetic
  structural protocol comparisons and strictly opt-in live-evaluation CLI. No
  live Gemini experiment or human literary comparison yet. See
  `docs/WINDOWED_CONTEXT.md` and `docs/EVAL_REPORT.md`.
- Compare generated provider prompts, segmentation and final output against fixture expectations, not against a mutable live provider.
- Capture live provider token usage and model behavior only with explicit authorization; keep `atoms_v1` opt-in until the documented quality gate is passed.

### M2. Server-grade persistence and teamwork

- Move queue to PostgreSQL: `jobs`, `attempts`, `chapters`, `translations`, `revisions`, `glossary`, `events` with migrations.
- Introduce team invitations, roles (admin/editor/translator), token rotation and per-action authorization.
- Idempotent batch jobs, worker leases and claim tests with multiple processes.
- Backups, recovery tests and structured logs without payload/credentials.

### M3. Web interface and import/export

- **v0.6 delivered (Step 1/2):** frozen v0.5 API compatibility tests + `POST /v1/imports/preview`. Supports TXT, HTML, EPUB, DOCX and offline Rulate account metadata JSON catalogs; guarded parsers with no writes, explicit numbering, missing-prefix warnings and no fabricated text. Details in `docs/CORE_API_CONTRACT.md`.
- **Next — Step 3:** persistent staged imports, revision-aware confirmation of selected chapters, duplicate/conflict decisions, title/number/path overrides and atomic SQLite storage. Never automatically overwrite existing sources/accepted translations.
- **Then — Steps 4-10:** closed-team browser UI, book index and chapter list, translation submission and progress, human editor and export, end-to-end user acceptance tests.

- Books, chapter TOC, EPUB/HTML/TXT upload, chapter selection, per-chapter progress.
- Review/edit UI with optimistic concurrency and revision comparisons.
- Publication readiness gates and safe export to desktop/Rulate publishing workflow.

### M4. Deployment

- Harden worker/API containers, TLS reverse proxy, migrations and health checks.
- Deploy after server details and credentials are provided.
- Keep translation service separate from existing Windows application and other Chaptera projects.
