"""RE-E2E-01: one synthetic book crosses the actual ReadEra product boundaries.

No external site access, Gemini billing, Google credentials, or production DB.
The Drive handoff intentionally exports the machine draft, not an accepted book.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.drive_export import DriveExportError, export_completed_job
from chaptera_server.storage import QueueStore
from chaptera_server.worker import TranslationWorker


BOOK = "https://www.royalroad.com/fiction/142007/aura"
TOC = """<div id="chapters">
<a href="/fiction/142007/aura/chapter/60001/1-first">1 - First</a>
<a href="/fiction/142007/aura/chapter/60002/2-second">2 - Second</a>
</div>"""
SOURCE = ("世界正在燃烧。", "天空正在下雨。")
MOD = {"X-API-Key": "test-moderator-key"}
ROOT = "syntheticReadEraLibraryRoot0001"


class OfflineSite:
    def __init__(self):
        self.urls = []

    async def fetch(self, source, url):
        assert source == "royalroad"
        self.urls.append(url)
        if url == BOOK:
            return TOC
        if "/60001/" in url:
            return '<div class="chapter-content"><p>' + SOURCE[0] * 24 + "</p></div>"
        if "/60002/" in url:
            return '<div class="chapter-content"><p>' + SOURCE[1] * 24 + "</p></div>"
        raise AssertionError("Unexpected website request")


class OfflineGemini:
    def __init__(self):
        self.requests = 0

    async def generate(self, request):
        if request.chapter_id.startswith("context:"):
            return json.dumps({"c0": {
                "summary": "Глава для теста.", "pov": "третье лицо",
                "tone_and_register": "нейтральный", "setting": [],
                "active_entities": [], "relationships": [],
                "important_events": [], "continuity_in": [],
                "continuity_out": [], "ambiguous_references": [],
                "reveal_constraints": [], "semantic_terms": [],
            }}, ensure_ascii=False)
        self.requests += 1
        return (request.source_html_with_markers
                .replace(SOURCE[0], "Мир горит.")
                .replace(SOURCE[1], "Небо плачет."))


class MemoryDrive:
    """Minimal create-only Drive boundary; never contacts Google's API."""

    def __init__(self):
        self.files = {
            ROOT: {"id": ROOT, "name": "Эрида",
                   "mimeType": "application/vnd.google-apps.folder",
                   "parents": ["root"], "appProperties": {}},
        }
        self.bodies = {}
        self.create_count = 0

    def _check_id(self, file_id):
        assert file_id in self.files or file_id.startswith("generatedReadEraFile")
        return file_id

    def check_destination(self, file_id):
        assert file_id == ROOT
        return self.files[file_id]

    def children(self, parent):
        return [dict(item) for item in self.files.values()
                if parent in item.get("parents", [])]

    def _create(self, parent, metadata, body=None):
        assert parent in self.files
        self.create_count += 1
        file_id = f"generatedReadEraFile{self.create_count:06d}"
        row = {**metadata, "id": file_id, "parents": [parent]}
        row.setdefault("appProperties", {})
        self.files[file_id] = row
        if body is not None:
            self.bodies[file_id] = body
        return dict(row)

    def create_metadata(self, parent, metadata):
        return self._create(parent, metadata)

    def upload_text(self, parent, metadata, payload):
        assert isinstance(payload, bytes) and payload
        return self._create(parent, metadata, body=payload)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    )


def test_book_link_to_batch_to_draft_qa_and_both_export_boundaries(tmp_path):
    db_path = tmp_path / "vertical.sqlite3"
    settings = Settings(
        db_path=db_path,
        admin_member_id="admin",
        team_tokens={"admin": "test-admin-key",
                     "moderator": "test-moderator-key"},
    )
    store = QueueStore(db_path)
    app = create_app(settings, store)
    site = OfflineSite()
    app.state.public_source_fetcher = site
    fake_model = OfflineGemini()
    drive = MemoryDrive()

    async def scenario():
        async with _client(app) as client:
            catalog_response = await client.post(
                "/v1/source-books/catalogs", headers=MOD,
                json={"source": "royalroad", "book_url": BOOK},
            )
            assert catalog_response.status_code == 201, catalog_response.text
            catalog = catalog_response.json()
            assert catalog["chapter_count"] == 2
            staged_response = await client.post(
                f"/v1/source-books/catalogs/{catalog['catalog_id']}/collect",
                headers=MOD,
                json={"ordinals": [1, 2],
                      "expected_catalog_sha256": catalog["catalog_sha256"]},
            )
            assert staged_response.status_code == 201, staged_response.text
            stage = staged_response.json()
            assert stage["status"] == "staged" and stage["stage_id"]
            assert len(site.urls) == 3

            confirmed = await client.post(
                f"/v1/imports/stages/{stage['stage_id']}/confirm",
                headers=MOD, json={
                    "book_id": "vertical-slice",
                    "book_mode": "create",
                    "title": "Синтетическая книга",
                    "owner_member_id": "moderator",
                    "prefix_policy": "reject",
                    "chapters": [
                        {"sequence": n, "action": "create",
                         "chapter_id": f"c{n}", "chapter_index": n,
                         "chapter_path": f"chapters/{n}.xhtml",
                         "expected_revision": 0}
                        for n in (1, 2)
                    ],
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["imported_count"] == 2

            selected = {
                "expected_glossary_revision": 0,
                "chapters": [
                    {"chapter_id": f"c{n}", "expected_source_revision": 1}
                    for n in (1, 2)
                ],
                "source_language": "Chinese",
                "target_language": "Russian",
                "transport": "html_v0",
                "max_attempts": 1,
            }
            batch_url = "/v1/books/vertical-slice/translation-batches"
            pre = await client.post(batch_url + "/preview",
                                    headers=MOD, json=selected)
            assert pre.status_code == 200, pre.text
            manifest = pre.json()["manifest_sha256"]
            with store._connect() as con:
                assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            submitted = await client.post(
                batch_url, headers={**MOD, "Idempotency-Key": "vertical-1"},
                json={**selected, "expected_manifest_sha256": manifest},
            )
            assert submitted.status_code == 201, submitted.text
            batch = submitted.json()
            ids = [x["job_id"] for x in batch["chapters"]]
            assert len(ids) == len(set(ids)) == 2

        # A different API and worker instance must resume the exact SQLite batch.
        restarted = create_app(settings, QueueStore(db_path))
        async with _client(restarted) as client:
            returned = await client.get(
                f"{batch_url}/{batch['batch_id']}", headers=MOD,
            )
            assert returned.status_code == 200
            assert [x["job_id"] for x in returned.json()["chapters"]] == ids
            replay = await client.post(
                batch_url, headers={**MOD, "Idempotency-Key": "vertical-1"},
                json={**selected, "expected_manifest_sha256": manifest},
            )
            assert replay.status_code == 201
            assert [x["job_id"] for x in replay.json()["chapters"]] == ids

            worker = TranslationWorker(QueueStore(db_path), fake_model)
            assert await worker.run_once()
            assert await worker.run_once()
            assert not await worker.run_once()
            assert fake_model.requests == 2

            states = [(await client.get(f"/v1/jobs/{jid}", headers=MOD)).json()
                      for jid in ids]
            assert all(job["status"] == "completed" for job in states), [
                {"id": j["id"], "status": j["status"], "error": j.get("error"),
                 "events": QueueStore(db_path).events(j["id"])} for j in states
            ]
            assert "Мир горит." in states[0]["translated_html"]
            assert "Небо плачет." in states[1]["translated_html"]

            # Completed Gemini drafts are NOT automatically human-accepted.
            before = await client.get(
                "/v1/books/vertical-slice/export/preview"
                "?expected_last_chapter=2", headers=MOD,
            )
            assert before.status_code == 200 and not before.json()["ready"]
            assert before.json()["manifest_sha256"] is None

            # The Drive handoff takes the machine draft BEFORE human acceptance.
            receipts = [
                export_completed_job(
                    QueueStore(db_path), drive, jid, root_folder_id=ROOT,
                )
                for jid in ids
            ]
            assert drive.create_count == 8  # 4 folders, 4 editable TXT files
            assert all(r["status"] == "exported_or_already_present"
                       for r in receipts)
            src_file = receipts[0]["source_file_id"]
            ru_file = receipts[0]["translation_file_id"]
            assert SOURCE[0] in drive.bodies[src_file].decode("utf-8")
            drive.bodies[src_file] = b"Editor revised source in Drive."
            drive.bodies[ru_file] = "Редактор изменил черновик.".encode("utf-8")
            again = export_completed_job(
                QueueStore(db_path), drive, ids[0], root_folder_id=ROOT,
            )
            assert again == receipts[0]
            assert drive.bodies[src_file] == b"Editor revised source in Drive."
            assert "Редактор изменил" in drive.bodies[ru_file].decode("utf-8")
            assert drive.create_count == 8

            for number, job in enumerate(states, 1):
                accepted = await client.put(
                    f"/v1/books/vertical-slice/chapters/c{number}"
                    "/accepted-translation", headers=MOD,
                    json={"translated_html": job["translated_html"],
                          "expected_revision": 0,
                          "expected_source_revision": 1},
                )
                assert accepted.status_code == 200, accepted.text
                qa = await client.post(
                    f"/v1/books/vertical-slice/chapters/c{number}/qa",
                    headers=MOD, json={
                        "expected_source_revision": 1,
                        "expected_accepted_revision": 1,
                        "expected_glossary_revision": 0,
                    },
                )
                assert qa.status_code == 200, qa.text
                assert qa.json()["diagnostic_only"] is True

            ready = await client.get(
                "/v1/books/vertical-slice/export/preview"
                "?expected_last_chapter=2", headers=MOD,
            )
            assert ready.status_code == 200 and ready.json()["ready"]
            proof = ready.json()["manifest_sha256"]
            result = await client.get(
                "/v1/books/vertical-slice/export",
                params={"expected_last_chapter": 2, "format": "txt",
                        "expected_manifest_sha256": proof},
                headers=MOD,
            )
            assert result.status_code == 200, result.text
            assert hashlib.sha256(result.content).hexdigest() == (
                result.headers["x-readera-content-sha256"]
            )
            assert result.text.count("Глава 1") == 1
            assert result.text.count("Глава 2") == 1
            assert "Мир горит." in result.text
            assert "Небо плачет." in result.text

            # Old job cannot be exported again after its immutable source moved.
            count_before = drive.create_count
            restarted.state.library.put_chapter(
                "vertical-slice", "c1", chapter_index=1,
                chapter_path="chapters/1.xhtml",
                source_html="<p>Совершенно новый исходник.</p>",
                expected_revision=1,
            )
            with pytest.raises(DriveExportError, match="changed"):
                export_completed_job(
                    QueueStore(db_path), drive, ids[0], root_folder_id=ROOT,
                )
            assert drive.create_count == count_before
            stale = await client.get(
                "/v1/books/vertical-slice/export",
                params={"expected_last_chapter": 2, "format": "txt",
                        "expected_manifest_sha256": proof},
                headers=MOD,
            )
            assert stale.status_code == 409
            assert "Небо плачет." not in stale.text

    asyncio.run(scenario())
