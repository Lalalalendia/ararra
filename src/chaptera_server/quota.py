"""Durable project+model Gemini RPD safety gate.

Google applies API rate limits per PROJECT, not per API key. Every
`generateContent` attempt (preflight, translation, atom packet, repair)
reserves one unit BEFORE contacting the provider; unknown network outcomes
are intentionally counted. This local cap cannot measure calls made outside
this SQLite worker and does not replace remote RPM/TPM or billing limits.
"""
from __future__ import annotations

import re
import time
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


class DailyQuotaExhausted(RuntimeError):
    def __init__(self, reset_at: float):
        super().__init__("Gemini daily request allowance exhausted")
        self.reset_at = reset_at


class DailyQuotaLedger:
    """Atomic ledger shared by every key/worker using the same project + DB."""

    def __init__(self, store: QueueStore, project: str, model: str, limit: int):
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", project):
            raise ValueError("Explicit non-secret Gemini project quota scope required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
            raise ValueError("Invalid Gemini quota model")
        if not 1 <= limit <= 1_000_000:
            raise ValueError("Gemini requests-per-day cap must be between 1 and 1000000")
        self.store, self.project, self.model, self.limit = store, project, model, limit
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
        moment = time.time() if now is None else now
        day, reset = pacific_window(moment)
        with self.store._connect() as con:
            row = con.execute("""
                SELECT used, externally_blocked FROM gemini_daily_requests
                WHERE project=? AND model=? AND pacific_day=?
            """, (self.project, self.model, day)).fetchone()
        used = int(row["used"]) if row else 0
        blocked = bool(row["externally_blocked"]) if row else False
        return {
            "model": self.model, "pacific_day": day,
            "used": used, "limit": self.limit,
            "remaining": 0 if blocked else max(0, self.limit - used),
            "reset_at": reset, "externally_blocked": blocked,
        }

    def reserve(self, *, now: float | None = None) -> dict:
        """Commit one conservative attempted call in SQLite before network IO."""
        moment = time.time() if now is None else now
        day, reset = pacific_window(moment)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("""
                SELECT used, externally_blocked FROM gemini_daily_requests
                WHERE project=? AND model=? AND pacific_day=?
            """, (self.project, self.model, day)).fetchone()
            if row and (row["externally_blocked"] or row["used"] >= self.limit):
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

    async def generate(self, request):
        self.ledger.reserve()
        self.sent_calls += 1
        try:
            return await self.provider.generate(request)
        except DailyProviderQuotaFailure as exc:
            reset = self.ledger.block_day()
            raise DailyQuotaExhausted(reset) from exc
