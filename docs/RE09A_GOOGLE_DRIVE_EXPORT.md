# RE-09A — ReadEra → two existing Google Drive folders (first code slice)

## Canonical user workflow

[Notion RE-09A](https://app.notion.com/p/3fc32a84beec815da939f4860a3728c1)
is the source of truth, **not** a generic backup or Google Drive ⇄ database sync:

1. ReadEra translates a chapter, stores the original and finished **machine**
   result in its existing SQLite job.
2. The server writes **one editable TXT per side** into the two **existing**
   regular Google Drive folders. Under each, it creates a normal book-named
   subfolder, then `Глава 001.txt`, `Глава 002.txt` etc.
3. ChatGPT may edit **both** source and translation **in the same Drive file
   IDs**. The exporter **never updates, removes, renames or overwrites** them.
4. Akula later uses its existing Sync Core to fetch both folders to the PC;
   human review/publication remain separate. No S3 relay and no new
   incoming/reviewed/manifests/status folders.

Existing destination folders, not to be recreated:
- **Источники:** `1daqCzEVgPdX639YV5otTLxgVTPbfeBEp`
- **Переводы:** `1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9`

## Implemented: guarded single-job export CLI

```sh
# Explicitly set the existing folder IDs, not arbitrary shared root folders.
export CHAPTERA_DRIVE_SOURCE_FOLDER_ID=1daqCzEVgPdX639YV5otTLxgVTPbfeBEp
export CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID=1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9

chaptera-server export-drive --job-id "<completed-translation-job-id>" --dry-run
# After OAuth consent, with protected credentials installed on the server:
chaptera-server export-drive --job-id "<completed-translation-job-id>"
```

The server needs a Google OAuth 2.0 **refresh token** from the owner who
grants the ReadEra application access to both destination folders. Use
a securely managed server credential, not a ChatGPT connector token. The CLI
supports `CREDENTIALS_DIRECTORY` systemd credentials:
- `google_drive_client_id`
- `google_drive_client_secret`
- `google_drive_refresh_token`

For local tests, the equivalent opt-in environment variables are
`CHAPTERA_DRIVE_CLIENT_ID`, `CHAPTERA_DRIVE_CLIENT_SECRET`, and
`CHAPTERA_DRIVE_REFRESH_TOKEN`. Do **not** put credentials in GitHub,
Notion, CI logs or the SQLite translation database.

**Scope caveat:** Google recommends `drive.file` for app-created or
explicitly Picker-shared files/folders. Simply knowing the ID of a folder
created elsewhere does *not* grant this OAuth client access. Configure
Drive API in the OAuth project's Google Cloud Console, complete owner consent
and grant access to **both existing folders** through an appropriate
supported picker/authorization path. If per-file scope cannot reach these
preexisting destinations, STOP and review the smallest defensible scope /
Google Workspace sharing configuration before broadening to restricted
`drive`. Never silently request broad access. OAuth consent/refresh-token
bootstrap is **not implemented** in this first code slice; credentials
must be explicitly provisioned by the operator.

## Safety and idempotency

- The CLI reads one SQLite snapshot and requires a **completed, book-owned**
  translation job whose original, source revision, glossary revision and
  persisted source hash still match the current book chapter. It cannot
  export a pending, stale or orphaned translation.
- Original `source_html` and `result_html` are written literally as
  UTF-8 `text/plain` (up to 4 MiB each). HTML fragments remain visible
  as text and never execute. This is a **machine result, not acceptance**;
  no book acceptance, publication or billing is triggered.
- Both root destinations must exist, have the folder MIME type, and allow
  adding children before the first mutation. A book folder name contains a
  stable book ID digest to prevent same-title book collisions.
- Files are tagged with private Drive `appProperties` identity
  (book ID digest, job ID, side, original SHA). These are not new manifest
  files. A matching existing file is reused **without reading or rewriting
  its body**, so human/ChatGPT modifications are preserved.
- For unknown foreign same-name files/folders, multiple matching names,
  malformed or truncated `files.list` pagination, HTTP/permission/OAuth
  failure or changed original revisions, fail closed.
- Drive has no two-file atomic commit. If the source upload succeeds and
  translation fails, a deliberate command retry retains the former source
  file ID and creates only the missing translation. It never rolls back
  a human-edited file. **Run only one exporter for the same book/position
  at a time** until a cross-process job fence is implemented. Google Drive
  does not enforce unique filenames.
- The client uses fixed HTTPS Google API endpoints, verifies TLS via httpx,
  refuses redirects/environment proxies, uses per-request deadlines, and
  returns status codes without logging access or refresh tokens.
- The Drive UI may display more user-owned files than the app's `drive.file`
  scope can enumerate. Do not treat a scoped listing as authoritative for
  arbitrary foreign files; assess this during OAuth/live acceptance.

## Acceptance status

The code is covered by isolated HTTP-mocked tests: two existing root folders,
correct parts, repeated export without duplicates, preservation of *both*
Drive edits, recover after partial upload, non-owned filename collision,
partial listing and OAuth refusal, source/glossary revision fence, and dry
run without Google credentials. Entire-repository CI must be green before
merge; this is **not** a live Drive upload or end-to-end delivery to Akula.

Unmet gates in order:
1. Owner authorizes this OAuth client for the two existing folder IDs; read
   them back through **the same credentials that ReadEra will use**.
2. A synthetic permitted test chapter is exported to both folders, reopened,
   edited **in place** via ChatGPT, then exported again without overwriting.
3. Connect the Google Drive provider to Akula's current Sync Core and test
   Drive → local delivery with conflict preservation.
4. Add a single queued post-completion export trigger and bounded retry only
   after the manual E2E succeeds; never transform a Drive export error into
   a failed Gemini translation.

Google API documentation:
- [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [OAuth server application flow](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Upload files](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
- [List/search files](https://developers.google.com/workspace/drive/api/guides/search-files)
