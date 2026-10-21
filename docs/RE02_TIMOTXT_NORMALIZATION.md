# RE-02 — Evidence-bound Timotxt normalization (FullCycle parity)

This **pure experimental module** ports FullCycle R15.1.27's glossary
parallel-evidence approach. It does not alter the source worker, book
library, production process, or paid translation pipeline.

## Evidence contract

- Paired terms must have identical codepoint length.
- All readable positions must match exactly. Only one Hangul syllable to
  one CJK character substitutions are candidates.
- Primary proof: same canonical Russian glossary target and at least one
  unchanged CJK anchor, or two independent exact-position pairs.
- Secondary proof: at least two unchanged CJK anchors and a unique clean
  source spelling from the corpus.
- Contradictory mappings are excluded globally; no guesses or model calls.
- Deterministic SHA-256 mapping fingerprint and per-glyph proof classes.
- Derivation returns both `raw_text` and `normalized_text`, separate SHA,
  mapping_used, unresolved glyph count, and an explicit `candidate_only`.
- Any remaining Hangul syllable/Jamo/PUA remains byte-identical and sets
  `needs_review`. Even `normalized_candidate` does not authorize importing
  a book or sending an unreviewed translation request.

## Integration boundary

The existing `SourceJobStore.finish` only supports verified `ok` bodies
or failures. Damaged RAW is currently rejected by the Timotxt adapter.
There is no **raw-but-unverified** durable state or separate derived source
field. Passing a normalized candidate into the existing `body` would
silently lose the original bytes, violating ReadEra source fidelity.

Before activating normalization, another PR must add durable raw SHA/text,
a separate derived normalization layer and mapping fingerprint, reviewer
approval and stale invalidation on raw/mapping changes. Preserve all source
worker retry and CAS semantics. Do not automatically translate unresolved
glyphs, and do not alter glossary keys during a source fetch.

**No mapping corpus bundled.** FullCycle's historical 50 mappings and
31.05% occurrence coverage came from its old projects.sqlite3 snapshot.
ReadEra has not imported or independently verified that snapshot.

References: [FullCycle R15.1.27](https://app.notion.com/p/3d232a84beec815097a0ef4f7ad123f2);
user Library `full_cycle_app__part_004_of_010(1).md`
(normalization implementation) and
`full_cycle_app__part_005_of_010.md` (offline tests).

Exact-head CI and offline unit tests required before merging this pure
foundation; no VPS, site access, book import or Gemini request is performed.
