# Stable translation API contract — baseline v0.5 / import addition v0.6

This is a **server-only contract**. `full_cycle_app` and the Windows executable are unchanged. Tests in `tests/test_api_contract.py` preserve the pre-import behavior.

## Runtime and inputs

- Python 3.11 and 3.12; headless import without PyQt, Windows runtime or desktop SQLite.
- `GET /healthz` is the sole public endpoint; all `/v1/*` methods require the team member's `X-API-Key`. Missing or invalid keys return 401. Team members share one workspace.
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

**Not implemented in v0.6:** staged upload persistence, confirmation, manual chapter mapping, conflict resolution, ingestion to canonical SQLite, web UI, import of chapter texts from the metadata-only JSON or Rulate site. Those are the following milestones. Preview success must not be displayed as successful import.

Example for a trusted local developer instance:

```bash
curl -X POST http://127.0.0.1:8000/v1/imports/preview \
  -H 'X-API-Key: YOUR_TEAM_TOKEN' \
  -F 'file=@novel.epub'
```
