# Server prompt policy: chapter-scoped glossary and context (M1 slice)

This is a **server-only** implementation; the Windows app is untouched. The
input/output is still the TSEG/TSRC HTML protocol. This milestone does **not**
claim feature parity with the desktop atom-based PromptBuilder or automatic
context generation.

## Job payload extensions

`POST /v1/jobs` accepts all previous fields unchanged, plus:

```json
{
  "chapter_id": "517",
  "chapter_path": "OEBPS/Text/chapter_0517.xhtml",
  "source_html": "<body><p>李明进入城市。</p></body>",
  "glossary_authority": "READY",
  "glossary": {
    "李明": {"rus": "Ли Мин", "note": "персонаж, мужчина", "authority": "required"}
  },
  "glossary_versions": {
    "李明": [
      {"scope": ["OEBPS/Text/chapter_0517.xhtml"], "override": {"rus": "Ли Минь"}}
    ]
  },
  "style_instructions": "Сохраняй сдержанный повествовательный тон."
}
```

The optional `chapter_context_snapshot` uses the existing Chaptera snapshot
format (schema 3/prompt 5). The optional `predecessor` object may contain:

```json
{
  "chapter_path": "OEBPS/Text/chapter_0516.xhtml",
  "source_hash": "...",
  "translation_tail_hash": "...",
  "translation_tail": "..."
}
```

Use `chaptera_translation_core.make_snapshot(...)` to create a valid snapshot
with `chapter_path`, SHA-256 of the **normalized, boilerplate-stripped source**,
`glossary_fingerprint(scoped_glossary(...))`, compact model context and the exact
predecessor dependency. The server checks the fingerprint, schema and all three
dependencies before adding snapshot text to a provider prompt. If it is stale,
the job still translates but emits a `context_stale` event and **does not send
that snapshot** to Gemini.

## Authority and safeguards

- When glossary authority is not `READY` or `READY_WITH_WARNINGS`, the glossary
  is not treated as canonical. This is a fail-closed boundary for unavailable
  glossary data.
- Exact chapter paths select version-map overrides. Two different overrides for
  the same term and chapter raise an error instead of producing an ambiguous
  glossary. Version scope changes are included in the idempotency hash.
- Only source terms occurring in **visible source text** are sent to Gemini:
  no matches from image URLs, HTML tag attributes or absent terms.
- Terms are grouped as `REQUIRED_BINDINGS`, `SESSION_BINDINGS`,
  `ADVISORY_BINDINGS`. Enforcement is `strict`, `lexical_family` or `flexible`,
  using the desktop rules. Advisory terms never override canonical project
  bindings or the source.
- Explicit entity gender contradictions between glossary notes and chapter
  context appear as `UNRESOLVED GLOSSARY / CHAPTER-CONTEXT CONFLICTS`; the
  model must defer to chapter-local source evidence instead of modifying the
  project's glossary.
- The predecessor's accepted-translation tail is capped at 2400 characters,
  used only for style/wording continuity, and cannot override source facts.
- Custom `style_instructions` are filtered: format/JSON/TSEG/HTML instructions
  are excluded. They cannot override the machine-owned completeness contract.
- A queued job stores its immutable glossary/context snapshot in SQLite so
  retries do not silently change instructions between attempts.

## Limits and next steps

The REST API currently accepts caller-provided snapshots: there is **no server
endpoint that generates chapter memory automatically**. Permanent book-level
glossary editing, snapshot invalidation, automatic context prepass, and
per-revision dependency tracking need persistent project models. The desktop
atom-based JSON `PromptBuilder`, richer repair passes, Gemini parity and web UI
are separate milestones. A `completed` status still requires deterministic
source unit and structure validation; prompt improvements do not bypass QC.
