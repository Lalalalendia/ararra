"""Full offline TOC typing -> CAS -> manifest -> staged import, without sites/VPS."""
from __future__ import annotations

import json

from chaptera_server.import_sessions import ImportSessions
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_stage import stage_verified_source_job
from chaptera_server.source_job_store import SourceJobStore
from chaptera_server.source_selection_store import SourceSelectionStore
from chaptera_server.source_site_storage import SourceCatalogStore
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"


def toc(tail="End"):
    labels = ["Chapter 1 - First", "Announcement", "Prologue",
              "Unclassified note", "Chapter 2 - " + tail]
    return '<div id="chapters">' + ''.join(
        f'<a href="/fiction/142007/aura/chapter/{60000+i}/{i}-part">{title}</a>'
        for i, title in enumerate(labels, 1)
    ) + '</div>'


def test_catalog_semantics_are_durable_and_title_changes_invalidate_cas(tmp_path):
    original = catalog_from_html("royalroad", BOOK, toc())
    assert [c.entry_type for c in original.chapters] == [
        "chapter", "service", "extra", "unknown", "chapter"]
    assert [c.author_number for c in original.chapters] == [1, None, None, None, 2]
    assert [c.ordinal for c in original.chapters] == [1, 2, 3, 4, 5]
    assert original.catalog_sha256 != catalog_from_html(
        "royalroad", BOOK, toc("Revised")).catalog_sha256
    store = QueueStore(tmp_path / "typed.sqlite3")
    catalogs = SourceCatalogStore(store)
    saved = catalogs.save("alice", original)
    assert saved["catalog_semantics_version"] == 2
    assert saved["chapters"][1]["entry_type"] == "service"
    reloaded = SourceCatalogStore(QueueStore(store.db_path)).load("alice", saved["catalog_id"])
    assert reloaded == original
    selection = SourceSelectionStore(store).save(
        "alice", saved["catalog_id"], reloaded, [5, 2, 1],
        expected_catalog_sha256=original.catalog_sha256,
        expected_selection_sha256=None,
    )
    assert selection["selected_count"] == 3
    assert selection["ordered_ordinals"] == [5, 2, 1]


def test_ordered_stage_preserves_author_labels_and_review_types(tmp_path):
    store = QueueStore(tmp_path / "stage.sqlite3")
    catalogs = SourceCatalogStore(store)
    selections = SourceSelectionStore(store)
    jobs = SourceJobStore(store)
    sessions = ImportSessions(store)
    cat = catalog_from_html("royalroad", BOOK, toc())
    snapshot = catalogs.save("alice", cat)
    saved = selections.save(
        "alice", snapshot["catalog_id"], cat, [2, 1, 3, 4],
        expected_catalog_sha256=cat.catalog_sha256,
        expected_selection_sha256=None,
    )
    job = jobs.create(
        "alice", snapshot["catalog_id"], cat, [2, 1, 3, 4],
        preserve_order=True, selection_id=saved["selection_id"],
        selection_sha256=saved["selection_sha256"],
    )
    for i in range(4):
        claimed = jobs.claim()
        assert claimed is not None
        jobs.finish(claimed["job_id"], claimed["lease_token"],
                    body=(f"Verified unique chapter {i}. " * 20))
    outcome = stage_verified_source_job(
        jobs, sessions, catalogs, "alice", job["job_id"],
        start_position=1, end_position=4,
    )
    assert outcome["status"] == "staged"
    staged = sessions.get("alice", outcome["stage_id"])
    rows = staged["preview"]["chapters"]
    assert [x["catalog_ordinal"] for x in rows] == [2, 1, 3, 4]
    assert [x["entry_type"] for x in rows] == [
        "service", "chapter", "extra", "unknown"]
    assert [x["detected_number"] for x in rows] == [None, 1, None, None]
    assert [x["destination_position"] for x in rows] == [1, 2, 3, 4]
    assert [x["source_id"] for x in rows] == [
        "60002", "60001", "60003", "60004"]
    with store._connect() as con:
        raw = con.execute(
            "SELECT payload_json FROM book_import_stages WHERE id=?",
            (outcome["stage_id"],),
        ).fetchone()["payload_json"]
    payload = json.loads(raw)["chapters"]
    assert payload[0]["entry_type"] == "service"
    assert "<h1>Announcement</h1>" in payload[0]["source_html"]
    assert "<h1>Unclassified note</h1>" in payload[3]["source_html"]
    assert "Глава 4. Unclassified note" not in payload[3]["source_html"]

def test_fractional_royalroad_titles_never_become_wrong_integer_author_numbers():
    page = '<div id="chapters">' + ''.join(
        f'<a href="/fiction/142007/aura/chapter/{60001+i}/{i}-part">{label}</a>'
        for i, label in enumerate((
            'Chapter 10.5 Interlude', 'Chapter 30.1 - Bonus Log',
            'Chapter 10 - Main', 'No Chapter Today - Next Chapter Friday',
        ))
    ) + '</div>'
    cat = catalog_from_html("royalroad", BOOK, page)
    assert [c.ordinal for c in cat.chapters] == [1, 2, 3, 4]
    assert [c.author_number for c in cat.chapters] == [None, None, 10, None]
    assert [c.entry_type for c in cat.chapters] == [
        "unknown", "unknown", "chapter", "unknown",
    ]
