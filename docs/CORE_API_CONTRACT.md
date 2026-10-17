# Stable translation API contract — baseline v0.5 / import addition v0.6

This is a **server-only contract**. `full_cycle_app` and the Windows executable are unchanged. Tests in `tests/test_api_contract.py` preserve the pre-import behavior.

## Runtime and inputs

- Python 3.11 and 3.12; headless import without PyQt, Windows runtime or desktop SQLite.
- `GET /healthz` is the sole public endpoint; all `/v1/*` methods require the team member's `X-API-Key`. Missing or invalid keys return 401. Team members share one workspace.
- Exactly two **global** team roles: `admin` (one member selected by `CHAPTERA_ADMIN_MEMBER_ID`) and `moderator` (every other valid team token). No book-local role table or permissions. Without explicit admin configuration, no caller gains admin controls.
- `GET /v1/me` returns only `{"member_id":"...","role":"admin|moderator"}` for any authenticated member.
- \`GET /v1/admin/members\`: administrator-only. Returns \`{members:[{member_id,role,status,credential_source}]}\`, never a token/digest. Existing static credentials remain supported; managed overrides show \`credential_source=managed\`.
- \`POST /v1/admin/members\` with \`{"member_id":"editor2"}\`: admin-only, **201** one-time \`access_token\` for a NEW moderator. The role cannot be supplied or elevated.
- \`POST /v1/admin/members/{member_id}/rotate\`: admin-only, **200** one-time replacement \`access_token\`, immediately invalidating prior static OR managed token. A previously disabled moderator is reactivated only by rotation.
- \`POST /v1/admin/members/{member_id}/disable\`: admin-only, **200** status disabled. Invalidates all currently known moderator credentials, cancels queued/retry jobs and signals running jobs to stop. **409** if the moderator still owns any books after the separate ownership schema is introduced: reassign those books first.
- Error statuses: 401 invalid/missing token, 403 moderator trying an admin action, 404 unknown moderator, 409 duplicate ID/admin credential operation/book ownership conflict, 422 invalid member ID. All admin and token-issuance replies set \`Cache-Control: no-store, private\`.

### Moderator book ownership (RE-03-OWNER-CAS)

ReadEra still has exactly **two global roles**, administrator and moderator,
and **all authenticated members may view and work on the shared library**.
Ownership is a separate accountability attribute, not per-book access control.

- Newly created books must name one ACTIVE moderator owner. For a moderator
  caller this defaults to their own member ID; the administrator explicitly
  supplies \`owner_member_id\` when creating a new book or confirming a staged
  import. Managed moderators issued in \`/team\` are valid owners, just like
  configured static moderators.
- \`POST /v1/books\`: additive \`owner_member_id\` field. \`GET /v1/books\`
  returns a shared catalogue of book IDs/titles and owner fields.
- \`GET /v1/books/{book_id}\` now includes \`owner_member_id\` and
  \`owner_revision\` (initial revision 0).
- \`PUT /v1/books/{book_id}/owner\`: administrator only, body
  \`{"owner_member_id":"active_moderator","expected_owner_revision":0}\`.
  Atomic CAS rejects stale owner revision as **409**, and rejects unknown/
  disabled/admin owners as **422**. Owner history is appended in SQLite
  \`book_owner_events\` without modifying sources, glossaries or accepted text.
- Preexisting SQLite books migrate with \`owner_member_id=NULL\`. NEVER guess
  an owner from a book's title, first translator, imported metadata or the
  admin account; explicitly assign a real moderator using the admin endpoint.
- Admin may not disable a moderator until all their owned books are reassigned.
  The moderator's key may otherwise be rotated without changing ownership.

### Moderator access security invariants (RE-03-MEMBER-MGMT)

- The **single administrator's** ID and bearer key remain solely in the protected \`team_tokens_json\` systemd credential/config, selected explicitly via \`CHAPTERA_ADMIN_MEMBER_ID\`. No implicit admin and **no HTTP endpoint** for changing/admin promotion.
- Only **moderators** may be issued/rotated/disabled over authenticated admin API. New tokens have >=256 random bits (\`secrets.token_urlsafe(32)\`); SQLite stores **only SHA-256 digests**, never the bearer secret. The issue/rotation response displays the token once; subsequent GET/list/reload cannot recover it.
- Static configured moderator keys remain compatible until an admin rotates or disables that member. At that point a DB override rejects the old static key immediately, including without a process restart. Existing administrator key is exempt from DB overrides.
- Moderator credentials persist across API restarts in the same SQLite database (back it up). Revocation and job cancellation are committed in one SQLite transaction, with audit events that contain only member IDs/actions, **no token bytes**. An already-running provider request may have been billed before cancellation is observed; do not promise network-level abort.
- UI at \`/team\` requires manual administrator key entry **per browser tab**. Secret stays in React component memory, never browser storage or URL. HTTP calls go through \`web/src/shared/api/team.ts\`; Vite proxies \`/v1\` for local development. Browser is not a substitute for HTTPS or a locked-down VPN/tunnel in deployment.
- Sending invitations by email, public signup, account password reset, cookie sessions, one-time magic links, rotating the administrator credential, and production deployment are **not** part of this package.


- `POST /v1/jobs` accepts a single chapter (`chapter_id`, `source_html`), optionally source/target language, scoped glossary, context and protocol. Omitting `transport` preserves **`html_v0`** for existing clients. `atoms_v1` must be selected explicitly.
- `Idempotency-Key` is scoped to a team member. A second identical request returns the same job; reusing the key with changed input produces 409.
- `GET /v1/jobs/{id}` and `/events` expose status/output and progress, and `POST /cancel` requests cancellation.
- Book-managed endpoints permit creation, `PUT /v1/books/{id}/chapters/{id}`, glossary editing, translations and accepted revisions. **`GET /v1/books/{book_id}/chapters`** is an authenticated shared-library TOC sorted by numeric chapter index and ID. It returns chapter ID/index/path and `source_revision`, `accepted_revision`, `accepted_source_revision`, `accepted_current` only; original and accepted full texts are fetched individually via `GET /v1/books/{id}/chapters/{id}`. 404 for a missing book; no guessed publication status. Source and accepted revisions are guarded by the caller's expected revision; conflicts return 409. An ordinary translated job never automatically becomes an *accepted* editorial revision.
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
3. \`DELETE /v1/imports/stages/{stage_id}\`: owner-only cancellation of an **uncommitted** stage. Returns \`{stage_id,status:"cancelled"}\`; frees a staging slot immediately and does not change any books, accepted translations or jobs. A committed stage cannot be cancelled (409); unknown/foreign stage is 404; expired is 410. The operation intentionally requires explicit editor confirmation in the browser.
4. \`POST /v1/imports/stages/{stage_id}/confirm\`: owner only, under
   \`BEGIN IMMEDIATE\`, one atomic transaction over book creation, chapter
   revisions, optional preserved preface and stage receipt. Every original
   sequence number must have exactly one choice. Any invalid or stale choice
   rolls the entire confirmation back; replays of the exact same confirmation
   return the original receipt. A different confirmation after commit yields 409.
5. \`GET /v1/imports/books/{book_id}/unassigned\`: authenticated shared-team
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

The next RE-02 work is metadata URL/id persistence and the browser book-import flow.

### RE-05: shared editorial drafts, review notes and accepted history

Every endpoint below is under \`/v1/books/{book_id}/chapters/{chapter_id}\`
and uses the normal \`X-API-Key\` team member authentication. There are only
two global roles (admin/moderator); the book owner is a responsibility field,
not an exclusive ACL.

- \`GET /draft\`: current shared draft with \`revision\`, \`source_revision\`,
  \`accepted_revision\`, \`body_text\` (plain text), \`actor_id\`, \`updated_at\`.
  When absent, returns an empty draft with revision 0 and the current chapter
  revision bases. No draft is fabricated from the LLM queue.
- \`PUT /draft\`: \`{body_text,expected_revision,expected_source_revision,
  expected_accepted_revision,rebase:false}\`. SQLite \`BEGIN IMMEDIATE\` CAS
  requires both chapter revisions and exact previous draft revision.
  \`rebase:true\` is explicit user acknowledgement when a saved draft targets
  a superseded source or accepted revision; stale unacknowledged drafts fail
  with 409. Each changed draft is appended to \`editor_draft_revisions\`.
  The draft is separate from the accepted text and never published.
- \`GET /notes\` and \`POST /notes\`: bounded review findings stored as separate
  DB rows linked to source, shared-draft and accepted revision numbers.
  \`POST\` body is \`{body_text,expected_source_revision,
  expected_draft_revision,expected_accepted_revision}\`; changed bases cause
  409. The authenticated actor is authoritative, not a user-supplied field.
- \`PATCH /notes/{note_id}\`: \`{status:"open"|"resolved",
  expected_revision}\` with per-note CAS. Resolutions keep the author and
  record resolver identity. These notes are NOT concatenated into chapter
  bodies or translation exports; all fields are escaped in browser UI.
- \`GET /accepted-revisions\`: metadata-only up to 100 latest accepted
  revisions, newest first (revision,source_revision,actor_id,created_at).
  \`GET /accepted-revisions/{revision}\`: that single historical translation.
- \`POST /restore-accepted\`: \`{revision,expected_current_revision,
  expected_source_revision}\`. Checks exact current revisions and requires
  the historical translation's source revision equal the current source.
  Atomically appends a NEW accepted version with audit actor; never changes
  historical records or source text. 409 for stale/current choice, 404 for
  absent revision. This is not Publish.

The editor keeps credentials in memory, persists plain-text Draft separately
from Accepted and notes separately from both. It performs no paid LLM call.
Partial-save errors or collisions must not silently replace text in the UI.
 No metadata-only JSON can create source chapters.


Example for a trusted local developer instance:

```bash
curl -X POST http://127.0.0.1:8000/v1/imports/preview \
  -H 'X-API-Key: YOUR_TEAM_TOKEN' \
  -F 'file=@novel.epub'
```
