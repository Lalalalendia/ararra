# RE-02 — Timotxt (提莫書屋): novel RAW import profile

Status: **Draft / offline candidate**, not released or deployed.

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
- Unknown Hangul or private-use glyph substitution is a **needs_review**
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
