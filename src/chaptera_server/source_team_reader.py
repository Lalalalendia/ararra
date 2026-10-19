"""Read-only, team-shared access to verified source collection checkpoints.

Ordinary BookLibrary chapters/translations are already team-shared. This
exposes the still-unimported original chapters, never mutable job controls,
personal credentials, quota ledgers, or another moderator's import stage.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing

from .source_job_store import (
    MAX_ITEM_BYTES, SourceJobConflict, SourceJobMissing, SourceJobStore,
)
from .storage import QueueStore


class SourceTeamReader:
    def __init__(self, repo: QueueStore):
        self.repo = repo

    @staticmethod
    def _shared(con: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = con.execute(
            "SELECT j.* FROM source_collect_jobs AS j WHERE j.id=?"
            " AND EXISTS (SELECT 1 FROM source_collect_items AS i"
            " WHERE i.job_id=j.id AND i.status='ok')",
            (job_id,),
        ).fetchone()
        if row is None:
            # A non-existent or unverified-only job has identical visibility.
            raise SourceJobMissing("Verified source collection not found")
        return row

    @staticmethod
    def _verified_metadata(item: sqlite3.Row, chapters: list[dict]) -> dict:
        idx = item["item_index"]
        if type(idx) is not int or not 0 <= idx < len(chapters):
            raise SourceJobConflict("Source checkpoint index does not match manifest")
        chapter = chapters[idx]
        if (
            item["ordinal"] != chapter["ordinal"]
            or item["source_id"] != chapter["source_id"]
            or item["url"] != chapter["url"]
            or not isinstance(item["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None
            or type(item["bytes"]) is not int
            or not 0 < item["bytes"] <= MAX_ITEM_BYTES
        ):
            raise SourceJobConflict("Verified source checkpoint identity mismatch")
        return {
            "ordinal": chapter["ordinal"],
            "title": chapter["title"],
            "source_id": chapter["source_id"],
            "url": chapter["url"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        }

    def list_recent(self, actor: str, *, limit: int = 20) -> list[dict]:
        """Authenticated-team book index; never include original chapter prose."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Shared source list limit must be 1..50")
        with closing(self.repo._connect()) as con:
            con.execute("BEGIN")
            rows = con.execute(
                "SELECT j.*, (SELECT COUNT(*) FROM source_collect_items AS i"
                " WHERE i.job_id=j.id AND i.status='ok') AS saved_count "
                "FROM source_collect_jobs AS j "
                "WHERE EXISTS (SELECT 1 FROM source_collect_items AS i"
                " WHERE i.job_id=j.id AND i.status='ok') "
                "ORDER BY j.updated_at DESC,j.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for row in rows:
                chapters = SourceJobStore._manifest(row)
                result.append({
                    "job_id": row["id"],
                    "collector_member_id": row["actor_id"],
                    "can_manage": row["actor_id"] == actor,
                    "source": row["source"],
                    "book_url": row["book_url"],
                    "selected_start": min(ch["ordinal"] for ch in chapters),
                    "selected_end": max(ch["ordinal"] for ch in chapters),
                    "verified_count": row["saved_count"],
                    "selected_count": len(chapters),
                    "updated_at": row["updated_at"],
                    "scope": "verified_original_checkpoints",
                })
            return result

    def chapters(self, job_id: str, *, offset: int = 0, limit: int = 50) -> dict:
        if (
            type(offset) is not int or not 0 <= offset <= 2000
            or type(limit) is not int or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid shared source chapter window")
        with closing(self.repo._connect()) as con:
            con.execute("BEGIN")
            row = self._shared(con, job_id)
            manifest = SourceJobStore._manifest(row)
            total = con.execute(
                "SELECT COUNT(*) FROM source_collect_items "
                "WHERE job_id=? AND status='ok'", (job_id,),
            ).fetchone()[0]
            items = con.execute(
                "SELECT item_index,ordinal,source_id,url,sha256,bytes "
                "FROM source_collect_items WHERE job_id=? AND status='ok' "
                "ORDER BY item_index LIMIT ? OFFSET ?", (job_id,limit,offset),
            ).fetchall()
            return {
                "job_id": job_id,
                "collector_member_id": row["actor_id"],
                "source": row["source"],
                "book_url": row["book_url"],
                "catalog_sha256": row["catalog_sha256"],
                "selected_count": len(manifest),
                "verified_count": total,
                "offset": offset,
                "has_more": offset + len(items) < total,
                "chapters": [self._verified_metadata(item, manifest) for item in items],
                "scope": "verified_original_checkpoints",
            }

    def chapter(self, job_id: str, ordinal: int) -> dict:
        """Return one checksum-verified original, not a bulk JSON manuscript."""
        if type(ordinal) is not int or not 1 <= ordinal <= 10_000_000:
            raise ValueError("Invalid source chapter number")
        with closing(self.repo._connect()) as con:
            con.execute("BEGIN")
            row = self._shared(con, job_id)
            manifest = SourceJobStore._manifest(row)
            item = con.execute(
                "SELECT * FROM source_collect_items "
                "WHERE job_id=? AND ordinal=? AND status='ok'",
                (job_id,ordinal),
            ).fetchone()
            if item is None:
                raise SourceJobMissing("Verified source chapter not found")
            metadata = self._verified_metadata(item, manifest)
            body = item["body"]
            if not isinstance(body, str):
                raise SourceJobConflict("Source chapter body missing")
            actual = body.encode("utf-8")
            if len(actual) != metadata["bytes"] or (
                hashlib.sha256(actual).hexdigest() != metadata["sha256"]
            ):
                raise SourceJobConflict("Source content checksum mismatch")
            return {
                "job_id": job_id,
                "collector_member_id": row["actor_id"],
                "source": row["source"],
                "book_url": row["book_url"],
                "body": body,
                **metadata,
                "scope": "single_verified_original",
            }
