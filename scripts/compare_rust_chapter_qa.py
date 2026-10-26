#!/usr/bin/env python3
"""M2c: exact ChapterQaStore.current() Python/Rust parity on disposable SQLite.

Python alone creates and reviews diagnostics. Rust gets only SELECT privileges.
No Gemini, Drive, source-site fetch, or production database is ever used.
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

BASE = "/v1/books/novel/chapters/c1/qa"
ALICE = {"X-API-Key": "qa-alice"}
BOB = {"X-API-Key": "qa-bob"}


def snapshot(db: Path) -> tuple:
    with sqlite3.connect(db) as con:
        return tuple(
            tuple(con.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in (
                "books", "book_chapters", "chapter_qa_reports",
                "chapter_qa_findings", "chapter_qa_review_events",
            )
        )


async def exercise(app, store: QueueStore) -> None:
    lib = app.state.library
    qa = app.state.chapter_qa
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://python", timeout=30,
        ) as py,
        httpx.AsyncClient(base_url="http://127.0.0.1:18495", timeout=30) as rust,
    ):
        async def match(status: int = 200):
            before = snapshot(store.db_path)
            for client in (py, rust):
                assert (await client.get(BASE)).status_code == 401
                assert (await client.get(
                    "/v1/books/unknown/chapters/c1/qa",
                    headers=ALICE,
                )).status_code == 404
                assert (await client.get(
                    "/v1/books/novel/chapters/missing/qa",
                    headers=BOB,
                )).status_code == 404
            assert (await rust.post(BASE, headers=ALICE)).status_code in (404, 405)
            for user in (ALICE, BOB):
                p, r = await asyncio.gather(
                    py.get(BASE, headers=user), rust.get(BASE, headers=user),
                )
                assert p.status_code == r.status_code == status, (p.text, r.text)
                assert "no-store" in r.headers["cache-control"]
                assert p.json() == r.json(), (p.json(), r.json())
                if status == 200:
                    assert "source_html" not in r.json()
                    assert "accepted_html" not in r.json()
                    assert "alignment_proven" not in r.json()
            assert snapshot(store.db_path) == before, "GET changed QA/accepted records"

        # No accepted translation, no diagnostic, exact hash of empty text.
        await match()
        initial = (await py.get(BASE, headers=ALICE)).json()
        assert initial["accepted_current"] is False
        assert initial["accepted_hash"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert initial["report"] is None

        # After acceptance the read is current, but Rust does not invent a QA report.
        lib.accept_translation(
            "novel", "c1", "<p>Коул Ланселот оставил запись.</p>",
            expected_revision=0, expected_source_revision=1, actor_id="alice",
        )
        await match()
        assert (await py.get(BASE, headers=ALICE)).json()["report"] is None
        expected = {
            "expected_source_revision": 1,
            "expected_accepted_revision": 1,
            "expected_glossary_revision": 1,
        }
        scan = await py.post(BASE, headers=ALICE, json=expected)
        assert scan.status_code == 200, scan.text
        assert scan.json()["finding_count"] >= 1
        await match()
        result = (await rust.get(BASE, headers=BOB)).json()
        finding = result["report"]["findings"][0]
        assert result["report"]["alignment_proven"] is False
        assert result["report"]["diagnostic_only"] is True

        # Human review has its own CAS; the GET must preserve the exact revision.
        reviewed = qa.review(
            "novel", "c1", finding["finding_id"],
            status="waived", expected_review_revision=1,
            note="Проверено вручную", actor_id="bob",
            expected_source_revision=1, expected_accepted_revision=1,
            expected_glossary_revision=1,
        )
        assert reviewed["findings"][0]["review_status"] == "waived"
        await match()
        assert (await rust.get(BASE, headers=BOB)).json()["report"]["findings"][0][
            "review_note"
        ] == "Проверено вручную"

        # Tampered stored report fingerprints must return 409, not stale success.
        with store._connect() as con:
            con.execute(
                "UPDATE chapter_qa_reports SET glossary_hash='tampered' WHERE id=?",
                (scan.json()["report_id"],),
            )
        await match(status=409)
        with store._connect() as con:
            con.execute(
                "UPDATE chapter_qa_reports SET glossary_hash=? WHERE id=?",
                (initial["glossary_hash"], scan.json()["report_id"]),
            )
        await match()

        # Accepted revision changed: old manual review is retained but not current.
        lib.accept_translation(
            "novel", "c1", "<p>Старик Ланселот оставил запись.</p>",
            expected_revision=1, expected_source_revision=1, actor_id="alice",
        )
        await match()
        newer = (await rust.get(BASE, headers=ALICE)).json()
        assert newer["accepted_revision"] == 2
        assert newer["report"] is None

        # Source changes invalidate accepted translation; no fabricated QA.
        lib.put_chapter(
            "novel", "c1", chapter_index=1, chapter_path="chapters/c1.xhtml",
            source_html="<p>老蘭斯洛 оставил новый текст.</p>",
            expected_revision=1,
        )
        await match()
        stale = (await rust.get(BASE, headers=ALICE)).json()
        assert stale["accepted_current"] is False
        assert stale["report"] is None
        assert stale["source_revision"] == 2
        with store._connect() as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_chapter_qa.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m2c-qa-") as tmp:
        root = Path(tmp)
        db = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db, team_tokens={
                "chief": "qa-chief", "alice": "qa-alice", "bob": "qa-bob",
            }, admin_member_id="chief", browser_session_secure=False,
        )
        store = QueueStore(db)
        app = create_app(settings, store)
        lib = app.state.library
        lib.create_book("novel", "QA synthetic", owner_member_id="alice",
                        actor_id="chief")
        lib.put_chapter(
            "novel", "c1", chapter_index=1, chapter_path="chapters/c1.xhtml",
            source_html="<p>老蘭斯洛留下了文字。</p>", expected_revision=0,
        )
        lib.put_glossary(
            "novel", glossary={
                "老蘭斯洛": {"rus": "Старик Ланселот", "note": "Персонаж"},
                "考爾·蘭斯洛": {"rus": "Коул Ланселот", "note": "Персонаж"},
            }, versions={}, authority="READY", expected_revision=0,
            actor_id="alice",
        )
        env = runner_env(root, db, settings)
        env.pop("ERIDA_RUST_TEST_WRITES", None)
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT", None)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            asyncio.run(exercise(app, store))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("rust_chapter_qa_snapshot_and_report_parity=PASS")
    print("rust_chapter_qa_review_and_staleness_parity=PASS")
    print("rust_chapter_qa_integrity_drift_fail_closed=PASS")
    print("rust_chapter_qa_without_write_gate=PASS")
    print("production_sqlite_access=NO")
    print("gemini_and_drive_network_calls=ZERO")


if __name__ == "__main__":
    main()
