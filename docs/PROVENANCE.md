# Source boundaries and provenance

This independent project was initialized using the Qt-free Chaptera Translation Core extraction, previously built from the owner's `full_cycle_app` source code.

Reused standalone algorithm modules (copied into `src/chaptera_translation_core/`):

- `core/translation_units.py`
- `core/translation_text_salvage.py`
- `core/worker_helpers/translation_completeness.py`
- `core/request_packing.py`
- `core/prepared_chapter.py`
- `utils/source_units.py`, `utils/helpers.py`, `utils/preparation_metrics.py`
- `api/errors.py`, `ports.py`, `service.py`

New independent server code lives only in `src/chaptera_server/`.

**No changes are made to `full_cycle_app`, and this project never imports `full_cycle_app` or PyQt6.** All future adjustments to prompts/providers/queues here are server-only. The previous desktop app maintains its existing production behavior.

Second stage (server-only) now adds:

- `chapter_context.py` (ported from desktop `utils/chapter_context.py`): desktop schema v3 / prompt v5, context snapshots, source/glossary/predecessor fingerprints, conflict detection, serialization, context prompt and model response validation. Desktop configuration lookup was replaced with a built-in default; the original desktop persistence/UI integration is intentionally not included.
- `semantic_terms.py`: pure source-evidenced observation normalizer from the desktop semantic preflight (no automatic promotion).
- `glossary_enforcement.py`: original enforcement inference rules, pure Python.
- `prompt_policy.py`: server-only composition for the existing HTML/TSEG protocol, adapting the desktop's literary-style filter, scoped glossary version `scope`/`override` semantics, required/session/advisory terminology hierarchy, and conflict-policy wording. It is *not* the original atoms-JSON `PromptBuilder` implementation.

Remaining fidelity work: real book/project glossary persistence, upstream context extraction/preflight scheduling, desktop source-atom JSON transport and targeted repair/reconciliation, live provider parity and representative side-by-side golden corpus tests. Desktop remains unchanged.
