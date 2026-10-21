# RE-02 — Timotxt (提莫書屋): novel RAW import profile

Status: **Parser merged in main by PR #118; live-site gate not passed and no VPS deployment.**

## Product reason

Nine specific Atarax books use timotxt.com as their listed source.
Other Notion databases track Marvel and Bleach fanfics from the same site.
This is an actual novel and fanfic source, not a miscellaneous literature
website. The ReadEra workflow is book URL -> full TOC -> user selection ->
verified RAW -> staged import -> existing translation and editorial flow.

- [Notion Timotxt source register](https://app.notion.com/p/3fd32a84beec81bd90e4ee85f629534f)
- [Previous FullCycle 429 evidence](https://app.notion.com/p/3d132a84beec8135b8f2c772ce145a54)
- [MED expansion contract](https://app.notion.com/p/3fe32a84beec813cb650cab7f4dfe615)

## Source contract

- Only the two exact HTTPS hosts www.timotxt.com and timotxt.com.
- Books use ten-digit IDs, including leading zeroes; chapters use their
  true same-book numeric HTML URL identities.
- The book landing page only previews recent chapters. Follow an
  **observed exact same-book /dir link**, never guess an endpoint.
  Parse the complete, ascending directory list, not the reverse-order teaser.
- Preserve title, source ID, TOC order, author number and auxiliary/service
  records as separate facts. Match declared book chapter count, rejecting
  truncated or inconsistent directories.
- Extract narrative from the explicit .content node only, excluding site
  navigation, advertising and known standalone footer lines.
- Assemble observed same-chapter _2.html etc. continuations in order,
  capped at 12 parts, before committing one chapter checksum/checkpoint.
  Missing/foreign/gapped parts fail; no successful half-chapter checkpoint.
- Any unresolved Hangul or private-use glyph is a **needs_review**
  error, not guessed Chinese text. Preserve exact bytes where supported.
- Request rate is scoped by existing SQLite source limiter:
  6 s minimum, 240 requests per UTC day, one simultaneous source request.
  HTTP 429 defers the whole job, honoring bounded Retry-After/cooldown;
  it does not trigger CAPTCHA, browser or session circumvention.

## Verification and limits

Synthetic-only acceptance covers TOC, URL identity, unrelated site links,
numeric Chinese chapter labels, service/extra classification, linked parts,
429 on the second part, invalid or obfuscated HTML, source worker checkpoint,
and authenticated source-to-Stage import. No real copyrighted text is stored
in CI fixtures.

A real-site release still needs site robots/terms/access review, a permitted
metadata-only book observation and one authorized chapter quality receipt
from the deployment environment. ReadEra's release gate #65 and RAM/disk/
backup requirements remain in force. No production source-worker is enabled.

Timotxt sometimes transforms original glyphs. The earlier FullCycle project
has normalization research, but no universal, proven mapping was ported.
Whole-book counts over the generic 2000 cap or directories split across
unrecognized lists remain unsupported until separately verified.

## Integrity follow-up: CSS-independent page continuation

The initial parser looked only in three navigation CSS containers. A source
template update could place a real `_2.html` link elsewhere while returning
a valid HTTP 200, causing a **silent partial chapter accepted as complete**.

The follow-up now scans all observed anchor URLs for the strict adjacent
same-book/same-chapter part identity, while a visible `下一頁` or `下一页`
link with no valid target is a hard `catalog_incomplete` error. This is
source-body fidelity logic, **not a license to guess undisclosed URLs**.
A second part yielding the exact text of an earlier part triggers
`catalog_conflict` rather than storing duplicate prose. Tests cover a
changed pager CSS wrapper, orphan/missing/cross-book next links and a
wrong server response repeated on part 2.

The system still cannot infer an unadvertised continuation. Operational
live validation must verify the actual chapter navigation markup,
authoritative chapter completeness and rights/robots before a
source-specific production support claim.

## Operator-owned metadata probe, without chapter downloading

A bounded read-only diagnostic checks one **already registered Atarax**
Timotxt book from the actual execution network. It never requests chapter
body URLs, creates source jobs, sends Gemini requests, or writes to the book
library. The normal PublicHtmlFetcher checks DNS/IP pinning, TLS,
robots.txt, redirects, source rate pacing and response sizes.

After confirming site terms and permitted metadata access, and while
the regular source-worker is stopped, the operator may run:

```sh
python scripts/probe_timotxt_metadata.py --book-id 2010493463 --approved-metadata-check
```

The program checks the book landing and its explicitly linked full
directory, returns only source IDs count, advertised count, coverage,
semantic catalog checksum and classification counts. It reports no
chapter titles or prose. Errors provide a `blocked_or_incomplete`
receipt, and a passing receipt still carries
`production_acceptance: pending authorized body, VPS backup and quotas`.

This command is **not run in automatic CI** or on the production VPS
by this PR. A GitHub-hosted test is not evidence that the site's robots
policy and anti-rate-limit behavior permit collection from the VPS.
The next authorized stage remains an independently approved one-chapter
pilot through `source-worker --run-once --job-id`, not an unbounded crawl.
