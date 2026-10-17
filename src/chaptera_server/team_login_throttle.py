"""Atomic SQLite login admission for team keys and moderator passwords.

Rate limits per ASGI peer and, for password login, per member identifier.
Do not trust unverified X-Forwarded-For: proxy/client identity is an ops gate.
"""
from __future__ import annotations

import hashlib
import math
import time

from .storage import QueueStore

WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 30
LOCK_AFTER_FAILURES = 8
MAX_MEMBER_ATTEMPTS = 10
LOCK_AFTER_MEMBER_FAILURES = 6
LOCK_SECONDS = 15 * 60


class BrowserLoginThrottled(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__("Too many authentication attempts")


def _digest(domain: str, identifier: str) -> str:
    return hashlib.sha256(
        ("readera:login:" + domain + ":" + identifier).encode("utf-8")
    ).hexdigest()


def _keys(remote_ip: str | None, member_id: str | None = None):
    result = [(_digest("ip", str(remote_ip or "unknown")),
               MAX_ATTEMPTS, LOCK_AFTER_FAILURES)]
    if member_id is not None:
        result.append((_digest("member", member_id),
                       MAX_MEMBER_ATTEMPTS, LOCK_AFTER_MEMBER_FAILURES))
    return result


class BrowserLoginThrottle:
    def __init__(self, store: QueueStore):
        self.store = store
        with store._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS browser_login_rate (
                    peer_sha256 TEXT PRIMARY KEY,
                    window_started REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    failures INTEGER NOT NULL,
                    locked_until REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_browser_login_rate_updated
                    ON browser_login_rate(updated_at);
            """)

    def begin(self, remote_ip: str | None, member_id: str | None = None) -> None:
        """Charge BOTH counters before checking credentials; fail atomically."""
        now = time.time()
        keys = _keys(remote_ip, member_id)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "DELETE FROM browser_login_rate WHERE updated_at<?",
                (now - 24 * 60 * 60,),
            )
            prepared = []
            for key, maximum, _failures in keys:
                row = con.execute(
                    "SELECT * FROM browser_login_rate WHERE peer_sha256=?",
                    (key,),
                ).fetchone()
                if row is not None and row["locked_until"] > now:
                    raise BrowserLoginThrottled(math.ceil(row["locked_until"] - now))
                in_window = bool(row is not None and now - row["window_started"] < WINDOW_SECONDS)
                attempts = int(row["attempts"]) if in_window else 0
                failures = int(row["failures"]) if in_window else 0
                if attempts >= maximum:
                    raise BrowserLoginThrottled(math.ceil(
                        WINDOW_SECONDS - (now - row["window_started"])
                    ))
                prepared.append((key, float(row["window_started"]) if in_window else now,
                                 attempts + 1, failures, now))
            for params in prepared:
                con.execute(
                    """INSERT INTO browser_login_rate
                       (peer_sha256,window_started,attempts,failures,locked_until,updated_at)
                       VALUES(?,?,?,?,0,?)
                       ON CONFLICT(peer_sha256) DO UPDATE SET
                         window_started=excluded.window_started,
                         attempts=excluded.attempts,
                         failures=excluded.failures,
                         locked_until=0,
                         updated_at=excluded.updated_at""",
                    params,
                )

    def failure(self, remote_ip: str | None, member_id: str | None = None) -> None:
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for key, _max, threshold in _keys(remote_ip, member_id):
                row = con.execute(
                    "SELECT failures FROM browser_login_rate WHERE peer_sha256=?",
                    (key,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Browser login attempt not recorded")
                failures = int(row["failures"]) + 1
                lock = now + LOCK_SECONDS if failures >= threshold else 0
                con.execute(
                    """UPDATE browser_login_rate
                       SET failures=?,locked_until=?,updated_at=? WHERE peer_sha256=?""",
                    (failures, lock, now, key),
                )

    def success(self, remote_ip: str | None, member_id: str | None = None) -> None:
        """Success clears failures but NOT attempts, preventing budget reset."""
        now = time.time()
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for key, _max, _threshold in _keys(remote_ip, member_id):
                con.execute(
                    """UPDATE browser_login_rate
                       SET failures=0,locked_until=0,updated_at=?
                       WHERE peer_sha256=?""",
                    (now, key),
                )
