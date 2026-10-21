"""Separate public-source worker; never uses Gemini credentials or library writes."""
from __future__ import annotations

import asyncio
import logging
import hashlib

from .source_catalog_med import IngestError
from .source_request_limiter import SourceSiteBusy
from .source_collect import MAX_BODY_CHARS
from .source_chapter_parts import fetch_complete_chapter
from .source_job_store import SourceJobStore, SourceJobConflict, MAX_ITEM_BYTES

log = logging.getLogger(__name__)


class SourceCollectionWorker:
    def __init__(self, jobs: SourceJobStore, fetcher, *, lease_seconds: float = 180.0):
        self.jobs = jobs
        self.fetcher = fetcher
        self.lease_seconds = lease_seconds

    async def run_once(self, *, only_job_id: str | None = None) -> bool:
        claim = self.jobs.claim(
            lease_seconds=self.lease_seconds, only_job_id=only_job_id,
        )
        if claim is None:
            return False
        job_id = claim["job_id"]
        token = claim["lease_token"]
        ch = claim["chapter"]
        try:
            body = await fetch_complete_chapter(
                claim["source"], claim["book_url"], ch["url"],
                lambda url: self.fetcher.fetch(claim["source"], url),
            )
            if not 100 <= len(body) <= MAX_BODY_CHARS:
                raise IngestError("too_short", "Source chapter has invalid character length")
            if len(body.encode("utf-8")) > MAX_ITEM_BYTES:
                raise IngestError("too_large", "Source chapter exceeds durable size limit")
            result = self.jobs.finish(job_id,token,body=body)
            log.info("source chapter checkpoint saved: job=%s ordinal=%s %s/%s",
                     job_id,ch["ordinal"],result["completed"],result["total"])
        except SourceSiteBusy as exc:
            # An upstream throttle or our own site budget is NOT a corrupt
            # chapter: release the job lease and retry later, without burning
            # a manual retry pass or deleting any verified checkpoint.
            try:
                self.jobs.defer(job_id, token, retry_after=exc.retry_after,
                                reason=exc.reason)
            except SourceJobConflict:
                log.warning("lost source job lease during site deferral: %s", job_id)
        except (IngestError, ValueError) as exc:
            # A source's login/CAPTCHA/locked text is a failure receipt,
            # not a valid chapter. Explicit actor retry is required.
            try:
                self.jobs.finish(job_id,token,error=f"{type(exc).__name__}: {str(exc)[:145]}")
            except SourceJobConflict:
                log.warning("lost source job lease before failure receipt: %s",job_id)
        # Unexpected process/socket/storage errors are NOT acknowledged; after
        # the lease expires, the exact unfinished chapter is safely reclaimed.
        return True

    async def run_forever(self, *, poll_seconds: float = 2.0,
                          stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                log.exception("source collection worker tick failed; lease will expire")
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(stop.wait(),timeout=max(0.2,poll_seconds))
            except asyncio.TimeoutError:
                pass
