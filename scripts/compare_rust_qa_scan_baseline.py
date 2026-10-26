#!/usr/bin/env python3
"""M2e-1: native Rust CPU QA scan vs Python on a proven safe subset.

The Rust writer is *only* a disposable SQLite experiment. Python remains
authoritative for all glossary/complex HTML/translation diagnostics.
No API keys for paid models, external Drive writes, or deployed DBs.
"""
from __future__ import annotations

import asyncio
import copy
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

ROOT = "/v1/books/novel/chapters"
AUTH = {"X-API-Key": "m2e-alice"}
REVISIONS = {
    "expected_source_revision": 1,
    "expected_accepted_revision": 1,
    "expected_glossary_revision": 0,
}
SOURCE = "<p>中文 исходный документ.</p>"
SAMPLES = (
    ("plain", "<p>Это законченный перевод без китайских символов.</p>"),
    ("residue", "<p>Перевод содержит 汉字, テスト и 가나다.</p>"),
)


def db_snapshot(db: Path) -> tuple:
    with sqlite3.connect(db) as con:
        return tuple(
            tuple(con.execute(f"SELECT * FROM {table} ORDER BY rowid"))
            for table in (
                "books", "book_chapters", "book_accepted_revisions",
                "chapter_qa_reports", "chapter_qa_findings",
                "chapter_qa_review_events", "jobs",
            )
        )


def normalize(report: dict) -> dict:
    normalized = copy.deepcopy(report)
    normalized.pop("report_id", None)
    normalized.pop("created_at", None)
    for finding in normalized["findings"]:
        finding.pop("finding_id", None)
    return normalized


async def verify(app, db: Path) -> None:
    library = app.state.library
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://python", timeout=30,
        ) as py,
        httpx.AsyncClient(base_url="http://127.0.0.1:18495",
                          timeout=30) as rust,
    ):
        for label, target in SAMPLES:
            path_p = f"{ROOT}/{label}-py/qa"
            path_r = f"{ROOT}/{label}-rust/qa"

            before = db_snapshot(db)
            for client, path in ((py, path_p), (rust, path_r)):
                unauthorized = await client.post(path, json=REVISIONS)
                assert unauthorized.status_code == 401, unauthorized.text
                assert (await client.post(
                    f"{ROOT}/unknown/qa", headers=AUTH, json=REVISIONS,
                )).status_code == 404
                for invalid in (
                    {**REVISIONS, "expected_source_revision": 0},
                    {**REVISIONS, "expected_accepted_revision": 0},
                    {**REVISIONS, "expected_glossary_revision": -1},
                    {**REVISIONS, "unknown_field": 1},
                ):
                    res = await client.post(path, headers=AUTH, json=invalid)
                    assert res.status_code == 422, (path, res.status_code, res.text)
            assert before == db_snapshot(db), "Denied scan mutated chapters/reports"

            p = await py.post(path_p, headers=AUTH, json=REVISIONS)
            r = await rust.post(path_r, headers=AUTH, json=REVISIONS)
            assert (p.status_code, r.status_code) == (200, 200), (p.text, r.text)
            assert "no-store" in r.headers["cache-control"]
            assert normalize(p.json()) == normalize(r.json()), (
                label, normalize(p.json()), normalize(r.json())
            )
            assert r.json()["alignment_proven"] is False
            assert r.json()["diagnostic_only"] is True
            assert r.json()["source_hash"] == p.json()["source_hash"]
            assert r.json()["accepted_hash"] == p.json()["accepted_hash"]
            assert r.json()["glossary_hash"] == p.json()["glossary_hash"]
            if label == "plain":
                assert r.json()["finding_count"] == 0
                assert r.json()["findings"] == []
            else:
                findings = r.json()["findings"]
                assert len(findings) == 1
                assert findings[0]["code"] == "CJK_RESIDUE"
                assert findings[0]["severity"] == "high"
                assert findings[0]["would_block"] is False
                assert findings[0]["evidence"] == {
                    "sample": "汉字テスト가나다", "count": 8,
                }
            # A successful native scan is immediately visible from Python.
            readback = (await py.get(path_r, headers=AUTH)).json()
            assert readback["report"] == r.json()
            after = db_snapshot(db)
            repeated = await rust.post(path_r, headers=AUTH, json=REVISIONS)
            assert repeated.status_code == 200
            assert repeated.json() == r.json()
            replay_python = await py.post(path_r, headers=AUTH, json=REVISIONS)
            assert replay_python.status_code == 200
            assert replay_python.json() == r.json()
            assert db_snapshot(db) == after, "Idempotent replay created a duplicate"

        # Python and Rust race on one new, matching chapter: exactly ONE report.
        race = f"{ROOT}/plain-race/qa"
        result = await asyncio.gather(
            py.post(race, headers=AUTH, json=REVISIONS),
            rust.post(race, headers=AUTH, json=REVISIONS),
        )
        assert all(r.status_code == 200 for r in result), [
            (r.status_code, r.text) for r in result
        ]
        assert result[0].json() == result[1].json()
        with sqlite3.connect(db) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM chapter_qa_reports WHERE chapter_id='plain-race'"
            ).fetchone()[0] == 1

        # Missing/stale accepted version and stale CAS must not insert reports.
        no_accepted = f"{ROOT}/unaccepted/qa"
        old = db_snapshot(db)
        for client in (py, rust):
            bad = await client.post(no_accepted, headers=AUTH, json=REVISIONS)
            assert bad.status_code == 409, bad.text
            for invalid in (
                {**REVISIONS, "expected_source_revision": 2},
                {**REVISIONS, "expected_accepted_revision": 3},
                {**REVISIONS, "expected_glossary_revision": 4},
            ):
                bad = await client.post(
                    f"{ROOT}/plain-stale/qa", headers=AUTH, json=invalid
                )
                assert bad.status_code == 409, bad.text
        assert db_snapshot(db) == old

        # Glossary-dependent QA is still Python-owned; Rust must reject it.
        restricted = f"/v1/books/restricted/chapters/c1/qa"
        before = db_snapshot(db)
        denied = await rust.post(
            restricted, headers=AUTH,
            json={**REVISIONS, "expected_glossary_revision": 1},
        )
        assert denied.status_code == 400, denied.text
        assert db_snapshot(db) == before

        # Complex HTML is not silently treated as plain unformatted text.
        complex_html = f"{ROOT}/complex-html/qa"
        before = db_snapshot(db)
        denied = await rust.post(complex_html, headers=AUTH, json=REVISIONS)
        assert denied.status_code == 400, denied.text
        assert db_snapshot(db) == before
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert con.execute(
                "SELECT COUNT(*) FROM chapter_qa_reports"
            ).fetchone()[0] == 5, "two independent pairs + one raced chapter only"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_qa_scan_baseline.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m2e-qa-") as temp:
        root = Path(temp)
        db = root / "disposable.sqlite3"
        settings = Settings(
            db_path=db,
            team_tokens={"chief": "m2e-chief",
                         "alice": "m2e-alice", "bob": "m2e-bob"},
            admin_member_id="chief", browser_session_secure=False,
        )
        store = QueueStore(db)
        app = create_app(settings, store)
        library = app.state.library
        library.create_book("novel", "Native QA subset",
                            owner_member_id="alice", actor_id="chief")
        for label, target in SAMPLES:
            for variant in ("py", "rust"):
                chapter = f"{label}-{variant}"
                library.put_chapter(
                    "novel", chapter, chapter_index={
                        "plain-py": 1, "plain-rust": 2,
                        "residue-py": 3, "residue-rust": 4,
                    }[chapter], chapter_path=f"chapters/{chapter}.xhtml",
                    source_html=SOURCE, expected_revision=0,
                )
                library.accept_translation(
                    "novel", chapter, target, expected_revision=0,
                    expected_source_revision=1, actor_id="alice",
                )
        for i, chapter in enumerate(
            ("plain-race", "unaccepted", "plain-stale", "complex-html"),
            start=5,
        ):
            library.put_chapter(
                "novel", chapter, chapter_index=i,
                chapter_path=f"chapters/{chapter}.xhtml",
                source_html=SOURCE, expected_revision=0,
            )
            if chapter != "unaccepted":
                library.accept_translation(
                    "novel", chapter, "<p>Простой русский перевод.</p>",
                    expected_revision=0, expected_source_revision=1,
                    actor_id="alice",
                )
        # Unsupported parser input is introduced without changing source/rev.
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE book_chapters SET accepted_html=? "
                "WHERE book_id='novel' AND chapter_id='complex-html'",
                ("<p>Сложная <em>разметка</em> с вложенными тегами.</p>",),
            )
        library.create_book("restricted", "Glosssary-dependent QA",
                            owner_member_id="alice", actor_id="chief")
        library.put_chapter(
            "restricted", "c1", chapter_index=1,
            chapter_path="chapters/c1.xhtml",
            source_html=SOURCE, expected_revision=0,
        )
        library.put_glossary(
            "restricted", glossary={
                "中文": {"rus": "Китайский", "note": "строгий термин"},
            }, versions={}, authority="READY", expected_revision=0,
            actor_id="alice",
        )
        library.accept_translation(
            "restricted", "c1", "<p>Китайский пример.</p>",
            expected_revision=0, expected_source_revision=1, actor_id="alice",
        )
        env = runner_env(root, db, settings)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            asyncio.run(verify(app, db))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        # Absence of the explicit disposable gate removes the POST route.
        env.pop("ERIDA_RUST_TEST_WRITES", None)
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT", None)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            async def no_write():
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:18495", timeout=15,
                ) as rust:
                    assert (await rust.get(
                        f"{ROOT}/plain-rust/qa", headers=AUTH
                    )).status_code == 200
                    response = await rust.post(
                        f"{ROOT}/plain-rust/qa", headers=AUTH, json=REVISIONS,
                    )
                    assert response.status_code in (404, 405), response.text
            before = db_snapshot(db)
            asyncio.run(no_write())
            assert db_snapshot(db) == before
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("rust_m2e_baseline_python_qa_report_parity=PASS")
    print("rust_m2e_cpu_only_cjk_residue_gate=PASS")
    print("rust_m2e_unsupported_glossary_html_fail_closed=PASS")
    print("rust_m2e_same_chapter_race_single_report=PASS")
    print("rust_m2e_production_write_route=DISABLED")
    print("paid_gemini_requests=ZERO")
    print("google_drive_remote_writes=ZERO")


if __name__ == "__main__":
    main()
