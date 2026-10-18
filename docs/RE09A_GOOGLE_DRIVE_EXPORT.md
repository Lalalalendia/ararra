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

## Local owner OAuth and folder selection (RE-09A-02a)

The [Google Picker desktop authorization flow](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
can grant the ReadEra OAuth application the two **preexisting** folders via
the recommended limited `drive.file` scope. The 2026 documentation
specifically supports `trigger_onepick=true`, `allow_multiple=true`,
`allow_folder_selection=true` and `file_ids=...`. This is a one-time,
**owner-approved** flow on their trusted workstation — *never* the
ChatGPT connector's OAuth token.

Prerequisites: enable **Drive API and Google Picker API** for the Google
Cloud project and create a **Desktop app** OAuth client with PKCE
support. Configure credentials and both folder IDs in a trusted local
shell (never in the repository or chat):

```sh
export CHAPTERA_DRIVE_CLIENT_ID='<desktop-oauth-client-id>'
export CHAPTERA_DRIVE_CLIENT_SECRET='<desktop-oauth-client-secret>'
export CHAPTERA_DRIVE_SOURCE_FOLDER_ID='1daqCzEVgPdX639YV5otTLxgVTPbfeBEp'
export CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID='1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9'

# Run ON the owner's own computer with browser and loopback network.
chaptera-server drive-authorize --token-file /absolute/protected/readera-refresh-token
# --no-browser prints an authorization URL instead, opened on SAME computer.
```

The command binds a temporary listener to `127.0.0.1` on a random
port, generates an independent OAuth state nonce and S256 PKCE verifier,
opens the system browser, requests exactly `drive.file`, and **requires
the user to choose both exact existing folder IDs**. Unknown selections,
OAuth errors and state mismatches are rejected. It exchanges the code for
an offline refresh token, verifies that **both folders are writable
through that same OAuth identity**, then saves the token in a **new**
operator-selected file with permission mode 0600; existing files,
including symlinks, cannot be overwritten.

Neither the authorization code nor refresh/access token is logged or
printed. The *authorization URL* may be printed on the local console to
make manual browser opening possible; don't copy it into tickets or logs.
The output token must then be provisioned to the VPS as
`google_drive_refresh_token` in the protected systemd credentials
directory, alongside the **same client ID / client secret used during
consent**. Alternatively configure `CHAPTERA_DRIVE_REFRESH_TOKEN`
only in a controlled nonproduction environment.

A loopback URI is meant for a Desktop OAuth client; running this
command over SSH on a headless VPS will not redirect into the
operator's workstation browser without extra network setup, and is
not the default procedure. Do not use Google's old deprecated
out-of-band copy/paste code flow.

**Next live acceptance:** run the bootstrap with actual owner consent,
verify the two existing folder IDs, then a **synthetic test chapter**
via `export-drive`. The OAuth code is implemented and offline-tested;
actual Google credentials and owner consent have not been provided, so
this is **not evidence of completed live authorization**.

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
