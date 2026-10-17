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

from .source_collect import SourceCatalog, SourceChapter
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


def _fingerprint(chapters: tuple[SourceChapter, ...]) -> str:
    canonical = "\n".join(
        f"{c.ordinal}\t{c.source_id}\t{c.url}" for c in chapters
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            "expires_at": row["expires_at"],
            "chapter_count": len(chapters),
            "first_ordinal": min(ordinals),
            "last_ordinal": max(ordinals),
            "chapters": chapters,
            "catalog_gaps": gaps,
        }

    def save(self, actor: str, catalog: SourceCatalog) -> dict:
        if not catalog.chapters or len(catalog.chapters) > MAX_CATALOG_CHAPTERS:
            raise SourceCatalogConflict("Catalog must contain 1..2000 chapters")
        if any(type(ch.ordinal) is not int or not 1 <= ch.ordinal <= 10_000_000 for ch in catalog.chapters):
            raise SourceCatalogConflict("Chapter number outside supported range")
        if catalog.catalog_sha256 != _fingerprint(catalog.chapters):
            raise SourceCatalogConflict("Source catalog fingerprint mismatch")
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
                "id,actor_id,source,book_url,catalog_sha256,chapters_json,created_at,expires_at"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (cat_id,actor,catalog.source,catalog.book_url,catalog.catalog_sha256,
                 json.dumps([asdict(ch) for ch in catalog.chapters],ensure_ascii=False),
                 now,now+CATALOG_TTL_SECONDS),
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
            if _fingerprint(chapters) != row["catalog_sha256"]:
                raise ValueError("Catalog data changed")
            if len({c.ordinal for c in chapters}) != len(chapters):
                raise ValueError("Duplicate ordinals")
            if len({c.source_id for c in chapters}) != len(chapters):
                raise ValueError("Duplicate source IDs")
            return SourceCatalog(row["source"],row["book_url"],chapters,row["catalog_sha256"])
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
