"""Stage verified source-job chapters in chosen immutable order.

No HTTP framework, network requests or direct BookLibrary commits.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from hashlib import sha256

from .source_job_store import SourceJobConflict


def stage_verified_source_job(
    jobs, sessions, catalog_store, actor: str, job_id: str, *,
    start_ordinal: int | None = None,
    end_ordinal: int | None = None,
    start_position: int | None = None,
    end_position: int | None = None,
) -> dict:
    state = jobs.get(actor,job_id)
    if state["selection_mode"] == "ordered":
        if (start_ordinal is not None or end_ordinal is not None or
            start_position is None or end_position is None or
            end_position < start_position or
            end_position - start_position + 1 > 20 or
            end_position > state["total"]):
            raise ValueError("Choose 1..20 ordered selection positions for staging")
        lo,hi = start_position,end_position
        selected=state["selected_ordinals"][lo-1:hi]
        hints=list(range(lo,hi+1))
    else:
        if (start_position is not None or end_position is not None or
            start_ordinal is None or end_ordinal is None or
            end_ordinal < start_ordinal or
            end_ordinal - start_ordinal + 1 > 20):
            raise ValueError("Stage no more than 20 contiguous chapters")
        selected=list(range(start_ordinal,end_ordinal+1))
        lo,hi = start_ordinal,end_ordinal
        hints=None
    collected = jobs.materialize(actor,job_id,selected)
    payload = collected.as_import_html()
    # Idempotent exact-byte and position fence across browser retries.
    fingerprint = sha256(payload).hexdigest()[:32]
    key = f"source-job:{job_id}:{lo}:{hi}:{fingerprint}"
    stage = sessions.stage(
        actor, f"verified-{job_id[:8]}-{lo}-{hi}.html",
        payload,idempotency_key=key[:128],suggested_positions=hints,
    )
    details = [asdict(ch)|{"body":None, "destination_position":hints[i] if hints else None}
               for i,ch in enumerate(collected.chapters)]
    store=catalog_store
    try:
        store.save_stage_receipt(actor,state["catalog_id"],stage["stage_id"],
                                 state["catalog_sha256"],details)
    except sqlite3.IntegrityError:
        old=store.stage_receipt(actor,stage["stage_id"])
        if (old["catalog_id"] != state["catalog_id"] or
            old["catalog_sha256"] != state["catalog_sha256"] or
            old["chapters"] != details):
            raise SourceJobConflict("Source stage receipt changed")
    return {
        "status":"staged", "job_id":job_id, "stage_id":stage["stage_id"],
        "collected_count":len(collected.chapters),
        "file_sha256":stage["file_sha256"],
    }
