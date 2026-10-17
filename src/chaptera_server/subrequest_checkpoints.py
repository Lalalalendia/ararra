"""Durable, lease-fenced validated provider subrequest responses (RE-06A).

Only core-validated responses may be saved, after a provider call succeeds.
Neither UI nor the API can read these texts. Each fingerprint binds a specific
queued job, provider model, full prompt, stage and source payload; editing a
book creates a stale job, never a replay against a different book revision.

A crash between receiving a remote response and committing a checkpoint is
inherently ambiguous: this prevents replay of *committed* successes, not exactly
once remote billing.
"""
from __future__ import annotations

import hashlib
import json
import time

from chaptera_translation_core.ports import ProviderRequest, TranslationCancelled

from .provider import ProviderFailure
from .storage import QueueStore

MAX_RESPONSE_BYTES = 2_000_000
MAX_STEP_LENGTH = 100


class CheckpointIntegrityError(ProviderFailure):
    """A checkpoint cannot be authenticated or disagrees with saved bytes."""


def provider_scope(provider: object) -> str:
    """Identify model independently of keys; prevent cross-model response reuse."""
    inner = getattr(provider, "provider", provider)
    model = getattr(inner, "model", None)
    if model:
        return "model:" + str(model)
    # Test/offline providers without model ID cannot share with real Gemini.
    cls = type(inner)
    return "offline:" + cls.__module__ + "." + cls.__qualname__


class SubrequestCheckpoints:
    """Single-worker SQLite checkpoint handle for one fenced job execution."""

    def __init__(self, store: QueueStore, job_id: str, lease_owner: str,
                 job_request_hash: str, model_scope: str):
        self.store = store
        self.job_id = job_id
        self.lease_owner = lease_owner
        self.job_request_hash = job_request_hash
        self.model_scope = model_scope
        self.saved_calls = 0
        self.reused_calls = 0
        with self.store._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS job_subrequest_checkpoints (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    step_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(job_id, step_key, request_fingerprint)
                )
            """)

    def _fingerprint(self, step: str, request: ProviderRequest) -> str:
        if not step or len(step) > MAX_STEP_LENGTH:
            raise ValueError("Invalid subrequest checkpoint step")
        exact = {
            "protocol": "subrequest_v1",
            "job_request_hash": self.job_request_hash,
            "model": self.model_scope,
            "step": step,
            "chapter_id": request.chapter_id,
            "transport": request.transport,
            "prompt": request.prompt,
            "source": request.source_html_with_markers,
            "unit_count": request.unit_count,
        }
        data = json.dumps(exact, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _assert_lease(self, con) -> None:
        row = con.execute(
            "SELECT status,lease_owner,cancel_requested,request_hash,input_json "
            "FROM jobs WHERE id=?", (self.job_id,),
        ).fetchone()
        if (row is None or row["status"] != "running"
                or row["lease_owner"] != self.lease_owner
                or row["cancel_requested"]):
            raise TranslationCancelled("Subrequest checkpoint lease lost or cancelled")
        if row["request_hash"] != self.job_request_hash:
            raise CheckpointIntegrityError("Job checkpoint identity changed")
        # The queue input is immutable by contract. A manual database edit
        # must not be able to graft checkpoints onto a different queued source.
        actual = hashlib.sha256(row["input_json"].encode("utf-8")).hexdigest()
        if actual != self.job_request_hash:
            raise CheckpointIntegrityError("Job input does not match its request fingerprint")

    def load(self, step: str, request: ProviderRequest) -> str | None:
        fingerprint = self._fingerprint(step, request)
        with self.store._connect() as con:
            self._assert_lease(con)
            row = con.execute("""
                SELECT response_text,response_sha256
                FROM job_subrequest_checkpoints
                WHERE job_id=? AND step_key=? AND request_fingerprint=?
            """, (self.job_id, step, fingerprint)).fetchone()
        if row is None:
            return None
        text = row["response_text"]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != row["response_sha256"]:
            raise CheckpointIntegrityError("Persisted subrequest checkpoint hash mismatch")
        self.reused_calls += 1
        return text

    def save(self, step: str, request: ProviderRequest, validated_response: str) -> None:
        if not isinstance(validated_response, str) or not validated_response.strip():
            raise CheckpointIntegrityError("Empty subrequest checkpoint response")
        if len(validated_response.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise CheckpointIntegrityError("Subrequest checkpoint response exceeds safety bound")
        fingerprint = self._fingerprint(step, request)
        digest = hashlib.sha256(validated_response.encode("utf-8")).hexdigest()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._assert_lease(con)
            existing = con.execute("""
                SELECT response_sha256 FROM job_subrequest_checkpoints
                WHERE job_id=? AND step_key=? AND request_fingerprint=?
            """, (self.job_id, step, fingerprint)).fetchone()
            if existing is not None:
                if existing["response_sha256"] != digest:
                    raise CheckpointIntegrityError("Subrequest checkpoint conflicts with existing result")
                return
            con.execute("""
                INSERT INTO job_subrequest_checkpoints
                (job_id,step_key,request_fingerprint,response_text,response_sha256,created_at)
                VALUES(?,?,?,?,?,?)
            """, (self.job_id, step, fingerprint, validated_response, digest, time.time()))
        self.saved_calls += 1
