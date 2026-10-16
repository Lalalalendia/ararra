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
  Expand real-model golden fixtures and implement batched/long-chapter context
  extraction; see `docs/ATOM_TRANSPORT.md`.
- Compare generated provider prompts, segmentation and final output against fixture expectations, not against a mutable live provider.
- Instrument token usage, costs and model behavior.

### M2. Server-grade persistence and teamwork

- Move queue to PostgreSQL: `jobs`, `attempts`, `chapters`, `translations`, `revisions`, `glossary`, `events` with migrations.
- Introduce team invitations, roles (admin/editor/translator), token rotation and per-action authorization.
- Idempotent batch jobs, worker leases and claim tests with multiple processes.
- Backups, recovery tests and structured logs without payload/credentials.

### M3. Web interface and import/export

- Books, chapter TOC, EPUB/HTML/TXT upload, chapter selection, per-chapter progress.
- Review/edit UI with optimistic concurrency and revision comparisons.
- Publication readiness gates and safe export to desktop/Rulate publishing workflow.

### M4. Deployment

- Harden worker/API containers, TLS reverse proxy, migrations and health checks.
- Deploy after server details and credentials are provided.
- Keep translation service separate from existing Windows application and other Chaptera projects.
