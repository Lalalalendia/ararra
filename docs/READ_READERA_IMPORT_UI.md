# ReadEra RE-02/03 — browser import wizard

## Delivered user workflow

The visual reference approved by the product owner is the four-screen ReadEra
concept in Canva (library, import/mapping, confirmation, editor). This PR
implements **real working UI for the library and import steps**, not the
two-column literary editor, publishing, or all of RE-03.

1. Open \`/import\` in the web client. Supply an active **team API key in memory
   for this page only**. No browser storage, URL embedding, or copied admin
   credentials. Backend identifies the member with \`GET /v1/me\`.
2. Upload TXT, HTML/HTM/XHTML, EPUB, or DOCX up to 12 MiB via file picker or
   drag-and-drop. \`POST /v1/imports/stages\` stores a **30-minute actor-owned
   temporary stage**, returning the same safe preview as the read-only route.
   Expanded source budget is 24 MiB; max 3 active stages per member. Each
   file selection uses a stable, member-scoped \`Idempotency-Key\` for retries.
3. Inspect all chapter titles/numbers/samples; missing and duplicate detected
   numbers intentionally have **empty destinations**, not guessed numbers.
   Every source chapter is explicitly designated create / replace / skip.
   If it has a pre-chapter introduction, choose **preserve separately** or
   **discard explicitly**. Full preface HTML is shown *as escaped text*, not
   executed in an HTML preview.
4. For a new book, set title, ID and **active moderator owner**. A moderator
   creates their own book; admin selects an active moderator from the
   admin-only list. Book responsibility is NOT a third global role or ACL.
   For existing books, select an actual \`GET /v1/books\` catalogue record.
5. For **replace**, type the exact existing chapter ID and press **Проверить**
   to fetch \`GET /v1/books/{book_id}/chapters/{chapter_id}\`. The server's
   chapter index/path and current \`source_revision\` are used; any editing
   of destinations/book selection invalidates the checked revision.
   The UI will not emit an unchecked replacement.
6. Confirm the exact operation summary. Only then:
   \`POST /v1/imports/stages/{stage_id}/confirm\` submits one decision for
   every source sequence. Database-side \`BEGIN IMMEDIATE\` guards all
   destinations and prevents partial writes even if an editor changes the
   target between inspection and commit. A 409 is shown **without auto-retry
   or silent change of revision**. Successful response shows server receipt;
   source import does NOT enqueue Gemini or publish the book.
7. The explicit **Отменить загрузку** button asks for confirmation and uses
   \`DELETE /v1/imports/stages/{stage_id}\` to free a staging slot.
   An existing confirmed book is never deleted. The user can recover a still
   active actor-owned stage by pasting its ID after re-entering their key.
   **Only the stage survives** in SQLite; edits to the unsent browser mapping
   are lost on page refresh and must be reviewed again.

The \`/books\` page now lists actual shared \`GET /v1/books\` responses, showing
the responsible moderator and owner revision without inventing publication
progress, chapter counts or translation quality states.

## HTTP and security boundaries

- Backend auth is always \`X-API-Key\`; the React pages do **not** implement
  server-side sessions or a login/refresh cookie. Users re-enter their key
  for each page after navigation/reload, and the key remains only in React
  memory while the component is mounted.
- Do **not** put these pages behind an unauthenticated public reverse proxy.
  Production requires HTTPS, a trusted access boundary, request-body limits
  and an explicitly assigned administrator. This PR does not deploy anything.
- Rulate JSON is **metadata only**. It is intentionally rejected from the
  staged import route; no chapters are fabricated. Persistent external
  catalogue IDs/links remain a separate RE-02 deliverable.
- Client-side mapping checks improve usability; **server transaction is
  authoritative**. 401/403/404/409/410/413/422 are shown as distinct errors.
  Any ambiguous lost-response after commit may be safely retried with the
  same exact confirmation, which returns the original receipt.
- A replacement preserves prior accepted translation history, but the
  accepted text can become stale. Source/QA/draft/accepted/published remain
  separate concepts. No auto acceptance or paid provider call.

## Source files and acceptance

| Contract | Source | Verification |
|---|---|---|
| Staged upload, GET/recover, DELETE, atomic POST confirm | \`src/chaptera_server/http/routers/imports.py\`, \`src/chaptera_server/import_sessions.py\` | \`tests/test_import_sessions.py\` |
| Strict decisions, safe paths, duplicate numbering, expiry, stale replace | \`web/src/features/import/model.ts\` | \`web/scripts/import-model.test.mjs\` (real TS transpiled and executed) |
| Memory-only authenticated fetch and typed JSON/multipart | \`web/src/shared/api/imports.ts\`, \`web/src/shared/api/team.ts\` | Web architecture guard; TypeScript check |
| Three-step React wizard, preface and checkout | \`web/src/features/import/ImportPage.tsx\`, \`ImportMapping.tsx\`, \`ImportReview.tsx\` | Web build; manual browser walkthrough still needed before prod |
| Shared book directory and navigation | \`web/src/features/books/BooksPage.tsx\`, \`web/src/routes/*\` | Web build |
| Source/backend boundaries | \`tests/test_architecture.py\`, \`web/scripts/architecture.test.mjs\` | Python 3.11/3.12 + web CI |

The CI workflow runs Python 3.11/3.12 API/SQLite tests plus JS runtime
decision-model tests, TypeScript typecheck and Vite production build. No
billable Gemini actions.

## Still open

- Browser automation/real screenshot QA against the approved Canva reference;
  unit/model and API contract checks are not a substitute.
- Persistent Rulate metadata-only catalog record management (external
  IDs/URLs), UI to review original/full chapters inside Reader, richer editor
  and glossary.
- Permanent production service remains blocked by the operator-owned
  systemd root bootstrap in
  [chaptera-site issue #46](https://github.com/Lalalalendia/chaptera-site/issues/46).
