"""Shared SQLite-backed outbound site admission across API and source workers.

One active HTTP request per reviewed site (including host aliases), process-
shared minimum delay, UTC daily request budget, crash-expiring fenced leases
and a common upstream-429 cooldown. Never opens a connection itself.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from .source_catalog_med import IngestError
from .source_site_profiles import site_profile
from .storage import QueueStore

LEASE_SECONDS = 75.0  # HTTP transport hard-times-out each request at 40s.
MAX_WAIT_SECONDS = 12.0  # Do not tie up an API request or a worker indefinitely.


class SourceSiteBusy(IngestError):
    """Transient admission refusal, not evidence of an invalid source chapter."""

    def __init__(self, seconds: float, reason: str):
        self.retry_after = max(1, math.ceil(float(seconds)))
        self.reason = reason
        super().__init__("site_busy", f"Site temporarily unavailable ({reason}); retry later")


class SourceRequestLimiter:
    def __init__(self, store: QueueStore, *, wait_seconds: float = MAX_WAIT_SECONDS):
        if not 0 <= wait_seconds <= 60:
            raise ValueError("Source admission wait must be 0..60 seconds")
        self.store = store
        self.wait_seconds = float(wait_seconds)
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS source_site_requests (
                    source TEXT PRIMARY KEY,
                    utc_day INTEGER NOT NULL,
                    requests_today INTEGER NOT NULL,
                    next_allowed_at REAL NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until REAL NOT NULL DEFAULT 0,
                    cooldown_until REAL NOT NULL DEFAULT 0
                );
            """)

    @staticmethod
    def _checked(source: str, hostname: str, delay: float) -> float:
        profile = site_profile(source)
        if hostname not in profile.hosts:
            raise ValueError("Hostname is outside reviewed source profile")
        if not math.isfinite(delay) or delay < profile.minimum_delay_seconds or delay > 20:
            raise ValueError("Source pacing must respect profile and robots budgets")
        return float(delay)

    def reserve(self, source: str, hostname: str, *, delay: float) -> str:
        """Atomic across independent processes; caller must release by token."""
        delay = self._checked(source, hostname, delay)
        now = time.time()
        day = int(now // 86400)
        profile = site_profile(source)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM source_site_requests WHERE source=?", (source,),
            ).fetchone()
            used = int(row["requests_today"]) if row and row["utc_day"] == day else 0
            if row and row["utc_day"] > day:
                raise SourceSiteBusy(3600, "clock_regression")
            if row and row["cooldown_until"] > now:
                raise SourceSiteBusy(row["cooldown_until"] - now, "upstream_cooldown")
            if used >= profile.daily_requests:
                raise SourceSiteBusy((day + 1) * 86400 - now, "daily_site_budget")
            if row and row["lease_token"] and row["lease_until"] > now:
                raise SourceSiteBusy(row["lease_until"] - now, "inflight_request")
            if row and row["next_allowed_at"] > now:
                raise SourceSiteBusy(row["next_allowed_at"] - now, "request_interval")
            token = uuid.uuid4().hex
            con.execute(
                """INSERT INTO source_site_requests
                   (source,utc_day,requests_today,next_allowed_at,lease_token,lease_until,cooldown_until)
                   VALUES(?,?,?,?,?,?,0)
                   ON CONFLICT(source) DO UPDATE SET
                     utc_day=excluded.utc_day, requests_today=excluded.requests_today,
                     next_allowed_at=excluded.next_allowed_at,
                     lease_token=excluded.lease_token, lease_until=excluded.lease_until""",
                (source, day, used + 1, now + delay, token, now + LEASE_SECONDS),
            )
            return token

    def release(self, source: str, token: str) -> bool:
        """A stale worker cannot unlock the successor reservation."""
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute(
                "UPDATE source_site_requests SET lease_token=NULL,lease_until=0"
                " WHERE source=? AND lease_token=?", (source, token),
            ).rowcount
        return bool(changed)

    def cool_down(self, source: str, seconds: float) -> None:
        site_profile(source)
        if not 1 <= seconds <= 86400:
            raise ValueError("Source cooldown must be within one day")
        until = time.time() + seconds
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # A cooldown is only recorded after a reserved HTTP response.
            con.execute(
                "UPDATE source_site_requests SET cooldown_until=MAX(cooldown_until,?)"
                " WHERE source=?", (until,source),
            )

    @asynccontextmanager
    async def slot(self, source: str, hostname: str, *, delay: float):
        """Wait briefly for normal pacing, otherwise defer without spending quota."""
        started = time.monotonic()
        while True:
            try:
                token = self.reserve(source, hostname, delay=delay)
                break
            except SourceSiteBusy as exc:
                remaining = self.wait_seconds - (time.monotonic() - started)
                if exc.retry_after > remaining:
                    raise
                await asyncio.sleep(exc.retry_after)
        try:
            yield
        finally:
            self.release(source, token)
