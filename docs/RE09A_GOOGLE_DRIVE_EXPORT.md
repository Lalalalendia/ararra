# RE-09A — ReadEra → two existing Google Drive folders (first code slice)

## Canonical user workflow

[Notion RE-09A](https://app.notion.com/p/3fc32a84beec815da939f4860a3728c1)
is the source of truth, **not** a generic backup or Google Drive ⇄ database sync:

1. ReadEra translates a chapter, stores the original and finished **machine**
   result in its existing SQLite job.
2. The server writes **one editable TXT per side** into the two **existing**
   regular Google Drive folders. Under each, it creates a **moderator-owned**
   folder (`books.owner_member_id`), then a normal book-named subfolder,
   then `Глава 001.txt`, `Глава 002.txt` etc. The moderator folder uses
   the exact member ID as its readable name (sanitized for Drive), not the
   identity of whoever started the translation job.
3. ChatGPT may edit **both** source and translation **in the same Drive file
   IDs**. The exporter **never updates, removes, renames or overwrites** them.
4. Akula later uses its existing Sync Core to fetch both folders to the PC;
   human review/publication remain separate. No S3 relay and no new
   incoming/reviewed/manifests/status folders.

Existing destination folders, not to be recreated:
- **Источники:** `1daqCzEVgPdX639YV5otTLxgVTPbfeBEp`
- **Переводы:** `1X2ZP8Fzbnq5MEAWJyGSErVGGFkfGBrR9`

## RE-09A-05: moderator → book → chapter (new canonical layout)

Preserve both previously authorized roots and owner OAuth credentials:

\`\`\`text
Источники/
  Firegod/
    Название книги [stable-book-key]/
      Глава 001.txt
Переводы/
  Firegod/
    Название книги [stable-book-key]/
      Глава 001.txt
\`\`\`

Here \`Firegod\` is only an example; the actual folder is named after
\`books.owner_member_id\` in SQLite. \`jobs.actor_id\` does not choose
the Drive folder. Both the moderator folder and the nested book folder
have private, stable \`appProperties\` identity tags. Retrying an export
reuses folder and TXT IDs even when a human renames them. The exporter
never modifies or deletes already-exported TXT bytes.

**Existing data:** older flat \`root/book/chapter\` folders must not be
shadow-copied into \`root/moderator/book/chapter\`. When such a book is found,
the exporter refuses further writes and requires an explicit, reviewed
move of the SAME existing book folder ID beneath the moderator, with
confirmation of both source and translation sides. Similarly, when a
book changes moderator owner, it refuses to duplicate an already-exported
book under the new owner; a reviewed move is a separate operation.
No automatic relocation, OAuth rotation, service restart or production
Drive mutation is part of this code migration.

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

## One-command owner setup on Windows (RE-09A-02b)

A practical blocker was verified through the currently connected Drive:
**both historical folder IDs returned HTTP 404**, even while unrelated
folders were visible. This is an account or permission/ID blocker, not
proof the folders were deleted. Do not create substitute folders.

For an operator with Windows, there is now
[`scripts/connect-google-drive.ps1`](../scripts/connect-google-drive.ps1).
It uses the exact two RE-09A folder IDs, opens Google account selection
and narrow Picker consent, and refuses a different folder selection.

First enable Drive API and Google Picker API on the Google Cloud project
and download a **Desktop app OAuth client JSON** from Google Cloud Console
to the owner's workstation (keep it out of GitHub and logs). From the
checked-out repository, install the CLI and run:

```powershell
py -m pip install -e .
.\scripts\connect-google-drive.ps1 -ClientJson "C:\private\desktop-oauth-client.json"
```

The script sets the two existing folder IDs in the process environment
only, reads the downloaded local JSON (not a pasted secret), and saves
a new token under `%LOCALAPPDATA%\ReadEra`. The **correct Google account
must be selected** if multiple accounts are signed in. If either folder
is absent from Picker, stop and verify ownership/sharing or outdated
folder IDs instead of choosing arbitrary replacements.

**Windows secret protection:** unlike POSIX, `os.chmod(0o600)`
does *not* restrict Windows ACLs. The CLI now removes inherited ACL
entries and grants file access only to the current account SID with
`icacls` **before writing the token**. Failure aborts and removes the
empty file. This is separately validated in Windows CI. As on Linux,
the resulting token is still a credential: do not copy it to a shared
folder, send it in chat or save it in Notion. Provision it over an
approved secure channel to the server's `google_drive_refresh_token`
systemd credential.

The command provides no OAuth credentials on its own: creating
the Google Cloud OAuth client and selecting the correct Google account
require a human. **The first actual Drive upload remains gated** by
issue #68 and must use a non-sensitive synthetic chapter before
enabling automatic uploads.

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

## Owner-only full live smoke after Google Picker (RE-09A-03)

The Google Drive connector in ChatGPT already successfully uploaded **two
synthetic TXT files into the approved roots**, read them back and edited both
in-place without changing their IDs. This proves the connector account's
permission only. It does **not** authorize the independent ReadEra OAuth
client: the connector token is neither transferred nor reused by ReadEra.

**To complete the independent ReadEra exporter acceptance** on the
owner's trusted Windows machine, first install the repository and create
the [Google Cloud Desktop app OAuth client](https://console.cloud.google.com/auth/clients).
Enable Drive API and Picker API, download its JSON privately, then run:

```powershell
py -m pip install -e .
.\scripts\connect-google-drive.ps1 -ClientJson "C:\private\desktop-client.json" -RunSmoke
```

The command opens Google account selection and Picker, requires *exactly*
the two existing folders and a narrow `drive.file` grant. The refresh
token is created once under `%LOCALAPPDATA%\ReadEra`, with current-user
Windows SID-only ACL before its contents are written. Never paste tokens
into ChatGPT, GitHub, Notion or CI logs.

- **Default:** without `-RunSmoke` it only validates writable folder
  metadata **and lists children using the new ReadEra OAuth identity**.
  Neither books nor Drive contents are changed.
- **Explicit `-RunSmoke`:** creates an entirely separate temporary SQLite
  queue with one synthesized *completed* book translation job. No production
  DB, Gemini request or publication is involved. It uses the **real
  `export_completed_job`** function to create one book subfolder in each
  existing Drive root and one `Глава 001.txt` file per side.
- Reads both remote files back and verifies bytes, parents, ownership tags,
  and MIME. Deliberately edits **only these two newly created synthetic
  files**, then executes the real exporter a second time. Their IDs, in-place
  edits and exactly-one-chapter cardinality must survive. Any bad OAuth
  grant, unsafe parent, incomplete listing, 403, or collision is a visible
  failure, never silently reinterpreted as a successful source export.
- Synthetic Drive files remain in clearly named
  `__RE09A_SMOKE_..._no_real_book` subfolders for inspection (no hidden
  cleanup/deletion). The temporary SQLite data is destroyed when the
  command exits. Earlier manually created test folders/files are not
  overwritten or touched.

If the owner already authorized the app, re-check without overwriting
the existing refresh token:

```powershell
.\scripts\connect-google-drive.ps1 -ClientJson "C:\private\desktop-client.json" -UseExistingToken -RunSmoke
```

The same program can be invoked directly from CLI with both exact
`CHAPTERA_DRIVE_*_FOLDER_ID` values configured:

```powershell
chaptera-server drive-smoke --client-json "C:\private\desktop-client.json" --token-file "$env:LOCALAPPDATA\ReadEra\drive-owner-refresh-token.txt"
chaptera-server drive-smoke --client-json "C:\private\desktop-client.json" --token-file "$env:LOCALAPPDATA\ReadEra\drive-owner-refresh-token.txt" --live-smoke
```

The first command is read-only. The second is an **actual** create/edit
test using ReadEra's own OAuth identity. CI simulates all Google responses;
CI results alone are not evidence that the owner completed these commands.

**VPS handoff is a separate protected credential operation.** Once
local owner acceptance is successful, provision the SAME OAuth client ID,
secret and refresh token to the VPS through systemd credentials
(`google_drive_client_id`, `google_drive_client_secret`,
`google_drive_refresh_token`). Set the same two
`CHAPTERA_DRIVE_*_FOLDER_ID` values; the Google connection is tied to
the OAuth application, not to the ChatGPT account session.

**On the VPS**, using the service account and its protected credentials:

```sh
# No --client-json or --token-file arguments. Never print secrets.
chaptera-server drive-smoke
# With explicit permission to create and edit TEST files only:
chaptera-server drive-smoke --live-smoke
# First inspect one actual completed book job without Google requests:
chaptera-server export-drive --job-id "<completed-book-job-id>" --dry-run
# Only after confirming the source, job revision, and destination:
chaptera-server export-drive --job-id "<completed-book-job-id>"
```

The first smoke is read-only, the second uses a fresh isolated synthetic
book and actually runs the exporter. Both use the **same server/systemd
credentials** as `export-drive`. They do not read the production SQLite
database. Successful local synthetic acceptance **alone** does not prove
that the VPS has been configured or that the server has exported a real
completed job. Do not put tokens into shell history, GitHub Actions,
Notion or Google Drive files.

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
