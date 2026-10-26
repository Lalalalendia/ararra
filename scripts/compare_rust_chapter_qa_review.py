#!/usr/bin/env python3
"""M2d: Python/Rust reviewed-QA CAS parity on one disposable SQLite.

Only the reference Python API scans diagnostics. The Rust PATCH is gated by
ERIDA_RUST_TEST_WRITES and ERIDA_RUST_DISPOSABLE_ROOT. No paid or remote I/O.
"""
from __future__ import annotations

import asyncio
import os
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
ALICE = {"X-API-Key": "m2d-alice"}
BOB = {"X-API-Key": "m2d-bob"}
VERSION = {
    "expected_source_revision": 1,
    "expected_accepted_revision": 1,
    "expected_glossary_revision": 1,
}


def rows(db: Path) -> tuple:
    """Prove denied operations do not mutate any text or QA audit records."""
    with sqlite3.connect(db) as con:
        return tuple(tuple(con.execute(f"SELECT * FROM {name} ORDER BY rowid"))
                     for name in (
                         "books", "book_chapters", "book_accepted_revisions",
                         "chapter_qa_reports", "chapter_qa_findings",
                         "chapter_qa_review_events", "jobs",
                     ))


def request(status: str, revision: int, note: str = "", **override) -> dict:
    return {
        "status": status,
        "expected_review_revision": revision,
        "note": note,
        **VERSION,
        **override,
    }


async def compare_denied(
    py: httpx.AsyncClient, rust: httpx.AsyncClient,
    db: Path, path: str, payload: dict, status: int,
    *, headers: dict = ALICE,
) -> None:
    before = rows(db)
    p = await py.patch(path, headers=headers, json=payload)
    r = await rust.patch(path, headers=headers, json=payload)
    assert (p.status_code, r.status_code) == (status, status), (
        path, status, p.status_code, r.status_code, p.text, r.text
    )
    # Axum JSON schema rejections are 422 before the private response wrapper.
    if status != 422:
        assert "no-store" in r.headers.get("cache-control", "")
    assert rows(db) == before, "Rejected review left a partial mutation"


async def exercise(app, db: Path) -> None:
    lib = app.state.library
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://python", timeout=30) as py,
        httpx.AsyncClient(base_url="http://127.0.0.1:18495", timeout=30) as rust,
    ):
        # Real QA scan is Python-owned. No Rust QA scan or network calls.
        lib.accept_translation(
            "novel", "c1", "<p>Коул Ланселот оставил запись.</p>",
            expected_revision=0, expected_source_revision=1, actor_id="alice",
        )
        scanned = await py.post(BASE, headers=ALICE, json=VERSION)
        assert scanned.status_code == 200, scanned.text
        findings = scanned.json()["findings"]
        assert len(findings) >= 1
        finding_id = findings[0]["finding_id"]
        path = BASE + "/findings/" + finding_id
        assert (await rust.get(BASE, headers=BOB)).json()["report"] == scanned.json()
        assert (await rust.patch(path, json=request("reviewed", 1))).status_code == 401

        # Strict request schema, no invalid or unaudited state changes.
        for candidate, status in (
            (request("unsupported", 1), 422),
            (request("reviewed", 0), 422),
            (request("reviewed", 1, "X" * 1001), 422),
            (request("reviewed", 1, unexpected="field"), 422),
            (request("waived", 1, "  "), 400),
            (request("reviewed", 1, expected_source_revision=5), 409),
            (request("reviewed", 1, expected_accepted_revision=4), 409),
            (request("reviewed", 1, expected_glossary_revision=4), 409),
            (request("reviewed", 3), 409),
        ):
            await compare_denied(py, rust, db, path, candidate, status)
        await compare_denied(
            py, rust, db, BASE + "/findings/nonexistent",
            request("reviewed", 1), 404,
        )
        await compare_denied(
            py, rust, db,
            "/v1/books/no-book/chapters/c1/qa/findings/" + finding_id,
            request("reviewed", 1), 404,
        )
        await compare_denied(
            py, rust, db,
            "/v1/books/novel/chapters/missing/qa/findings/" + finding_id,
            request("reviewed", 1), 404,
        )

        # Successful Python write is immediately visible to Rust and its audit.
        result = await py.patch(
            path, headers=ALICE, json=request("reviewed", 1, "Проверено"),
        )
        assert result.status_code == 200, result.text
        assert (await rust.get(BASE, headers=BOB)).json()["report"] == result.json()
        assert result.json()["findings"][0]["review_revision"] == 2
        await compare_denied(
            py, rust, db, path, request("reviewed", 1, "replayed"), 409,
        )

        # Rust success must be byte/JSON-identical to Python's current report.
        result = await rust.patch(
            path, headers=BOB, json=request("waived", 2, "  Осознанная форма.  "),
        )
        assert result.status_code == 200, result.text
        assert result.json()["findings"][0]["review_note"] == "Осознанная форма."
        assert result.json()["findings"][0]["reviewer_id"] == "bob"
        assert result.json() == (await py.get(BASE, headers=ALICE)).json()["report"]
        assert result.json()["findings"][0]["review_revision"] == 3
        assert result.json()["alignment_proven"] is False

        # Concurrent actors on the same SQLite revision: exactly one commit.
        responses = await asyncio.gather(
            py.patch(path, headers=ALICE,
                     json=request("open", 3, "Снято")),
            rust.patch(path, headers=BOB,
                       json=request("reviewed", 3, "Повторно просмотрено")),
        )
        assert sorted(r.status_code for r in responses) == [200, 409], [
            (r.status_code, r.text) for r in responses
        ]
        latest = (await py.get(BASE, headers=ALICE)).json()["report"]
        assert latest == (await rust.get(BASE, headers=BOB)).json()["report"]
        assert latest["findings"][0]["review_revision"] == 4

        with sqlite3.connect(db) as con:
            history = con.execute(
                "SELECT review_revision,from_status,to_status,actor_id,review_note "
                "FROM chapter_qa_review_events WHERE finding_id=? "
                "ORDER BY review_revision", (finding_id,),
            ).fetchall()
            changed = con.execute(
                "SELECT source_html,accepted_html FROM book_chapters "
                "WHERE book_id='novel' AND chapter_id='c1'",
            ).fetchone()
        assert len(history) == 3, history
        assert [row[0] for row in history] == [2, 3, 4], history
        assert [row[1] for row in history] == ["open", "reviewed", "waived"]
        assert history[1][2:] == ("waived", "bob", "Осознанная форма.")
        assert changed == (
            "<p>老蘭斯洛留下了文字。</p>",
            "<p>Коул Ланселот оставил запись.</p>",
        )

        # A tampered historical report must not authorize further review.
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE chapter_qa_reports SET source_hash='tampered'"
                " WHERE id=?", (scanned.json()["report_id"],),
            )
        await compare_denied(
            py, rust, db, path,
            request("reviewed", 4, "tampered"), 409,
        )
        with sqlite3.connect(db) as con:
            con.execute(
                "UPDATE chapter_qa_reports SET source_hash=? WHERE id=?",
                (scanned.json()["source_hash"], scanned.json()["report_id"]),
            )

        # A subsequent accepted-source revision fences the old QA review.
        lib.accept_translation(
            "novel", "c1", "<p>Старик Ланселот оставил запись.</p>",
            expected_revision=1, expected_source_revision=1, actor_id="alice",
        )
        await compare_denied(
            py, rust, db, path,
            request("reviewed", 4, "stale accepted"), 409,
        )
        lib.put_chapter(
            "novel", "c1", chapter_index=1, chapter_path="chapters/c1.xhtml",
            source_html="<p>老蘭斯洛 другое содержание.</p>",
            expected_revision=1,
        )
        await compare_denied(
            py, rust, db, path,
            request("reviewed", 4, "stale source"), 409,
        )
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_chapter_qa_review.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m2d-review-") as tmp:
        root = Path(tmp)
        db = root / "synthetic.sqlite3"
        settings = Settings(
            db_path=db, team_tokens={
                "chief": "m2d-chief", "alice": "m2d-alice", "bob": "m2d-bob",
            }, admin_member_id="chief", browser_session_secure=False,
        )
        store = QueueStore(db)
        app = create_app(settings, store)
        lib = app.state.library
        lib.create_book("novel", "QA acceptance", owner_member_id="alice",
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
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            asyncio.run(exercise(app, db))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        # Without the disposable gate a real-server Rust API offers GET only.
        env.pop("ERIDA_RUST_TEST_WRITES", None)
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT", None)
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18495", process)
            async def no_write():
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:18495",
                ) as rust:
                    assert (await rust.get(BASE, headers=ALICE)).status_code == 200
                    response = await rust.patch(
                        BASE + "/findings/anything", headers=ALICE,
                        json=request("reviewed", 4, "No writer"),
                    )
                    assert response.status_code in (404, 405), response.text
            before = rows(db)
            asyncio.run(no_write())
            assert rows(db) == before
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("rust_qa_review_cross_runtime_cas=PASS")
    print("rust_qa_review_immutable_audit=PASS")
    print("rust_qa_review_stale_text_and_hash_guard=PASS")
    print("rust_qa_review_without_write_gate=DENIED")
    print("gemini_network_calls=ZERO")
    print("google_drive_remote_writes=ZERO")


if __name__ == "__main__":
    main()
