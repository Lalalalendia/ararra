"""RE-MVP: one real multipart TXT (1 or N chapters) -> separate Drive outbox.

No paid Gemini calls or real Google requests. Marking a job completed is
an explicit local worker *simulation*, not proof of production translation.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.drive_outbox import DriveOutbox
from chaptera_server.storage import QueueStore

HEADERS = {"X-API-Key": "moderator-secret"}


def txt(count: int) -> bytes:
    return ("\n\n".join(
        f"Глава {i}. Заголовок {i}\n"
        f"Текст исходной главы номер {i}. Текст достаточно длинный для проверки."
        for i in range(1, count + 1)
    ) + "\n").encode("utf-8")


@pytest.mark.parametrize("count", [1, 3])
def test_one_uploaded_file_becomes_exactly_n_separate_drive_handoffs(tmp_path, count):
    db = tmp_path / f"scenario-{count}.sqlite3"
    settings = Settings(
        db_path=db,
        team_tokens={"admin": "admin-secret", "moderator": "moderator-secret"},
        admin_member_id="admin",
    )
    store = QueueStore(db)
    app = create_app(settings, store)
    outbox = DriveOutbox(store)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            stage = await c.post(
                "/v1/imports/stages", headers=HEADERS,
                files={"file": ("novel.txt", txt(count), "text/plain")},
            )
            assert stage.status_code == 201, stage.text
            stage_data = stage.json()
            assert stage_data["preview"]["chapter_count"] == count
            book_id = f"mvp-{count}"
            decisions = [
                {
                    "sequence": i, "action": "create",
                    "chapter_id": f"c{i}", "chapter_index": i,
                    "chapter_path": f"chapters/{i}.xhtml",
                    "expected_revision": 0,
                }
                for i in range(1, count + 1)
            ]
            confirmed = await c.post(
                f"/v1/imports/stages/{stage_data['stage_id']}/confirm",
                headers=HEADERS,
                json={
                    "book_id": book_id, "book_mode": "create",
                    "title": "Тестовый роман", "owner_member_id": "moderator",
                    "prefix_policy": "reject", "chapters": decisions,
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["imported_count"] == count

            chapters = (await c.get(f"/v1/books/{book_id}/chapters",
                                    headers=HEADERS)).json()["chapters"]
            assert len(chapters) == count
            payload = {
                "expected_glossary_revision": 0,
                "chapters": [
                    {"chapter_id": row["chapter_id"],
                     "expected_source_revision": row["source_revision"]}
                    for row in chapters
                ],
                "source_language": "auto", "target_language": "Russian",
                "transport": "html_v0", "max_attempts": 3,
            }
            base = f"/v1/books/{book_id}/translation-batches"
            preview = await c.post(base + "/preview", headers=HEADERS, json=payload)
            assert preview.status_code == 200, preview.text
            assert preview.json()["chapter_count"] == count
            assert outbox.status()["enabled"] is False
            assert outbox.status()["pending"] == 0

            request = {**payload,
                       "expected_manifest_sha256": preview.json()["manifest_sha256"],
                       "drive_export_requested": True}
            submitted = await c.post(
                base, headers={**HEADERS, "Idempotency-Key": f"stage-{count}"},
                json=request,
            )
            assert submitted.status_code == 201, submitted.text
            result = submitted.json()
            assert result["drive_export_requested"] is True
            assert len(result["chapters"]) == count
            assert outbox.status()["pending"] == 0
            retry = await c.post(
                base, headers={**HEADERS, "Idempotency-Key": f"stage-{count}"},
                json=request,
            )
            assert retry.status_code == 201
            assert retry.json()["batch_id"] == result["batch_id"]

            # Fake worker success **only in isolated sqlite**; never contact Gemini.
            with store._connect() as con:
                for index, chapter in enumerate(result["chapters"], start=1):
                    con.execute(
                        "UPDATE jobs SET status='completed',result_html=?,updated_at=?"
                        " WHERE id=?",
                        (f"<p>Тестовый перевод главы {index}</p>",
                         time.time(), chapter["job_id"]),
                    )
            assert outbox.status()["pending"] == count
            assert outbox.run_once(
                lambda _: pytest.fail("Operator disabled: no Google requests"),
                consented_only=True,
            )["stage"] == "no_eligible_export"

            # Operator-enabled only in disposable SQLite. Fake receipts
            # model the separately verified real Drive exporter contract.
            outbox.set_enabled(True)
            ids = []
            for _ in range(count):
                exported = outbox.run_once(
                    lambda job_id: {
                        "source_file_id": "test-source-" + job_id,
                        "translation_file_id": "test-translation-" + job_id,
                    }, consented_only=True,
                )
                assert exported["stage"] == "exported"
                ids.append(exported["translation_file_id"])
            assert len(set(ids)) == count
            assert outbox.run_once(
                lambda _: pytest.fail("No repeated export"),
                consented_only=True,
            )["stage"] == "no_eligible_export"
            batch = (await c.get(
                f"{base}/{result['batch_id']}", headers=HEADERS
            )).json()
            assert batch["counts"]["completed"] == count
            assert batch["drive_counts"] == {"exported": count}
            assert [row["drive_translation_file_id"] for row
                    in batch["chapters"]] == ids
            assert [row["chapter_index"] for row in batch["chapters"]] == list(
                range(1, count + 1)
            )

    asyncio.run(scenario())
