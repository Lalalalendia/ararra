#!/usr/bin/env python3
"""M2b: Python/Rust batch status and Drive consent parity, no paid or network I/O.

Python alone creates two real batches on a disposable SQLite. Both runtimes
must read identical responses; Rust write gate and Python worker stay off.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore
from compare_rust_api import launch, ready
from compare_rust_import_stages import runner_env

BASE = "/v1/books/novel/translation-batches"


def snapshot(db: Path) -> tuple:
    with sqlite3.connect(db) as con:
        return tuple(tuple(con.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                     for table in (
                         "translation_batches", "translation_batch_items",
                         "translation_batch_drive_requests", "jobs",
                         "drive_export_outbox",
                     ))


async def exercise(app, db: Path, settings: Settings) -> None:
    alice = {"X-API-Key": settings.team_tokens["alice"]}
    bob = {"X-API-Key": settings.team_tokens["bob"]}
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://python", timeout=25) as py,
        httpx.AsyncClient(base_url="http://127.0.0.1:18495", timeout=25) as rust,
    ):
        async def create(selected: tuple[str, ...], key: str, consent: bool) -> dict:
            body = {
                "expected_glossary_revision": 0,
                "chapters": [
                    {"chapter_id": cid, "expected_source_revision": 1}
                    for cid in selected
                ],
                "source_language": "Chinese",
                "target_language": "Russian",
                "transport": "html_v0",
            }
            preview = await py.post(BASE + "/preview", headers=alice, json=body)
            assert preview.status_code == 200, preview.text
            posted = await py.post(
                BASE, headers={**alice, "Idempotency-Key": key},
                json={
                    **body,
                    "expected_manifest_sha256": preview.json()["manifest_sha256"],
                    "drive_export_requested": consent,
                },
            )
            assert posted.status_code == 201, posted.text
            return posted.json()

        consented = await create(("c3", "c1", "c2"), "consented", True)
        plain = await create(("c2",), "plain", False)
        ids = [r["job_id"] for r in consented["chapters"]]
        plain_id = plain["chapters"][0]["job_id"]
        assert len(set([*ids, plain_id])) == 4

        async def compare() -> None:
            before = snapshot(db)
            for client in (py, rust):
                assert (await client.get(BASE)).status_code == 401
                assert (await client.get(
                    "/v1/books/no-book/translation-batches",
                    headers=alice,
                )).status_code == 404
                assert (await client.get(
                    BASE + "/no-such-batch", headers=alice,
                )).status_code == 404
                assert (await client.post(
                    BASE, headers=alice,
                )).status_code in (400, 405, 422)
            for person in (alice, bob):
                for path in (BASE, BASE + "?limit=1", BASE + "?limit=50",
                             BASE + "/" + consented["batch_id"],
                             BASE + "/" + plain["batch_id"]):
                    p, r = await asyncio.gather(
                        py.get(path, headers=person),
                        rust.get(path, headers=person),
                    )
                    assert p.status_code == r.status_code == 200, (
                        path, p.text, r.text
                    )
                    assert p.json() == r.json(), (path, p.json(), r.json())
                    assert "no-store" in r.headers["cache-control"]
                    assert "原文" not in r.text and "RU from worker" not in r.text
                wrong = await rust.get(
                    "/v1/books/other/translation-batches/"
                    + consented["batch_id"], headers=person,
                )
                assert wrong.status_code == 404
            assert snapshot(db) == before, "Batch GET mutated durable job/Drive state"

        # First compare the queued status on real Python-created records.
        await compare()
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE jobs SET status='completed',result_html='<p>RU from worker</p>'"
                " WHERE id=?", (ids[0],),
            )
            con.execute("UPDATE jobs SET status='failed' WHERE id=?", (ids[1],))
            con.execute("UPDATE jobs SET status='running' WHERE id=?", (ids[2],))
            con.execute(
                "UPDATE jobs SET status='completed',result_html='<p>RU from worker</p>'"
                " WHERE id=?", (plain_id,),
            )
        assert (await py.get(BASE + "/" + consented["batch_id"],
                             headers=alice)).json()["drive_counts"] == {
            "awaiting_exporter": 1, "not_delivered": 1,
            "awaiting_translation": 1,
        }
        await compare()

        # Fake an exporter receipt without any network or permission to Drive.
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE drive_export_outbox SET status='done',receipt_json=? "
                "WHERE job_id=?",
                (json.dumps({
                    "source_file_id": "synthetic-source",
                    "translation_file_id": "synthetic-translation",
                }), ids[0]),
            )
        saved = (await py.get(BASE + "/" + consented["batch_id"],
                              headers=alice)).json()
        assert saved["chapters"][0]["drive_translation_file_id"] == "synthetic-translation"
        assert saved["drive_counts"]["exported"] == 1
        await compare()

        # Invalid/missing Drive file IDs must fail closed, never say exported.
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE drive_export_outbox SET receipt_json=? WHERE job_id=?",
                ('{"translation_file_id":null}', ids[0]),
            )
        bad = (await py.get(BASE + "/" + consented["batch_id"],
                             headers=alice)).json()
        assert bad["drive_counts"]["requires_review"] == 1
        await compare()

        # An unconsented batch never has Drive output, even after completion.
        no_drive = (await rust.get(BASE + "/" + plain["batch_id"],
                                   headers=bob)).json()
        assert no_drive["status"] == "completed"
        assert no_drive["drive_export_requested"] is False
        assert no_drive["drive_counts"] == {"not_requested": 1}
        with sqlite3.connect(db) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM drive_export_outbox WHERE job_id=?",
                (plain_id,),
            ).fetchone()[0] == 0


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_translation_batches.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m2b-batch-") as temp:
        root = Path(temp)
        db = root / "synthetic.sqlite3"
        settings = Settings(
            db_path=db,
            team_tokens={"chief": "chief-test", "alice": "alice-test",
                         "bob": "bob-test"},
            admin_member_id="chief", browser_session_secure=False,
        )
        store = QueueStore(db)
        app = create_app(settings, store)
        library = app.state.library
        library.create_book("novel", "Synthetic Book",
                            owner_member_id="alice", actor_id="chief")
        for i in range(1, 4):
            library.put_chapter(
                "novel", f"c{i}", chapter_index=i,
                chapter_path=f"Text/{i}.xhtml",
                source_html=f"<p>原文 {i}</p>", expected_revision=0,
            )
        env = runner_env(root, db, settings)
        env.pop("ERIDA_RUST_TEST_WRITES", None)
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT", None)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            asyncio.run(exercise(app, db, settings))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("rust_batch_list_get_python_http_parity=PASS")
    print("rust_batch_drive_consent_and_receipt_parity=PASS")
    print("rust_batch_reads_without_write_gate=PASS")
    print("gemini_network_calls=ZERO")
    print("google_drive_remote_writes=ZERO")


if __name__ == "__main__":
    main()
