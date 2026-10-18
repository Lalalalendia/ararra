# ReadEra RE-06C — durable chapter batch submission (first API slice)

This implementation makes the existing single-worker, moderator-key queue usable
for an explicit multi-chapter selection. It **does not** introduce another
scheduler, Gemini key, background process or publication path.

## Client workflow

1. Obtain source revisions and numeric positions from
   `GET /v1/books/{book_id}/chapters`; obtain the current glossary revision from
   `GET /v1/books/{book_id}`. All requests require a team session (with CSRF
   for mutations) or an authenticated `X-API-Key`.
2. `POST /v1/books/{book_id}/translation-batches/preview` with the selected
   chapter IDs, their exact `expected_source_revision`, the book's
   `expected_glossary_revision`, and translation options.
3. Display the returned chapters (numeric chapter order) and require explicit
   human confirmation of the exact `manifest_sha256`.
4. `POST /v1/books/{book_id}/translation-batches` with the same JSON, the
   returned `expected_manifest_sha256`, and a stable **Idempotency-Key** header
   (1–128 characters). Store the returned `batch_id`.
5. Resume/inspect through `GET /v1/books/{book_id}/translation-batches/{batch_id}`;
   list recent batches with `GET .../translation-batches`. Progress is a join
   of the existing durable jobs; no separate progress engine is involved.

Minimal preview JSON:

```json
{
  "expected_glossary_revision": 0,
  "chapters": [
    {"chapter_id": "chapter-1", "expected_source_revision": 1},
    {"chapter_id": "chapter-2", "expected_source_revision": 1}
  ],
  "source_language": "Chinese",
  "target_language": "Russian",
  "transport": "html_v0",
  "max_attempts": 3
}
```

The confirm request adds `"expected_manifest_sha256": "<sha256 from preview>"`
and the `Idempotency-Key` HTTP header. An identical retry returns the original
batch **and the same job IDs**, even after a server restart. Reusing a key with
different content returns HTTP 409.

## Browser workflow (RE-06C second slice)

In `/app/books`, open **Перевести главы** for an existing book:

1. Choose individual source chapters or use **Выбрать первые 100**. Numeric
   ordering is taken from the live SQLite-backed TOC, not from filename order
   or character length. Only chapter IDs and source revisions are loaded,
   never the full chapter text.
2. Keep `html_v0` (default) or explicitly select experimental `atoms_v1`;
   optionally set 1–10 maximum attempts. Changes discard prior approval.
3. Click **Проверить партию**. A read-only server preview returns a SHA-256
   receipt for the exact current chapter and glossary revisions. This makes
   zero queued jobs. Review the count/hash, then tick the explicit confirmation.
4. Click **Подтвердить отправку в очередь**. The client sends a fresh random
   idempotency key and the exact preview payload in one API request. Failed or
   ambiguous replies keep the **same** key/payload for safe manual retry.
   After a confirmed response, selection is cleared to prevent a second
   accidental batch submission.
5. See per-chapter statuses in the same window. Use **Сохранённые партии** and
   **Обновить прогресс** to recover after browser reload or sign-in. History
   is read from server SQLite, not browser persistence. Status `completed`
   means a generated result exists, **not** that RU has been accepted.

The dialog uses the existing HttpOnly team session and CSRF-bearing request
boundary, and uses only text-safe DOM APIs for untrusted IDs. It has a bounded
mobile layout and never displays API keys, source HTML or translated text.

Chromium acceptance exercises the real FastAPI/SQLite path without starting a
Gemini worker: no jobs before acknowledgement, two jobs after one confirm,
progress after page reload, and HTTP 409 with no extra jobs when a selected
source is changed after preview. A real paid full-book trial is outside this
workflow.

## RE-06C local Gemini project budget gate

Each moderator adds a Gemini key in **Личный кабинет** and then explicitly
sets a conservative **local daily request cap** for that same Google project
(1–1,000,000 requests/day, normally much lower). This is NOT a Google quota
lookup, provider-verified entitlement, token-cost ceiling or financial budget.
The UI shows the locally counted used/remaining requests and always states that
other applications using the Google project are not measured here.

`PUT /v1/me/gemini-keys/{project_id}/daily-cap` with
`{"daily_request_cap": 21}` (for example) requires the owning moderator's
authenticated session and CSRF. The number should be explicitly chosen using
that moderator's actual project/model allowance, not guessed from the API key.
Changing the key for the same project does not silently raise its limit; deleting
the key deletes the associated cap.

The worker will **not make any paid Gemini calls** for an uncapped personal
project, including context preflight, translation and repair. A configured
cap is re-read under the same SQLite transaction that reserves each outgoing
request; quota exhaustion defers the existing job to Pacific midnight without
burning its normal finite retry attempts. Project/model accounting is durable
across restarts. A separately configured host limit can only tighten, never
relax, a moderator's cap.

A batch of 100 chapters is NOT assumed to cost only 100 API calls: context,
segmentation, retries and repairs may cost many more. The preview checks that
the moderator has at least one capped project, not that all chapters fit into
one day's remaining allowance. Any overflow stays queued and resumes under
the existing worker's Pacific-day handling. This does not grant unlimited
paid throughput and does not create another scheduler.

## Safety boundaries

- All source IDs/revisions, the book glossary version and the preview SHA are
  checked in one `BEGIN IMMEDIATE` transaction. An invalid or outdated member
  of the batch aborts the **entire** queue submission; no partial work.
- 1–100 unique source chapters, 16 MiB total selected UTF-8 source data.
- Snapshot jobs use current book source, glossary and existing transport. Jobs
  retain the original queue actor and exact source/glossary revisions. The worker
  already checks for staleness again before calling Gemini.
- Job scheduling remains **one ready chapter first across moderators**, based on
  existing ready-job counts; never chapter text length. The same per-project quota
  logic and per-moderator stored Gemini keys continue to apply. Members with no
  key do not trigger paid calls.
- `preview`, `submit`, `GET` progress, CI and smoke tests make **zero**
  Gemini requests. Returning batch status does not accept or publish RU text.
- Shared library permissions remain exactly administrator/moderator. Book owner
  is still tracked separately; no new per-book ACL is invented. Team auth and
  browser CSRF are mandatory through the common dependency.
- Batch metadata contains chapter IDs, revision numbers and digests, not full
  source, generated text, prompt artifacts or secrets.

## Remaining RE-06C work

Actual Google-side token allowance and billing awareness remain unverified;
optional whole-batch cancel, browser visual approval against Canva, real-provider
literary QA and production acceptance also remain.
CI's headless Chromium run proves only the isolated local workflow, not
billable throughput or readiness of permanent VPS services. Root bootstrap for persistent
translator systemd services is still blocked by chaptera-site issue #46.
