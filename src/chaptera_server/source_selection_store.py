"""Actor-owned TOC selection drafts; immutable ordered plan when a job starts.

Only catalog ordinals are accepted: URLs and book ownership always originate
in SourceCatalogStore. Saves are CAS-fenced to prevent stale tabs overwriting
another selection. No external requests or source prose are stored here.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

from .source_collect import SourceCatalog
from .source_site_storage import SourceCatalogConflict
from .storage import QueueStore

MAX_PLAN_ITEMS = 2000


class SourceSelectionMissing(KeyError):
    pass


class SourceSelectionExpired(ValueError):
    pass


class SourceSelectionConflict(ValueError):
    pass


def _canonical(catalog_sha256: str, ordinals: list[int]) -> str:
    return json.dumps(
        {"version": 1, "catalog_sha256": catalog_sha256, "ordered_ordinals": ordinals},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def _digest(catalog_sha256: str, ordinals: list[int]) -> str:
    return hashlib.sha256(_canonical(catalog_sha256, ordinals).encode("utf-8")).hexdigest()


class SourceSelectionStore:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_book_selections (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    catalog_id TEXT NOT NULL,
                    catalog_sha256 TEXT NOT NULL,
                    ordered_ordinals_json TEXT NOT NULL,
                    selection_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    UNIQUE(actor_id,catalog_id)
                );
                CREATE INDEX IF NOT EXISTS ix_source_book_selections_recent
                    ON source_book_selections(actor_id,updated_at DESC);
                CREATE INDEX IF NOT EXISTS ix_source_book_selections_expiry
                    ON source_book_selections(expires_at);
            """)

    @staticmethod
    def _public(row) -> dict:
        try:
            numbers = json.loads(row["ordered_ordinals_json"])
            if (not isinstance(numbers, list) or not 1 <= len(numbers) <= MAX_PLAN_ITEMS or
                any(type(n) is not int or not 1 <= n <= 10_000_000 for n in numbers) or
                len(set(numbers)) != len(numbers) or
                _digest(row["catalog_sha256"], numbers) != row["selection_sha256"]):
                raise ValueError("Invalid or changed selection")
        except (TypeError, ValueError) as exc:
            raise SourceSelectionConflict("Saved selection is invalid") from exc
        return {
            "selection_id": row["id"],
            "catalog_id": row["catalog_id"],
            "catalog_sha256": row["catalog_sha256"],
            "selection_sha256": row["selection_sha256"],
            "ordered_ordinals": numbers,
            "selected_count": len(numbers),
            "expires_at": row["expires_at"],
            "plan_version": 1,
        }

    @staticmethod
    def _catalog_row(con, actor: str, catalog_id: str, now: float):
        row = con.execute(
            "SELECT catalog_sha256,expires_at FROM source_book_catalogs "
            "WHERE id=? AND actor_id=?", (catalog_id, actor),
        ).fetchone()
        if row is None:
            raise SourceSelectionMissing("Source catalog not found")
        if row["expires_at"] <= now:
            raise SourceSelectionExpired("Catalog expired; inspect book again")
        return row

    def save(
        self, actor: str, catalog_id: str, catalog: SourceCatalog,
        ordered_ordinals: list[int], *,
        expected_catalog_sha256: str, expected_selection_sha256: str | None,
    ) -> dict:
        if (not isinstance(ordered_ordinals, list) or
            not 1 <= len(ordered_ordinals) <= MAX_PLAN_ITEMS or
            any(type(n) is not int or not 1 <= n <= 10_000_000 for n in ordered_ordinals) or
            len(set(ordered_ordinals)) != len(ordered_ordinals)):
            raise SourceSelectionConflict("Select 1..2000 unique catalog chapters")
        if catalog.catalog_sha256 != expected_catalog_sha256:
            raise SourceSelectionConflict("Stale catalog; inspect book again")
        available = {c.ordinal for c in catalog.chapters}
        if any(n not in available for n in ordered_ordinals):
            raise SourceSelectionConflict("Selection contains a chapter outside the observed book")
        if (expected_selection_sha256 is not None and
            not re.fullmatch(r"[a-f0-9]{64}", expected_selection_sha256)):
            raise SourceSelectionConflict("Invalid selection CAS digest")
        now = time.time()
        serialized = json.dumps(ordered_ordinals, separators=(",", ":"))
        digest = _digest(expected_catalog_sha256, ordered_ordinals)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM source_book_selections WHERE expires_at<?", (now,))
            origin = self._catalog_row(con, actor, catalog_id, now)
            if origin["catalog_sha256"] != expected_catalog_sha256:
                raise SourceSelectionConflict("Catalog changed during selection")
            prior = con.execute(
                "SELECT * FROM source_book_selections WHERE actor_id=? AND catalog_id=?",
                (actor, catalog_id),
            ).fetchone()
            if ((prior["selection_sha256"] if prior else None) != expected_selection_sha256):
                raise SourceSelectionConflict("Selection changed in another tab; reload the plan")
            if prior:
                con.execute(
                    "UPDATE source_book_selections SET ordered_ordinals_json=?,"
                    "selection_sha256=?,updated_at=?,expires_at=? WHERE id=?",
                    (serialized, digest, now, origin["expires_at"], prior["id"]),
                )
                ident = prior["id"]
            else:
                ident = str(uuid.uuid4())
                con.execute(
                    "INSERT INTO source_book_selections("
                    "id,actor_id,catalog_id,catalog_sha256,ordered_ordinals_json,"
                    "selection_sha256,created_at,updated_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (ident, actor, catalog_id, expected_catalog_sha256,
                     serialized, digest, now, now, origin["expires_at"]),
                )
            saved = con.execute(
                "SELECT * FROM source_book_selections WHERE id=?", (ident,),
            ).fetchone()
            return self._public(saved) | {"catalog_count": len(catalog.chapters)}

    def load(self, actor: str, selection_id: str) -> dict:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT * FROM source_book_selections WHERE id=? AND actor_id=?",
                (selection_id, actor),
            ).fetchone()
            if row is None:
                raise SourceSelectionMissing("Selection not found")
            origin = self._catalog_row(con, actor, row["catalog_id"], time.time())
            if (row["expires_at"] <= time.time() or
                origin["catalog_sha256"] != row["catalog_sha256"]):
                raise SourceSelectionExpired("Source catalog or selection expired")
            return self._public(row)

    def get(self, actor: str, catalog_id: str) -> dict:
        with self.store._connect() as con:
            row = con.execute(
                "SELECT * FROM source_book_selections WHERE actor_id=? AND catalog_id=?",
                (actor, catalog_id),
            ).fetchone()
            if row is None:
                raise SourceSelectionMissing("No saved plan for this catalog")
            self._catalog_row(con, actor, catalog_id, time.time())
            if row["expires_at"] <= time.time():
                raise SourceSelectionExpired("Selection expired; inspect book again")
            return self._public(row)

    def recent(self, actor: str) -> dict:
        now = time.time()
        with self.store._connect() as con:
            row = con.execute(
                "SELECT s.* FROM source_book_selections AS s "
                "JOIN source_book_catalogs AS c "
                "ON c.id=s.catalog_id AND c.actor_id=s.actor_id "
                "WHERE s.actor_id=? AND s.expires_at>? AND c.expires_at>? "
                "AND s.catalog_sha256=c.catalog_sha256 "
                "ORDER BY s.updated_at DESC LIMIT 1",
                (actor, now, now),
            ).fetchone()
            return {"selection": self._public(row) if row else None}
