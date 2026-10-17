"""Durable SQLite checkpoint adapter around the existing desktop atom algorithm.

Only pre-validated provider responses are persisted. Core still owns source
atoms, reconciliation, repair, completeness and final publication validation.
The job's immutable input and exact prompt/payload form the reuse boundary.
"""
from __future__ import annotations

import hashlib
import re
import time

from .storage import QueueStore
from chaptera_translation_core.atom_transport import atom_checkpoint_key


class AtomCheckpointConflict(ValueError):
    """Checkpoint is altered, oversized or written by a stale worker lease."""


MAX_RESPONSE_BYTES = 8 * 1024 * 1024
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


class SQLiteAtomCheckpoint:
    """Job-private, fenced load/save for a single owner of an active lease.

    A different worker after a normal retry or daily reset gets a new lease,
    but can load already committed checkpoints for this same immutable job.
    """

    def __init__(self, store: QueueStore, job_id: str, lease_owner: str,
                 model_id: str = "unconfigured"):
        if not job_id or not lease_owner:
            raise ValueError("Job and lease ownership are required")
        self.store = store
        self.job_id = job_id
        self.lease_owner = lease_owner
        self.model_id = model_id
        self.saved_this_run = 0
        self.reused_this_run = 0

    @staticmethod
    def _check_key(key: str) -> None:
        if not FINGERPRINT.fullmatch(key):
            raise AtomCheckpointConflict("Invalid atom checkpoint fingerprint")

    def _storage_key(self, key: str) -> str:
        self._check_key(key)
        # Never mix the same prompt/atoms between model versions.
        value = (self.model_id + "\\0" + key).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _authorized(self, con) -> None:
        row = con.execute(
            """SELECT 1 FROM jobs WHERE id=? AND status='running'
               AND lease_owner=? AND cancel_requested=0""",
            (self.job_id, self.lease_owner),
        ).fetchone()
        if row is None:
            raise AtomCheckpointConflict("Stale, cancelled or invalid atom checkpoint lease")

    def load(self, key: str) -> str | None:
        storage_key = self._storage_key(key)
        with self.store._connect() as con:
            self._authorized(con)
            row = con.execute(
                """SELECT response_text,response_sha256 FROM atom_job_checkpoints
                   WHERE job_id=? AND request_fingerprint=?""",
                (self.job_id, storage_key),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["response_text"])
        data = raw.encode("utf-8")
        if len(data) > MAX_RESPONSE_BYTES or hashlib.sha256(data).hexdigest() != row["response_sha256"]:
            raise AtomCheckpointConflict("Atom checkpoint content integrity mismatch")
        self.reused_this_run += 1
        return raw

    def save(self, key: str, raw: str) -> None:
        storage_key = self._storage_key(key)
        if not isinstance(raw, str) or not raw.strip():
            raise AtomCheckpointConflict("Only nonempty validated atom responses may be saved")
        data = raw.encode("utf-8")
        if len(data) > MAX_RESPONSE_BYTES:
            raise AtomCheckpointConflict("Atom response exceeds checkpoint size bound")
        checksum = hashlib.sha256(data).hexdigest()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._authorized(con)
            row = con.execute(
                """SELECT response_sha256 FROM atom_job_checkpoints
                   WHERE job_id=? AND request_fingerprint=?""",
                (self.job_id, storage_key),
            ).fetchone()
            if row is not None:
                if row["response_sha256"] != checksum:
                    raise AtomCheckpointConflict("Conflicting response for immutable atom request")
            else:
                con.execute(
                    """INSERT INTO atom_job_checkpoints(
                        job_id,request_fingerprint,response_text,response_sha256,created_at)
                       VALUES(?,?,?,?,?)""",
                    (self.job_id, storage_key, raw, checksum, time.time()),
                )
                self.saved_this_run += 1
            con.execute("COMMIT")
