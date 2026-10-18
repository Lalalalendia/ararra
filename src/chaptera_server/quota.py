"""Durable project+model Gemini RPD safety gate.

Google applies API rate limits per PROJECT, not per API key. Every
`generateContent` attempt (preflight, translation, atom packet, repair)
reserves one unit BEFORE contacting the provider; unknown network outcomes
are intentionally counted. This local cap cannot measure calls made outside
this SQLite worker and does not replace remote RPM/TPM or billing limits.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Callable
from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo

from .provider import DailyProviderQuotaFailure
from .storage import QueueStore

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_window(instant: float) -> tuple[str, float]:
    """Day label and UNIX timestamp of the next local midnight, DST-safe."""
    local = datetime.fromtimestamp(instant, PACIFIC)
    following = local.date() + timedelta(days=1)
    return local.date().isoformat(), datetime.combine(
        following, clock_time.min, tzinfo=PACIFIC
    ).timestamp()


def read_daily_quota_status(store: QueueStore, project: str, model: str,
                            limit: int, *, now: float | None = None) -> dict:
    """Read the local RPD ledger without creating tables or reserving requests.

    This is a *local attempt count*, not the provider's complete project usage
    or a billable token meter. The caller must not return the project ID.
    """
    moment = time.time() if now is None else now
    day, reset = pacific_window(moment)
    with store._connect() as con:
        exists = con.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='gemini_daily_requests'
        """).fetchone()
        row = con.execute("""
            SELECT used, externally_blocked FROM gemini_daily_requests
            WHERE project=? AND model=? AND pacific_day=?
        """, (project, model, day)).fetchone() if exists else None
    used = int(row["used"]) if row else 0
    blocked = bool(row["externally_blocked"]) if row else False
    return {
        "model": model, "pacific_day": day,
        "used": used, "limit": limit,
        "remaining": 0 if blocked else max(0, limit - used),
        "reset_at": reset, "externally_blocked": blocked,
    }


class DailyQuotaExhausted(RuntimeError):
    def __init__(self, reset_at: float):
        super().__init__("Gemini daily request allowance exhausted")
        self.reset_at = reset_at


class DailyQuotaLedger:
    """Atomic ledger shared by every key/worker using the same project + DB."""

    def __init__(self, store: QueueStore, project: str, model: str, limit: int,
                 *, limit_reader: Callable[[sqlite3.Connection], int] | None = None):
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", project):
            raise ValueError("Explicit non-secret Gemini project quota scope required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
            raise ValueError("Invalid Gemini quota model")
        if not 1 <= limit <= 1_000_000:
            raise ValueError("Gemini requests-per-day cap must be between 1 and 1000000")
        self.store, self.project, self.model, self.limit = store, project, model, limit
        self.limit_reader = limit_reader
        with self.store._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS gemini_daily_requests (
                    project TEXT NOT NULL,
                    model TEXT NOT NULL,
                    pacific_day TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
                    externally_blocked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(project, model, pacific_day)
                )
            """)

    def status(self, *, now: float | None = None) -> dict:
        with self.store._connect() as con:
            current_limit = (self.limit_reader(con) if self.limit_reader
                             else self.limit)
        return read_daily_quota_status(
            self.store, self.project, self.model, max(0, current_limit), now=now
        )

    def reserve(self, *, now: float | None = None) -> dict:
        """Commit one conservative attempted call in SQLite before network IO."""
        moment = time.time() if now is None else now
        day, reset = pacific_window(moment)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # Read the live per-project cap UNDER the same write lock that
            # reserves an attempt. Lowering/revoking a cap cannot race a
            # cached in-flight provider and allow an extra paid call.
            limit = self.limit_reader(con) if self.limit_reader else self.limit
            if not 1 <= limit <= self.limit:
                # A dynamic cap may be 0 when the project has been removed.
                # Externally configured global ceilings remain a hard maximum.
                if limit <= 0:
                    con.execute("ROLLBACK")
                    raise DailyQuotaExhausted(reset)
                limit = min(limit, self.limit)
            row = con.execute("""
                SELECT used, externally_blocked FROM gemini_daily_requests
                WHERE project=? AND model=? AND pacific_day=?
            """, (self.project, self.model, day)).fetchone()
            if row and (row["externally_blocked"] or row["used"] >= limit):
                con.execute("ROLLBACK")
                raise DailyQuotaExhausted(reset)
            con.execute("""
                INSERT INTO gemini_daily_requests(project,model,pacific_day,used)
                VALUES(?,?,?,1)
                ON CONFLICT(project,model,pacific_day)
                DO UPDATE SET used=used+1
            """, (self.project, self.model, day))
            con.execute("COMMIT")
        return self.status(now=moment)

    def block_day(self, *, now: float | None = None) -> float:
        """The provider reported real daily RPD/TPD exhaustion before local cap."""
        moment = time.time() if now is None else now
        day, reset = pacific_window(moment)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("""
                INSERT INTO gemini_daily_requests(project,model,pacific_day,used,externally_blocked)
                VALUES(?,?,?,0,1)
                ON CONFLICT(project,model,pacific_day)
                DO UPDATE SET externally_blocked=1
            """, (self.project, self.model, day))
            con.execute("COMMIT")
        return reset


class QuotaControlledProvider:
    """Intercept EVERY request, including context preflight and repair calls."""

    def __init__(self, provider, ledger: DailyQuotaLedger):
        self.provider, self.ledger = provider, ledger
        self.sent_calls = 0
        self.completed_calls = 0

    async def generate(self, request):
        self.ledger.reserve()
        self.sent_calls += 1
        try:
            result = await self.provider.generate(request)
            self.completed_calls += 1
            return result
        except DailyProviderQuotaFailure as exc:
            reset = self.ledger.block_day()
            raise DailyQuotaExhausted(reset) from exc
