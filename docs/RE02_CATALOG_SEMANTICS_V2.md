# RE-02-CATALOG-TYPES-03a — semantic TOC fingerprint v2 (offline only)

Parent: [#99](https://github.com/Lalalalendia/ararra/issues/99). Related:
[#96](https://github.com/Lalalalendia/ararra/issues/96),
[#98](https://github.com/Lalalalendia/ararra/issues/98).

## Why it is separate

The existing v1 catalog SHA, selection SHA, SQLite snapshots and job manifests
are established integrity contracts. Changing their meaning in place could
invalidate old catalogs and in-flight jobs or alter resume/idempotency behavior.
This PR adds **only a pure, opt-in Python module and tests**. It does not
change SourceCatalogStore, SourceSelectionStore, SourceJobStore, adapters,
HTTP routes, ImportSessions, the source worker, or the VPS.

The ordinary source flow still uses v1. It must not be advertised as safe live
extra/service classification merely because this optional model exists.

## Semantic entry

A CatalogSemanticEntry contains:
- source_ordinal: 1-based order in the site TOC, not the author chapter number
- source_id and URL: source-owned immutable identity and canonical chapter URL
- title: displayed source heading
- author_number: optional reliable author label, never inferred from position
- entry_type: unknown=0, chapter=1, extra=2, service=3
- classification_source: none=0, structured=1, title_pattern=2
- classification_confidence: unverified=0, high=1, medium=2

Allowed evidence combinations:
- unknown / none / unverified
- chapter, extra or service / structured / high
- chapter, extra or service / title_pattern / medium

Unknown is the safe default, never silently treated as a chapter. Repeated
author numbers are accepted; source ordinals and NFC-normalized source IDs
must be unique. Different classification meanings require another version.

## Canonical binary encoding

All integers are unsigned big-endian. Text is NFC-normalized UTF-8 with
a u32 big-endian **byte** length (not character count). Internal tabs/newlines
are preserved and not used as separators.

~~~text
ASCII("RE02-CATALOG-SEMANTICS") + 0x00 + ASCII("v2") + 0x00
text(source adapter ID)
text(canonical source-owned book ID)
u32(number of entries)
repeat in strictly increasing source_ordinal:
    u64(source_ordinal)
    text(source_id)
    text(canonical URL)
    text(title)
    u8(author_number_present) [u64(author_number) iff present]
    u8(entry_type)
    u8(classification_source)
    u8(classification_confidence)
~~~

The source ID is a lowercase [a-z][a-z0-9_-]* slug, supplied by the adapter.
The book ID must be derived from the existing validated source_book_id()
logic, never from arbitrary client input. The digest does **not** replace
site allowlists, same-book URL checks, robots policies, or TLS validation.

Limits: 1–2000 entries, ordinals/author numbers 1–10,000,000, adapter ID at
most 64 UTF-8 bytes, book ID and chapter source ID at most 512 bytes each,
URL/title at most 4096 bytes each, and final canonical bytes at most 2 MiB.
Malformed evidence, duplicate identities, invalid Unicode and oversized
inputs fail closed.

## Golden digests and compatibility

SHA-256 of the two-entry fixture in the test suite (semantic v2):

    5eed13e0e77429aed5f2af2806353cc7237b8d61da6cbc579767780f5aac8545

Pinned existing v1 identity digest for those same source IDs/URLs:

    eee2d4cb0cad5ca410cc25b87090a745ee6e1ad99ebf87678801224b33f46e6b

Changing only a title, type, author number, or classification evidence changes
v2, but leaves v1 untouched. This PR deliberately does not wire the v2 hash
into existing CAS, catalog IDs, manifests, or checkpoints.

## Future work — not in this PR

1. Add version-tagged semantic digest columns to new actor-owned catalog
   snapshots with additive SQLite migration; retain legacy v1 snapshots.
2. Introduce explicit ordered-v2 CAS and manifest semantics, binding both
   legacy identity SHA and semantic v2 SHA. Preserve legacy range/ordered-v1
   job verification and recovery; never reinterpret a stored legacy digest.
3. Implement conservative site-specific parsing (Royal Road prologues,
   interludes and announcements; Chinese story, extra and service headings).
   Unknown/extra/service need explicit review before queueing.
4. Preserve semantic provenance through worker checkpoints, Stage, checked
   exports, and manual ImportSessions confirmation. Do not recast unknown
   records as story chapters by synthesizing a misleading heading.
5. Test migration, restart, conflicts, last/first 2000 rows, Node and
   Chromium; live rights/robots/TLS and VPS worker acceptance remain separate
   gates (#65 and #83).

This is not a production source-classification release.
