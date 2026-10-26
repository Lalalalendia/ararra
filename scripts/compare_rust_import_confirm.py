#!/usr/bin/env python3
"""M1p: disposable Python/Rust cross-runtime *atomic* TXT import acceptance.

Both HTTP processes share exactly one temporary Python-created SQLite schema.
No production DB, source network, Gemini, staged source disclosure or real keys.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import httpx
from compare_rust_api import launch, ready
from compare_rust_import_stages import runner_env
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.library import _normalized_source_hash
from chaptera_server.storage import QueueStore

NARRATIVE = "История продолжается вдоль большой реки между городами. " * 16
SOURCE = ("Глава 1\n" + NARRATIVE + "\nГлава 2\n" + NARRATIVE).encode()
STAGE = "/v1/imports/stages"


def request(book: str) -> dict:
    return {
        "book_id": book, "book_mode": "create", "title": "Проверка Rust",
        "prefix_policy": "discard",
        "chapters": [
            {"sequence": 2, "action": "create", "chapter_id": "ch-2",
             "chapter_index": 2, "chapter_path": "chapters/2.xhtml",
             "expected_revision": 0},
            {"sequence": 1, "action": "create", "chapter_id": "ch-1",
             "chapter_index": 1, "chapter_path": "chapters/1.xhtml",
             "expected_revision": 0},
        ],
    }


def inspect(db: Path, book: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT chapter_id,chapter_index,chapter_path,source_html,source_hash,"
            "source_revision FROM book_chapters WHERE book_id=? ORDER BY chapter_index",
            (book,),
        ).fetchall()
        for chapter_id, index, path, html, source_hash, revision in rows:
            assert source_hash == _normalized_source_hash(html, path)
            assert source_hash == hashlib.sha256(html.encode()).hexdigest(), (
                "Canonical current-chapter H1 must NOT be deleted", chapter_id
            )
            assert html.startswith(f"<h1>Глава {index}</h1>\n<p>")
            assert revision == 1
        return rows


def chapter_rows(db: Path, book: str) -> list[tuple]:
    """Read every source byte and destination version; never print chapter text."""
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT chapter_id,chapter_index,chapter_path,source_html,source_hash,"
            "source_revision FROM book_chapters WHERE book_id=? ORDER BY chapter_index",
            (book,),
        ).fetchall()


def replace_one_and_skip_other(book: str) -> dict:
    """Existing book: update chapter 1, explicitly skip chapter 2."""
    return {
        "book_id": book, "book_mode": "existing", "title": None,
        "owner_member_id": None, "prefix_policy": "reject",
        "chapters": [
            {"sequence": 2, "action": "skip"},
            {"sequence": 1, "action": "replace", "chapter_id": "ch-1",
             "chapter_index": 1, "chapter_path": "chapters/1.xhtml",
             "expected_revision": 1},
        ],
    }


async def exercise(app, db: Path, url: str, settings: Settings) -> None:
    alice = {"X-API-Key": settings.team_tokens["alice"]}
    bob = {"X-API-Key": settings.team_tokens["bob"]}
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://python", timeout=30) as py,
        httpx.AsyncClient(base_url=url, timeout=30) as rust,
    ):
        async def stage(client: httpx.AsyncClient, raw: bytes, *, actor=alice) -> str:
            response = await client.post(
                STAGE, headers=actor,
                files={"file": ("story.txt", raw, "text/plain")},
            )
            assert response.status_code == 201, response.text
            return response.json()["stage_id"]

        # Python-created stage must commit in native Rust and Python must
        # observe its saved chapter hashes and actor-owned receipt.
        rust_stage = await stage(py, SOURCE)
        url_rust = f"{STAGE}/{rust_stage}/confirm"
        assert (await rust.post(url_rust, json=request("rust-book"))).status_code == 401
        assert (await rust.post(url_rust, headers=bob,
                                json=request("rust-book"))).status_code == 404
        confirm = await rust.post(url_rust, headers=alice, json=request("rust-book"))
        assert confirm.status_code == 200, confirm.text
        receipt = confirm.json()
        assert receipt["created_book"] and receipt["imported_count"] == 2
        assert receipt["skipped_count"] == 0
        assert [r["sequence"] for r in receipt["chapters"]] == [1, 2]
        assert (await rust.post(url_rust, headers=alice, json=request("rust-book"))).json() == receipt
        assert (await py.get(f"{STAGE}/{rust_stage}",headers=alice)).json()["commit_result"] == receipt
        changed=request("rust-book")
        changed["chapters"][0]["chapter_id"]="different"
        assert (await rust.post(url_rust,headers=alice,json=changed)).status_code == 409
        rust_rows=inspect(db, "rust-book")
        assert len(rust_rows)==2

        # Rust-created stage confirmed by unchanged Python route: source
        # HTML and SHA256 must be byte identical across both writers.
        python_stage=await stage(rust,SOURCE)
        url_py=f"{STAGE}/{python_stage}/confirm"
        py_confirm=await py.post(url_py,headers=alice,json=request("python-book"))
        assert py_confirm.status_code == 200,py_confirm.text
        python_rows=inspect(db,"python-book")
        assert python_rows==rust_rows,(python_rows,rust_rows)
        assert (await rust.get(f"{STAGE}/{python_stage}",headers=alice)).json()["commit_result"]==py_confirm.json()


        # M1p acceptance of EXISTING books is not just create:
        # compare both writers' one-chapter replace + explicit skip and the
        # exact SQLite rows that survive rejected destination decisions.
        revised = ("Глава 1\n" +
                   NARRATIVE.replace("между городами", "между деревнями") +
                   "\nГлава 2\n" + NARRATIVE).encode()
        rust_replace_stage = await stage(py, revised)
        python_replace_stage = await stage(rust, revised)
        providers = (
            (rust, rust_replace_stage, "rust-book"),
            (py, python_replace_stage, "python-book"),
        )
        expected_before = {
            book: chapter_rows(db, book) for _, _, book in providers
        }
        assert expected_before["rust-book"] == expected_before["python-book"]
        assert len(expected_before["rust-book"]) == 2
        for name in ("stale_revision", "occupied_index", "occupied_path",
                     "skip_with_destination", "missing_decision",
                     "preserve_absent_prefix"):
            for client, stage_id, book in providers:
                candidate = replace_one_and_skip_other(book)
                if name == "stale_revision":
                    candidate["chapters"][1]["expected_revision"] = 0
                elif name == "occupied_index":
                    candidate["chapters"][1]["chapter_index"] = 2
                elif name == "occupied_path":
                    candidate["chapters"][1]["chapter_path"] = "chapters/2.xhtml"
                elif name == "skip_with_destination":
                    candidate["chapters"][0]["chapter_id"] = "ch-2"
                elif name == "missing_decision":
                    candidate["chapters"].pop(0)
                elif name == "preserve_absent_prefix":
                    candidate["prefix_policy"] = "preserve"
                result = await client.post(
                    f"{STAGE}/{stage_id}/confirm", headers=alice, json=candidate,
                )
                assert result.status_code == 409, (
                    name, book, result.status_code, result.text
                )
                assert chapter_rows(db, book) == expected_before[book], (
                    name, "rejected decision changed an existing book", book
                )
                with sqlite3.connect(db) as conn:
                    row = conn.execute(
                        "SELECT status,commit_result_json FROM book_import_stages "
                        "WHERE id=?", (stage_id,),
                    ).fetchone()
                assert row == ("staged", None), (name, book, row)
        for client, stage_id, book in providers:
            selection = replace_one_and_skip_other(book)
            confirmed = await client.post(
                f"{STAGE}/{stage_id}/confirm", headers=alice, json=selection,
            )
            assert confirmed.status_code == 200, (book, confirmed.text)
            receipt = confirmed.json()
            assert receipt["created_book"] is False
            assert receipt["imported_count"] == 1
            assert receipt["skipped_count"] == 1
            assert receipt["chapters"][0]["sequence"] == 1
            assert receipt["chapters"][0]["source_revision"] == 2
            assert receipt["chapters"][1] == {"sequence": 2, "action": "skip"}
            assert (await client.post(
                f"{STAGE}/{stage_id}/confirm", headers=alice, json=selection
            )).json() == receipt
        after_rust = chapter_rows(db, "rust-book")
        after_python = chapter_rows(db, "python-book")
        assert after_rust == after_python, "Rust/Python replace+skip changed different bytes"
        assert after_rust[0][-1] == 2 and after_rust[1][-1] == 1
        assert after_rust[0][3] != expected_before["rust-book"][0][3]
        assert after_rust[1] == expected_before["rust-book"][1]
        with sqlite3.connect(db) as conn:
            for book in ("rust-book", "python-book"):
                assert conn.execute(
                    "SELECT COUNT(*) FROM book_owner_events WHERE book_id=?",
                    (book,),
                ).fetchone()[0] == 1, "existing import must not change book ownership"
        # A fresh source revision makes a once-valid replace CAS stale.
        retry_stage = await stage(py, revised)
        stale = replace_one_and_skip_other("rust-book")
        rejected = await rust.post(
            f"{STAGE}/{retry_stage}/confirm", headers=alice, json=stale,
        )
        assert rejected.status_code == 409, rejected.text
        assert chapter_rows(db, "rust-book") == after_rust


        # A real unassigned preface must not be silently discarded.
        # Both writers must reject the default "reject" policy and preserve
        # the exact unassigned text/hash only after explicit consent.
        prefaced = ("Перед началом.\n\n" + SOURCE.decode("utf-8")).encode("utf-8")
        prefix_rust_stage = await stage(py, prefaced)
        prefix_python_stage = await stage(rust, prefaced)
        for client, stage_id, book in (
            (rust, prefix_rust_stage, "rust-preface"),
            (py, prefix_python_stage, "python-preface"),
        ):
            staged = await client.get(f"{STAGE}/{stage_id}", headers=alice)
            assert staged.status_code == 200, staged.text
            prefix_html = staged.json()["unassigned_prefix_html"]
            assert prefix_html == "<p>Перед началом.</p>", prefix_html
            selection = request(book)
            selection["prefix_policy"] = "reject"
            refusal = await client.post(
                f"{STAGE}/{stage_id}/confirm", headers=alice, json=selection,
            )
            assert refusal.status_code == 409, (book, refusal.text)
            with sqlite3.connect(db) as conn:
                assert conn.execute(
                    "SELECT COUNT(*) FROM books WHERE id=?", (book,)
                ).fetchone()[0] == 0
                assert conn.execute(
                    "SELECT status,commit_result_json FROM book_import_stages WHERE id=?",
                    (stage_id,),
                ).fetchone() == ("staged", None)
            selection["prefix_policy"] = "preserve"
            success = await client.post(
                f"{STAGE}/{stage_id}/confirm", headers=alice, json=selection,
            )
            assert success.status_code == 200, (book, success.text)
            assert success.json()["unassigned_prefix_preserved"] is True
            with sqlite3.connect(db) as conn:
                saved = conn.execute(
                    "SELECT source_html,source_sha256,actor_id FROM "
                    "book_import_unassigned WHERE book_id=?", (book,),
                ).fetchone()
            assert saved == (
                prefix_html, hashlib.sha256(prefix_html.encode()).hexdigest(), "alice"
            ), (book, "unassigned preface bytes changed")
        assert inspect(db, "rust-preface") == inspect(db, "python-preface")

        # An expired stage is never resurrected into a write by either API.
        expired_stage = await stage(py, SOURCE)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE book_import_stages SET expires_at=0 WHERE id=?",
                (expired_stage,),
            )
        expiry_statuses = []
        for client in (rust, py):
            expired = await client.post(
                f"{STAGE}/{expired_stage}/confirm", headers=alice,
                json=request("should-not-exist-expired"),
            )
            expiry_statuses.append(expired.status_code)
        assert expiry_statuses[0] == expiry_statuses[1], expiry_statuses
        assert expiry_statuses[0] in (404, 410), expiry_statuses
        with sqlite3.connect(db) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM books WHERE id='should-not-exist-expired'"
            ).fetchone()[0] == 0

        # A staged chapter using an unsupported escaped entity MUST NOT
        # be imported by Rust just because Python might normalize it.
        unsafe=await stage(rust,("Глава 1\n"+NARRATIVE+"A & B").encode())
        one=request("entity-refused")
        one["chapters"]=one["chapters"][:1]
        one["chapters"][0].update({
            "sequence":1,"chapter_id":"cx","chapter_index":1,
            "chapter_path":"chapters/1.xhtml",
        })
        refused=await rust.post(f"{STAGE}/{unsafe}/confirm",headers=alice,json=one)
        assert refused.status_code == 422,refused.text
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM books WHERE id='entity-refused'").fetchone()[0]==0
            assert conn.execute("SELECT status FROM book_import_stages WHERE id=?",
                                (unsafe,)).fetchone()[0]=="staged"

        # A real SQLite RAISE(FAIL) on the SECOND chapter must roll back
        # the book, owner event, first chapter and stage receipt together.
        crash_stage=await stage(py,SOURCE)
        with sqlite3.connect(db) as conn:
            conn.execute("""
                CREATE TRIGGER m1p_inject_second BEFORE INSERT ON book_chapters
                WHEN NEW.book_id='fault-book' AND NEW.chapter_id='ch-2'
                BEGIN SELECT RAISE(FAIL,'injected second-chapter SQL failure'); END
            """)
        failed=await rust.post(f"{STAGE}/{crash_stage}/confirm",headers=alice,json=request("fault-book"))
        assert failed.status_code in (409,503),failed.text
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM books WHERE id='fault-book'").fetchone()[0]==0
            assert conn.execute("SELECT COUNT(*) FROM book_owner_events WHERE book_id='fault-book'").fetchone()[0]==0
            assert conn.execute("SELECT COUNT(*) FROM book_chapters WHERE book_id='fault-book'").fetchone()[0]==0
            assert conn.execute("SELECT status FROM book_import_stages WHERE id=?",
                                (crash_stage,)).fetchone()[0]=="staged"
            conn.execute("DROP TRIGGER m1p_inject_second")
        retry=await rust.post(f"{STAGE}/{crash_stage}/confirm",headers=alice,json=request("fault-book"))
        assert retry.status_code==200,retry.text
        assert len(inspect(db,"fault-book"))==2
        # Authoritative data is not accessible by a foreign actor.
        assert (await rust.get(f"{STAGE}/{crash_stage}",headers=bob)).status_code==404

        # Keep production Python features untouched. No paid worker, no
        # background sync or arbitrary HTML fallback is present in Rust.

def main() -> None:
    if len(sys.argv)!=2:
        raise SystemExit("usage: compare_rust_import_confirm.py /path/to/erida-api")
    binary=str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1p-confirm-") as tmp:
        root=Path(tmp)
        db=root/"disposable.sqlite3"
        settings=Settings(
            db_path=db,
            team_tokens={"chief":"m1p-chief","alice":"m1p-alice","bob":"m1p-bob"},
            admin_member_id="chief",browser_session_secure=False,
        )
        app=create_app(settings, QueueStore(db))
        env=runner_env(root,db,settings)
        process=launch([binary],env)
        try:
            ready("http://127.0.0.1:18495",process)
            asyncio.run(exercise(app,db,"http://127.0.0.1:18495",settings))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        # The complete Rust import route must be absent when the strict
        # disposable writer double gate is disabled on the same SQLite DB.
        env.pop("ERIDA_RUST_TEST_WRITES")
        env.pop("ERIDA_RUST_DISPOSABLE_ROOT")
        process=launch([binary],env)
        try:
            ready("http://127.0.0.1:18495",process)
            with httpx.Client(base_url="http://127.0.0.1:18495") as rs:
                confirm_url="/v1/imports/stages/unknown/confirm"
                assert rs.post(confirm_url,headers={"X-API-Key":settings.team_tokens["alice"]}).status_code in (404,405)
                assert rs.get("/v1/books/rust-book",headers={"X-API-Key":settings.team_tokens["alice"]}).status_code==200
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print("rust_import_confirm_python_stage_to_rust_sqlite=PASS")
    print("rust_import_confirm_rust_stage_to_python_sqlite=PASS")
    print("rust_import_confirm_source_h1_sha256_exact=PASS")
    print("rust_import_confirm_second_chapter_rollback=PASS")
    print("rust_import_confirm_disposable_only_writer=PASS")
    print("rust_import_confirm_paid_gemini_calls=ZERO")


if __name__=="__main__":
    main()
