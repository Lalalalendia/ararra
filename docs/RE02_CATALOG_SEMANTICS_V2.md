# RE-02-CATALOG-TYPES-03 — semantic catalog cutover

No live source-collection jobs have been started. The v2 semantic
SHA-256 is therefore used directly in the existing catalog_sha256 field,
already bound to server-side selection CAS and immutable job manifests.
There is no migration or dual-version SQLite infrastructure.

The existing canonical byte format and digest vectors are tested by
tests/test_source_catalog_semantics.py. It binds source identity,
source ordinal, chapter ID and URL, heading, author number, entry type,
classification evidence and confidence. A changed title/type invalidates
the old saved selection even if the chapter URL has not changed.

Source metadata flows through adapter → observed TOC → SQLite catalog →
ordered source manifest → checkpointed collection → staged import preview.
Unrecognized headings remain unknown. Unknown, extra and service records
are excluded by default until an explicit checkbox decision. ImportSessions
also requires a separate per-chapter create/replace/skip confirmation.
No invented author number is inferred from the TOC position.

Still gated separately: live site-rights/robots/TLS (#65), controlled
source-worker resource/backup acceptance (#83), optional expanded editor
UX (#98). This PR does not deploy to VPS or run Gemini/Google Drive.
