"""Cross-product acceptance: ZIP emitted by the real Full Cycle App exporter.

Fixture was produced by executing src/full_cycle_app/projects/chapter_exchange.py
from the user's 2026-10-17 source dump, with a minimal in-memory Store containing
synthetic chapters 7, 9 and 10. No actual book text, network or billing.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO

from chaptera_server.book_import import parse_book_import
from chaptera_server.import_sessions import ImportSessions
from chaptera_server.library import BookLibrary
from chaptera_server.storage import QueueStore

FIXTURE = Path(__file__).parent / "fixtures" / "full_cycle_actual_exporter_20261017.zip.b64"
ZIP_SHA256 = "b23df10ca7abd4e155cfdf38e7203a55e58554e7034ae9787394dbb2de7d146a"


def test_actual_full_cycle_exporter_zip_preview_commit_and_raw_readback(tmp_path):
    data = base64.b64decode(FIXTURE.read_text(encoding="ascii"), validate=False)
    assert hashlib.sha256(data).hexdigest() == ZIP_SHA256
    with ZipFile(BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "full_cycle_app.chapter_exchange"
        assert manifest["version"] == 1 and manifest["kind"] == "source"
        assert [i["chapter_index"] for i in manifest["chapters"]] == [7, 9, 10]
        for row in manifest["chapters"]:
            for version in ("original", "working"):
                payload = archive.read(row[f"{version}_file"])
                assert hashlib.sha256(payload).hexdigest() == row[f"{version}_sha256"]
    preview, parsed, _ = parse_book_import("fullcycle.zip", data)
    assert preview["source_archive"]["boundaries"] == "manifest"
    assert preview["chapter_count"] == 3
    assert [x["suggested_index"] for x in preview["chapters"]] == [7, 9, 10]
    assert preview["source_archive"]["missing_positions"] == [[8, 8]]
    assert len(parsed) == 3
    assert parsed[1].archive["original_text"] != parsed[1].archive["working_text"]
    store = QueueStore(tmp_path / "authentic-fullcycle.sqlite3")
    staged = ImportSessions(store).stage("alice", "fullcycle.zip", data)
    payload = {
        "book_id": "fullcycle-fixture",
        "book_mode": "create",
        "title": "История",
        "prefix_policy": "reject",
        "chapters": [
            {"sequence": i + 1, "action": "create", "chapter_id": f"chapter-{idx}",
             "chapter_index": idx, "chapter_path": f"chapters/{idx}.xhtml",
             "expected_revision": 0}
            for i, idx in enumerate((7, 9, 10))
        ],
    }
    receipt = ImportSessions(store).confirm("alice", staged["stage_id"], payload)
    assert receipt["imported_count"] == 3
    library = BookLibrary(store)
    assert "第999章" in library.get_chapter("fullcycle-fixture", "chapter-7")["source_html"]
    restored = ImportSessions(QueueStore(store.db_path)).archive_original("fullcycle-fixture", "chapter-9")
    assert restored["original_text"] == "原文九.\n\nАбзац два."
    assert restored["working_text"] == "手工 исправленный текст.\n\nАбзац два."
    assert restored["original_sha256"] != restored["working_sha256"]
