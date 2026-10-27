# RE-02-FCA-01: Full Cycle App source ZIP import

## Operator workflow
1. Export a **source** ZIP with the existing Full Cycle App chapter-exchange function.
2. Select the ZIP in Erida's import form; inspect the manifest-defined TOC and warnings.
3. Explicitly create/replace/skip each entry. Replacement requires the live source revision.
4. Erida saves the **working** chapter as canonical source HTML and keeps the original parser TXT as immutable evidence.
5. Recover prior copies using GET /v1/imports/books/{book_id}/chapters/{chapter_id}/source-originals.

## Byte contract
Reads full_cycle_app.chapter_exchange version 1, kind source, unchanged.
The manifest must list independent chapters with chapter_index, title, URL,
working_file, original_file and SHA-256 for both. TXT is strict UTF-8.
Each filename must be chapters/{chapter_index:06d}.txt or original/{chapter_index:06d}.txt.
These explicit boundaries replace *all* title-guessing regexes. An index is an
import destination hint, not proof of the author's chapter number.

Only manifest.json, optional README.txt and referenced TXT members are accepted.
Duplicate names, unknown members, symlinks, ZIP traversal, encrypted archives,
missing files, corrupted SHA, bogus metadata and unsupported kinds fail closed.

## Capacity, retention and caveats
- 32 MiB compressed ZIP upload; 64 MiB expanded; 80 MiB staging budget.
- Up to 2000 source chapters, with 2,000,000 characters per entry.
- Other upload formats retain their existing 12 MiB cap.
- Temporary private stage expires after 30 minutes.
- Confirm is one atomic SQLite transaction with explicit selections, revision CAS,
  actor-owned stage, no silent replace, and idempotent replay.
- Parser originals and working copies are append-only for each import stage.
- The archive contains only *exported* chapters: no evidence that the website
  catalog was exhaustively downloaded. Gaps are flagged; coverage stays unverified.
- No Gemini, remote Drive, site scraping, VPS or Full Cycle App changes.

## Acceptance
Tests in tests/test_full_cycle_exchange_import.py verify manifest identity,
arbitrary titles, missing positions, checksums, invalid ZIPs, HTTP auth,
preview isolation, atomic confirmation, replay, originals, and replacement CAS.
Experimental Rust preview must separately implement this format before
production Rust routing; Python remains the authoritative importer.
