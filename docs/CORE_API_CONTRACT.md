# Stable translation API contract — baseline v0.5 / import addition v0.6

This is a **server-only contract**. `full_cycle_app` and the Windows executable are unchanged. Tests in `tests/test_api_contract.py` preserve the pre-import behavior.

## Runtime and inputs

- Python 3.11 and 3.12; headless import without PyQt, Windows runtime or desktop SQLite.
- `GET /healthz` is the sole public endpoint; all `/v1/*` methods require the team member's `X-API-Key`. Missing or invalid keys return 401. Team members share one workspace.
- Exactly two **global** team roles: `admin` (one member selected by `CHAPTERA_ADMIN_MEMBER_ID`) and `moderator` (every other valid team token). No book-local role table or permissions. Without explicit admin configuration, no caller gains admin controls.
- `GET /v1/me` returns only `{"member_id":"...","role":"admin|moderator"}` for any authenticated member.
- `GET /v1/admin/members` requires an administrator: missing/wrong token -> 401, valid moderator token -> 403, admin token -> `{"members":[{"member_id":"...","role":"..."}]}` without credentials. This endpoint is read-only; token provisioning is still operator-managed.

- `POST /v1/jobs` accepts a single chapter (`chapter_id`, `source_html`), optionally source/target language, scoped glossary, context and protocol. Omitting `transport` preserves **`html_v0`** for existing clients. `atoms_v1` must be selected explicitly.
- `Idempotency-Key` is scoped to a team member. A second identical request returns the same job; reusing the key with changed input produces 409.
- `GET /v1/jobs/{id}` and `/events` expose status/output and progress, and `POST /cancel` requests cancellation.
- Book-managed endpoints permit creation, `PUT /v1/books/{id}/chapters/{id}`, glossary editing, translations and accepted revisions. Source and accepted revisions are guarded by the caller's expected revision; conflicts return 409. An ordinary translated job never automatically becomes an *accepted* editorial revision.
- Only validated, structurally complete translations become `completed`; failure/partial output is not accepted and retries are bounded.

## New v0.6 read-only import preview

`POST /v1/imports/preview` accepts one multipart field named `file`. The response contains an outline and **bounded samples, not full source texts**. This endpoint never creates a book, chapter or translation, never reaches out to Rulate/Gemini and does not invoke a translation worker.

An ordinary book upload returns:

```json
{
  "kind":"book", "format":"txt", "filename":"sample.txt", "title":"sample",
  "chapter_count":1,
  "chapters":[{"sequence":1,"detected_number":517,"suggested_index":517,
    "title":"Глава 517", "source_path":"sample.txt", "characters":200,
    "sha256":"<64-hex-digit SHA-256>","sample":"<up to 180 characters>","has_text":true}],
  "unassigned_prefix_characters":0,"unassigned_prefix_sample":"",
  "warnings":[],"stored":false
}
```

The content lengths and hashes are descriptive only, **not** proof of translation quality. Chapter numbers recognized in the original are never silently renumbered. Repeated numbers or text outside chapter boundaries require a later preview-and-confirm decision before data can be stored.

Supported contents:

| Format | Parsing | Current limit |
|---|---|---|
| `.txt` | UTF-8/UTF-16/CP1251; Russian/English/Chinese/Japanese chapter headings | 12 MiB input |
| `.html`, `.htm`, `.xhtml` | `<h1>`–`<h4>` chapter headings, sanitized metadata preview | 12 MiB input |
| `.epub` | OPF manifest + ordered spine XHTML, no automatic image fetching | 12 MiB input, 64 MiB archive expansion |
| `.docx` | Word paragraph XML and chapter labels (no embedded media) | 12 MiB input, 64 MiB archive expansion |
| `.json` | **Exactly** the `rulate_account_metadata` export schema v1 created by the existing Windows tool | 12 MiB input |

For the Rulate metadata JSON the response has `kind=metadata_catalog`, arrays of books and chapter links, `has_text=false`, and a warning that no chapter text exists. It is **not** equivalent to an original-text import. This does not log in to Rulate, scrape websites or restore publication credentials.

Limits: 2,000 book chapters, 2 million visible characters per chapter, 2,000 metadata-only books and 50,000 metadata chapter links. Unsafe ZIP paths, encrypted archives, archive bombs and XML DTDs are rejected. 413 indicates an oversized uploaded file; 422 indicates unrecognized/invalid content. Set HTTP ingress/request-body limits before external deployment; this development API is not internet-hardened.

## RE-02: opt-in staged full-source import (additive API)

**The existing \`POST /v1/imports/preview\` remains read-only and keeps its
v0.6 response shape.** Staging and confirmation require additional authenticated
calls; no upload is ever silently treated as a successful import.

1. \`POST /v1/imports/stages\`, multipart \`file\`, optional \`Idempotency-Key\`:
   accepts TXT/HTML/EPUB/DOCX, parses and persists an **actor-owned** temporary
   session in the same SQLite DB as the shared BookLibrary. Returns \`201\`
   and \`{stage_id,status:"staged",file_sha256,expires_at,preview,
   unassigned_prefix_saved}\`; \`preview\` contains the old safe samples, not
   chapter body text. Maximum original upload is 12 MiB; maximum expanded
   staged candidate+preface JSON is 24 MiB. Up to three uncommitted sessions per
   member. Unconfirmed sessions expire in 30 minutes. Reusing the same
   member-scoped idempotency key with different file bytes/name yields 409.
2. \`GET /v1/imports/stages/{stage_id}\`: owner only (404 for other members).
   Returns the preview and **full unassigned_prefix_html** for review. No
   generated translations or public chapter body.
3. \`POST /v1/imports/stages/{stage_id}/confirm\`: owner only, under
   \`BEGIN IMMEDIATE\`, one atomic transaction over book creation, chapter
   revisions, optional preserved preface and stage receipt. Every original
   sequence number must have exactly one choice. Any invalid or stale choice
   rolls the entire confirmation back; replays of the exact same confirmation
   return the original receipt. A different confirmation after commit yields 409.
4. \`GET /v1/imports/books/{book_id}/unassigned\`: authenticated shared-team
   retrieval of explicitly preserved pre-chapter text. The text is not
   automatically assigned a chapter number, translated or published.

For example, confirm two chapters into a **new** book after inspecting their
detected numbers and deciding they belong at indices 12 and 13:

\`\`\`json
{
  "book_id":"demo",
  "book_mode":"create",
  "title":"Новелла",
  "prefix_policy":"preserve",
  "chapters":[
    {"sequence":1,"action":"create","chapter_id":"12",
     "chapter_index":12,"chapter_path":"chapters/12.xhtml","expected_revision":0},
    {"sequence":2,"action":"create","chapter_id":"13",
     "chapter_index":13,"chapter_path":"chapters/13.xhtml","expected_revision":0}
  ]
}
\`\`\`

Every chapter decision is explicit: \`action=create|replace|skip\`.
A skip must specify no destination. A replacement requires an existing exact
\`chapter_id\` and its **live \`expected_revision\`**. An unnumbered or
duplicate-heading chapter must be mapped manually; any duplicate target
chapter_id/index/path rejects the whole batch. For an existing book use
\`book_mode=existing\` (no implicit book creation or rename). Even if two
editors race, stale revisions fail closed. Source replacement increments the
source revision, preserves all previously accepted translation versions, and
makes old accepted content **not current** until explicitly accepted again.

Unassigned pre-chapter material is not thrown away silently. When present,
\`prefix_policy\` must be \`preserve\` (stays in separately retrievable
\`book_import_unassigned\`) or \`discard\` (explicit intentional exclusion).
Default \`reject\` prevents confirmation when an unassigned preface exists.
Detected chapter headings are restored into stored canonical source HTML.
Sessions never change the job queue and never call Gemini.

**Error codes:** 401 missing/invalid member token, 404 unknown/unowned
stage/book, 410 expired stage, 409 conflicts/idempotency mismatch/ambiguous
selection, 413 upload or expanded-session budget, 422 unsupported or metadata-
only input. Committed receipts are retained for up to one day.

**Not implemented in this RE-02 slice:** persistent imported Rulate metadata
URLs/IDs, import staging UI, public authentication, automatic Rulate scraping,
non-text media import, a bulk publication button, long-lived source-asset
storage outside SQLite. The metadata-only JSON **still works for read-only
preview** and is explicitly rejected for source chapter staging. Do not claim
that a metadata-only catalog contains downloadable chapters.

The next RE-02 work is metadata URL/id persistence and the browser book-import flow. No metadata-only JSON can create source chapters.


Example for a trusted local developer instance:

```bash
curl -X POST http://127.0.0.1:8000/v1/imports/preview \
  -H 'X-API-Key: YOUR_TEAM_TOKEN' \
  -F 'file=@novel.epub'
```
