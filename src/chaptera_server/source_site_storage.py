"""Actor-scoped, bounded, read-only source catalog snapshots and stage receipts.

The source catalog is metadata only. Collected HTML enters the existing
ImportSessions.stage() and still needs explicit manual create/replace/skip.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict

from .source_collect import SourceCatalog, SourceChapter, catalog_fingerprint
from .source_catalog_med import source_book_id, validate_url
from .storage import QueueStore

CATALOG_TTL_SECONDS = 1800
MAX_CATALOG_CHAPTERS = 2000
MAX_ACTIVE_CATALOGS = 3


class SourceCatalogMissing(KeyError):
    pass


class SourceCatalogExpired(ValueError):
    pass


class SourceCatalogConflict(ValueError):
    pass


def _fingerprint(
    chapters: tuple[SourceChapter, ...], source: str, book_url: str,
) -> str:
    return catalog_fingerprint(source, book_url, chapters)


class SourceCatalogStore:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_book_catalogs (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    book_url TEXT NOT NULL,
                    catalog_sha256 TEXT NOT NULL,
                    chapters_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_source_book_catalog_actor
                    ON source_book_catalogs(actor_id,expires_at);
                CREATE TABLE IF NOT EXISTS source_book_stage_receipts (
                    stage_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    catalog_sha256 TEXT NOT NULL,
                    selected_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            # Idempotent additive upgrade of existing RE-02 SQLite stores.
            columns = {row["name"] for row in con.execute("PRAGMA table_info(source_book_catalogs)")}
            if "toc_pages_json" not in columns:
                con.execute("ALTER TABLE source_book_catalogs ADD COLUMN toc_pages_json TEXT NOT NULL DEFAULT '[]'")
            if "declared_chapter_count" not in columns:
                con.execute("ALTER TABLE source_book_catalogs ADD COLUMN declared_chapter_count INTEGER")
            if "coverage_state" not in columns:
                con.execute("ALTER TABLE source_book_catalogs ADD COLUMN coverage_state TEXT NOT NULL DEFAULT 'observed_html_only'")

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        chapters = json.loads(row["chapters_json"])
        ordinals = [ch["ordinal"] for ch in chapters]
        sorted_ordinals = sorted(set(ordinals))
        gaps = []
        for left, right in zip(sorted_ordinals, sorted_ordinals[1:]):
            if right - left > 1:
                remaining = 200 - len(gaps)
                gaps.extend(range(left + 1, left + 1 + min(remaining, right - left - 1)))
                if len(gaps) >= 200:
                    break
        return {
            "catalog_id": row["id"],
            "source": row["source"],
            "book_url": row["book_url"],
            "catalog_sha256": row["catalog_sha256"],
            "catalog_semantics_version": 2,
            "expires_at": row["expires_at"],
            "chapter_count": len(chapters),
            "first_ordinal": min(ordinals),
            "last_ordinal": max(ordinals),
            "chapters": chapters,
            "catalog_gaps": gaps,
            "toc_page_count": max(1,len(json.loads(row["toc_pages_json"]))),
            "toc_page_urls": json.loads(row["toc_pages_json"]) or [row["book_url"]],
            "declared_chapter_count": row["declared_chapter_count"],
            "coverage_state": row["coverage_state"],
            "scope": "observed_public_html_only",
        }

    def save(self, actor: str, catalog: SourceCatalog) -> dict:
        if not catalog.chapters or len(catalog.chapters) > MAX_CATALOG_CHAPTERS:
            raise SourceCatalogConflict("Catalog must contain 1..2000 chapters")
        if any(type(ch.ordinal) is not int or not 1 <= ch.ordinal <= 10_000_000 for ch in catalog.chapters):
            raise SourceCatalogConflict("Chapter number outside supported range")
        if catalog.catalog_sha256 != _fingerprint(catalog.chapters, catalog.source, catalog.book_url):
            raise SourceCatalogConflict("Source catalog fingerprint mismatch")
        pages = catalog.page_urls or (catalog.book_url,)
        if (catalog.coverage_state not in ("declared_count_matched","observed_html_only") or
            not 1 <= len(pages) <= 12 or
            (catalog.declared_chapter_count is not None and
             catalog.declared_chapter_count != len(catalog.chapters))):
            raise SourceCatalogConflict("Source catalog coverage metadata invalid")
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM source_book_catalogs WHERE expires_at < ?", (now,))
            old = con.execute(
                "SELECT id FROM source_book_catalogs WHERE actor_id=? "
                "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
                (actor, MAX_ACTIVE_CATALOGS-1),
            ).fetchall()
            if old:
                con.executemany(
                    "DELETE FROM source_book_catalogs WHERE id=?",
                    [(row["id"],) for row in old],
                )
            cat_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO source_book_catalogs("
                "id,actor_id,source,book_url,catalog_sha256,chapters_json,created_at,expires_at,"
                "toc_pages_json,declared_chapter_count,coverage_state"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cat_id,actor,catalog.source,catalog.book_url,catalog.catalog_sha256,
                 json.dumps([asdict(ch) for ch in catalog.chapters],ensure_ascii=False),
                 now,now+CATALOG_TTL_SECONDS,
                 json.dumps(pages),catalog.declared_chapter_count,
                 catalog.coverage_state),
            )
            row = con.execute("SELECT * FROM source_book_catalogs WHERE id=?", (cat_id,)).fetchone()
            return self._public(row)

    def load(self, actor: str, catalog_id: str) -> SourceCatalog:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT * FROM source_book_catalogs WHERE id=? AND actor_id=?",
                (catalog_id, actor),
            ).fetchone()
        if row is None:
            raise SourceCatalogMissing("Catalog not found")
        if row["expires_at"] < time.time():
            raise SourceCatalogExpired("Catalog expired; inspect book again")
        try:
            chapters = tuple(SourceChapter(**c) for c in json.loads(row["chapters_json"]))
            validate_url(row["book_url"],row["source"])
            book_id = source_book_id(row["book_url"],row["source"])
            for ch in chapters:
                validate_url(ch.url,row["source"])
                if source_book_id(ch.url,row["source"]) != book_id:
                    raise ValueError("Foreign book in catalog")
            if not chapters or len(chapters) > MAX_CATALOG_CHAPTERS:
                raise ValueError("Invalid catalog cardinality")
            if any(type(ch.ordinal) is not int or not 1 <= ch.ordinal <= 10_000_000 for ch in chapters):
                raise ValueError("Invalid catalog chapter number")
            if _fingerprint(chapters, row['source'], row['book_url']) != row["catalog_sha256"]:
                raise ValueError("Catalog data changed")
            if len({c.ordinal for c in chapters}) != len(chapters):
                raise ValueError("Duplicate ordinals")
            if len({c.source_id for c in chapters}) != len(chapters):
                raise ValueError("Duplicate source IDs")
            pages=tuple(json.loads(row["toc_pages_json"])) or (row["book_url"],)
            if not 1 <= len(pages) <= 12:
                raise ValueError("Invalid TOC page provenance")
            for url in pages:
                validate_url(url,row["source"])
                if source_book_id(url,row["source"]) != book_id:
                    raise ValueError("Foreign TOC page in stored catalog")
            declared=row["declared_chapter_count"]
            if declared is not None and declared!=len(chapters):
                raise ValueError("Advertised chapter count diverged from catalog")
            coverage=row["coverage_state"]
            if coverage not in ("declared_count_matched","observed_html_only"):
                raise ValueError("Invalid TOC coverage state")
            return SourceCatalog(row["source"],row["book_url"],chapters,
                                 row["catalog_sha256"],pages,declared,coverage)
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceCatalogConflict("Stored catalog is invalid") from exc

    def save_stage_receipt(
        self, actor: str, catalog_id: str, stage_id: str,
        catalog_sha256: str, chapters: list[dict],
    ) -> None:
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO source_book_stage_receipts("
                "stage_id,actor_id,catalog_id,catalog_sha256,selected_json,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (stage_id,actor,catalog_id,catalog_sha256,
                 json.dumps(chapters,ensure_ascii=False,sort_keys=True),time.time()),
            )

    def stage_receipt(self, actor: str, stage_id: str) -> dict:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT * FROM source_book_stage_receipts WHERE stage_id=? AND actor_id=?",
                (stage_id,actor),
            ).fetchone()
        if row is None:
            raise SourceCatalogMissing("Source stage receipt not found")
        return {"stage_id":stage_id, "catalog_id":row["catalog_id"],
                "catalog_sha256":row["catalog_sha256"],
                "chapters":json.loads(row["selected_json"])}
