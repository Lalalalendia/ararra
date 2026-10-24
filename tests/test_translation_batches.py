"""RE-06C batch queue admission is exact, atomic, restart-safe and offline."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


ADMIN = {"X-API-Key": "admin-secret"}
MOD = {"X-API-Key": "mod-secret"}
SECOND = {"X-API-Key": "second-secret"}
BOOK_URL = "/v1/books/novel/translation-batches"


def make_workspace(tmp_path):
    db = tmp_path / "batches.sqlite3"
    settings = Settings(
        db_path=db,
        team_tokens={"admin": "admin-secret", "moderator": "mod-secret",
                     "second": "second-secret"},
        admin_member_id="admin",
    )
    store = QueueStore(db)
    app = create_app(settings, store)
    library = app.state.library
    library.create_book("novel", "Selected chapters",
                        owner_member_id="moderator", actor_id="admin")
    for i in range(1, 4):
        library.put_chapter(
            "novel", f"c{i}", chapter_index=i,
            chapter_path=f"Text/{i}.xhtml",
            source_html=f"<p>原文 {i}</p>", expected_revision=0,
        )
    return app, settings, store, library


def selected(*chapter_ids):
    return {
        "source_language": "Chinese",
        "target_language": "Russian",
        "transport": "html_v0",
        "expected_glossary_revision": 0,
        "chapters": [
            {"chapter_id": cid, "expected_source_revision": 1}
            for cid in chapter_ids
        ],
    }


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


async def preview(c, body, headers=MOD):
    return await c.post(BOOK_URL + "/preview", json=body, headers=headers)


async def submit(c, body, receipt, key, headers=MOD):
    return await c.post(BOOK_URL,
                        json={**body, "expected_manifest_sha256": receipt},
                        headers={**headers, "Idempotency-Key": key})


def job_count(store):
    with store._connect() as con:
        return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def test_preview_is_read_only_requires_team_and_checks_source_versions(tmp_path):
    app, _, store, lib = make_workspace(tmp_path)

    async def scenario():
        async with client_for(app) as c:
            body = selected("c3", "c1")
            assert (await preview(c, body, {})).status_code == 401
            result = await preview(c, body)
            assert result.status_code == 200, result.text
            info = result.json()
            assert [x["chapter_id"] for x in info["chapters"]] == ["c1", "c3"]
            assert info["chapter_count"] == 2
            assert len(info["manifest_sha256"]) == 64
            assert "原文" not in result.text
            assert job_count(store) == 0
            duplicate = await preview(c, selected("c1", "c1"))
            assert duplicate.status_code == 400
            missing = await preview(c, selected("c1", "deleted"))
            assert missing.status_code == 409
            stale = await preview(c, {
                **selected("c1"), "expected_glossary_revision": 1,
            })
            assert stale.status_code == 409
            version = await preview(c, {
                **selected("c1"),
                "chapters": [{"chapter_id": "c1", "expected_source_revision": 7}],
            })
            assert version.status_code == 409
            assert (await c.get(BOOK_URL, headers=MOD)).json()["batches"] == []

    asyncio.run(scenario())
    assert job_count(store) == 0


def test_submit_is_atomic_ordered_explicit_and_persists_after_restart(tmp_path):
    app, settings, store, _ = make_workspace(tmp_path)
    payload = selected("c3", "c1")

    async def scenario():
        async with client_for(app) as c:
            receipt = (await preview(c, payload)).json()["manifest_sha256"]
            no_key = await c.post(
                BOOK_URL, json={**payload, "expected_manifest_sha256": receipt},
                headers=MOD,
            )
            assert no_key.status_code == 400
            bad_hash = await submit(c, payload, "0" * 64, "batch-1")
            assert bad_hash.status_code == 409
            assert job_count(store) == 0

            posted = await submit(c, payload, receipt, "batch-1")
            assert posted.status_code == 201, posted.text
            saved = posted.json()
            assert saved["status"] == "active"
            assert saved["counts"]["queued"] == 2
            assert [x["chapter_id"] for x in saved["chapters"]] == ["c1", "c3"]
            assert [x["chapter_index"] for x in saved["chapters"]] == [1, 3]
            assert len({x["job_id"] for x in saved["chapters"]}) == 2
            assert "原文" not in posted.text
            assert job_count(store) == 2
            with store._connect() as con:
                specs = [
                    json.loads(row["input_json"])
                    for row in con.execute("SELECT input_json FROM jobs ORDER BY rowid")
                ]
            assert [(spec["chapter_id"], spec["book_chapter_revision"])
                    for spec in specs] == [("c1", 1), ("c3", 1)]
            assert all(spec["book_glossary_revision"] == 0 for spec in specs)
            assert all(spec["target_language"] == "Russian" for spec in specs)

            # An identical retry returns the original jobs, not two more jobs.
            replay = await submit(c, payload, receipt, "batch-1")
            assert replay.status_code == 201
            assert replay.json()["batch_id"] == saved["batch_id"]
            assert job_count(store) == 2

            conflict = await submit(c, selected("c1"), receipt, "batch-1")
            assert conflict.status_code == 409
            assert job_count(store) == 2
            batches = await c.get(BOOK_URL, headers=SECOND)
            assert batches.status_code == 200
            assert len(batches.json()["batches"]) == 1
            wrong_book = await c.get(
                f"/v1/books/other/translation-batches/{saved['batch_id']}", headers=MOD,
            )
            assert wrong_book.status_code == 404
            unauthenticated = await c.get(
                f"{BOOK_URL}/{saved['batch_id']}",
            )
            assert unauthenticated.status_code == 401

            # The grouped jobs survive a process restart with the same SQLite.
            restarted = create_app(settings, QueueStore(store.db_path))
            async with client_for(restarted) as second:
                fetched = await second.get(
                    f"{BOOK_URL}/{saved['batch_id']}", headers=ADMIN,
                )
                assert fetched.status_code == 200
                assert fetched.json()["chapters"] == saved["chapters"]
                repeated = await submit(second, payload, receipt, "batch-1")
                assert repeated.json()["batch_id"] == saved["batch_id"]
                assert job_count(store) == 2

    asyncio.run(scenario())


def test_edit_between_preview_and_submit_refuses_partial_enqueue(tmp_path):
    app, _, store, lib = make_workspace(tmp_path)

    async def scenario():
        async with client_for(app) as c:
            body = selected("c1", "c2", "c3")
            receipt = (await preview(c, body)).json()["manifest_sha256"]
            lib.put_chapter(
                "novel", "c2", chapter_index=2, chapter_path="Text/2.xhtml",
                source_html="<p>Изменённый исходник</p>", expected_revision=1,
            )
            stale = await submit(c, body, receipt, "stale-revision")
            assert stale.status_code == 409
            assert job_count(store) == 0
            assert (await c.get(BOOK_URL, headers=MOD)).json()["batches"] == []

            # A new preview with current revision creates all three or none.
            updated = {
                **body,
                "chapters": [
                    {"chapter_id": f"c{i}",
                     "expected_source_revision": 2 if i == 2 else 1}
                    for i in range(1, 4)
                ],
            }
            new_receipt = (await preview(c, updated)).json()["manifest_sha256"]
            ok = await submit(c, updated, new_receipt, "stale-revision")
            assert ok.status_code == 201, ok.text
            assert job_count(store) == 3

    asyncio.run(scenario())


def test_progress_tracks_original_jobs_without_extra_worker_or_model_calls(tmp_path):
    app, _, store, _ = make_workspace(tmp_path)

    async def scenario():
        async with client_for(app) as c:
            body = selected("c1", "c2")
            receipt = (await preview(c, body)).json()["manifest_sha256"]
            saved = (await submit(c, body, receipt, "progress")).json()
            bid = saved["batch_id"]
            first, lease = store.claim_next()
            assert first["id"] == saved["chapters"][0]["job_id"]
            running = (await c.get(f"{BOOK_URL}/{bid}", headers=MOD)).json()
            assert running["counts"]["running"] == 1
            assert store.complete(first["id"], lease, "<p>RU</p>", "hash", 1)
            other = store.request_cancel(saved["chapters"][1]["job_id"])
            assert other["status"] == "cancelled"
            finished = (await c.get(f"{BOOK_URL}/{bid}", headers=MOD)).json()
            assert finished["status"] == "finished_with_errors"
            assert finished["counts"]["completed"] == 1
            assert finished["counts"]["cancelled"] == 1
            assert "RU" not in json.dumps(finished)
            assert store.claim_next() is None

    asyncio.run(scenario())


def test_separate_members_can_submit_same_book_without_per_book_acl(tmp_path):
    app, _, store, _ = make_workspace(tmp_path)

    async def scenario():
        async with client_for(app) as c:
            body = selected("c1")
            receipt = (await preview(c, body, ADMIN)).json()["manifest_sha256"]
            a = (await submit(c, body, receipt, "key", MOD)).json()
            b = (await submit(c, body, receipt, "key", SECOND)).json()
            assert a["batch_id"] != b["batch_id"]
            assert a["actor_id"] == "moderator"
            assert b["actor_id"] == "second"
            assert job_count(store) == 2

    asyncio.run(scenario())



def test_unexpected_second_insert_failure_rolls_back_every_job(tmp_path, monkeypatch):
    app, _, store, _ = make_workspace(tmp_path)
    batches = app.state.translation_batches
    body = {**selected("c1", "c2"), "style_instructions": "", "max_attempts": 3}
    receipt = batches.preview("novel", body)["manifest_sha256"]
    original = QueueStore._enqueue_in_transaction
    calls = [0]

    def fail_second(con, actor_id, spec, now, *, idempotency_key=None):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("forced second insert failure")
        return original(con, actor_id, spec, now, idempotency_key=idempotency_key)

    monkeypatch.setattr(QueueStore, "_enqueue_in_transaction", staticmethod(fail_second))
    with pytest.raises(RuntimeError, match="second insert"):
        batches.submit(
            "novel", {**body, "expected_manifest_sha256": receipt},
            actor_id="moderator", idempotency_key="atomic-rollback",
        )
    assert calls[0] == 2
    assert job_count(store) == 0
    with store._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM translation_batches").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM translation_batch_items").fetchone()[0] == 0

def test_invalid_chapter_count_rejected_before_database_write(tmp_path):
    app, _, store, _ = make_workspace(tmp_path)

    async def scenario():
        async with client_for(app) as c:
            too_many = selected(*(f"c{i}" for i in range(101)))
            assert (await preview(c, too_many)).status_code == 422
            assert (await preview(c, selected())).status_code == 422
            invalid = await preview(c, {
                **selected("c1"), "unexpected_unsafe_field": "x",
            })
            assert invalid.status_code == 422
            assert job_count(store) == 0

    asyncio.run(scenario())


def test_consented_chapter_batch_stages_individual_drive_files_without_network(tmp_path):
    """One confirmed 2-chapter batch yields exactly 2 durable handoffs."""
    from chaptera_server.drive_outbox import DriveOutbox
    import time

    app, _, store, _ = make_workspace(tmp_path)
    consented = {**selected("c2", "c1"), "drive_export_requested": True}
    other = selected("c3")

    async def scenario():
        async with client_for(app) as c:
            preview_receipt = (await preview(c, selected("c2", "c1"))).json()
            assert preview_receipt["chapter_count"] == 2
            assert job_count(store) == 0

            posted = await submit(
                c, consented, preview_receipt["manifest_sha256"], "drive-batch-1"
            )
            assert posted.status_code == 201, posted.text
            batch = posted.json()
            assert batch["drive_export_requested"] is True
            assert batch["drive_counts"] == {"awaiting_translation": 2}
            assert len({row["job_id"] for row in batch["chapters"]}) == 2

            # Identical ACK-loss retry preserves the same 2 jobs.
            retry = await submit(c, consented, preview_receipt["manifest_sha256"],
                                 "drive-batch-1")
            assert retry.status_code == 201
            assert retry.json()["batch_id"] == batch["batch_id"]
            changed = await submit(c, selected("c2", "c1"),
                                   preview_receipt["manifest_sha256"], "drive-batch-1")
            assert changed.status_code == 409
            assert job_count(store) == 2

            # Another moderator can submit ordinary jobs but cannot authorize
            # remote export under a different book owner's folder.
            second = await submit(c, consented, preview_receipt["manifest_sha256"],
                                  "drive-wrong-owner", headers=SECOND)
            assert second.status_code == 409
            assert job_count(store) == 2

            other_preview = (await preview(c, other)).json()
            no_drive = await submit(c, other, other_preview["manifest_sha256"],
                                    "without-drive")
            assert no_drive.status_code == 201
            assert no_drive.json()["drive_export_requested"] is False

            with store._connect() as con:
                for chapter in batch["chapters"]:
                    con.execute(
                        "UPDATE jobs SET status='completed',result_html=?,updated_at=? "
                        "WHERE id=?", ("<p>Готовый русский перевод</p>",
                                      time.time(), chapter["job_id"])
                    )
                con.execute(
                    "UPDATE jobs SET status='completed',result_html=?,updated_at=? "
                    "WHERE id=?", ("<p>Без разрешения на экспорт</p>",
                                  time.time(), no_drive.json()["chapters"][0]["job_id"])
                )
            outbox = DriveOutbox(store)
            assert outbox.status()["enabled"] is False
            assert outbox.status()["pending"] == 2
            assert outbox.run_once(lambda _id: pytest.fail("Drive must stay offline"))[
                "stage"] == "no_eligible_export"
            status = await c.get(
                f"{BOOK_URL}/{batch['batch_id']}", headers=MOD
            )
            assert status.status_code == 200
            details = status.json()
            assert details["counts"]["completed"] == 2
            assert details["drive_counts"] == {"awaiting_exporter": 2}
            assert all(row["drive_status"] == "awaiting_exporter"
                       for row in details["chapters"])

            # Simulate operator-approved exporter on an isolated temp DB,
            # using fake file receipts, NOT a Google network request.
            outbox.set_enabled(True)
            # An existing non-consented pending row can come from the
            # legacy global mode. New MVP exporter must never claim it.
            unconsented_id=no_drive.json()["chapters"][0]["job_id"]
            with store._connect() as con:
                con.execute(
                    "INSERT INTO drive_export_outbox"
                    "(job_id,status,run_after,created_at,updated_at)"
                    " VALUES(?,'pending',?,?,?)",
                    (unconsented_id,time.time(),time.time(),time.time()),
                )
            for _ in range(2):
                result = outbox.run_once(
                    lambda jid: {
                        "source_file_id": "offline-source-" + jid,
                        "translation_file_id": "offline-translation-" + jid,
                    },consented_only=True
                )
                assert result["stage"] == "exported"
                assert result["job_id"] != unconsented_id
            assert outbox.status()["done"] == 2
            assert outbox.status()["pending"] == 1
            assert outbox.run_once(
                lambda _: pytest.fail("Unconsented job must not leave the queue"),
                consented_only=True
            )["stage"] == "no_eligible_export"
            done = (await c.get(f"{BOOK_URL}/{batch['batch_id']}",
                                headers=MOD)).json()
            assert done["drive_counts"] == {"exported": 2}
            assert len({row["drive_translation_file_id"] for row
                        in done["chapters"]}) == 2
            assert (await c.get(
                f"{BOOK_URL}/{no_drive.json()['batch_id']}", headers=MOD
            )).json()["chapters"][0]["drive_status"] == "not_requested"

    asyncio.run(scenario())
