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

Parity status: original `PromptBuilder`, entire chapter-context memory subsystem, rich glossary engine and original Gemini provider were not directly copied because of UI/runtime dependencies; these require dedicated independent implementation and tests.
