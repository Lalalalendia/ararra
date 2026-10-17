"""Durable, transaction-atomic abuse guard for browser key exchange.

For a closed workspace, rate limit by the ASGI peer IP; never trust raw
X-Forwarded-For supplied by an unauthenticated client. The reverse proxy must
supply a trustworthy client address before Internet exposure.
"""
from __future__ import annotations

import hashlib
import math
import time

from .storage import QueueStore

WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 30
LOCK_AFTER_FAILURES = 8
LOCK_SECONDS = 15 * 60


class BrowserLoginThrottled(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__("Too many authentication attempts")


def _peer_digest(remote_ip: str | None) -> str:
    return hashlib.sha256(("readera:login:ip:" + str(remote_ip or "unknown")).encode()).hexdigest()


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

    def begin(self, remote_ip: str | None) -> None:
        """Charge every attempt before checking the credential, atomically."""
        now = time.time()
        peer = _peer_digest(remote_ip)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "DELETE FROM browser_login_rate WHERE updated_at<?",
                (now - 24 * 60 * 60,),
            )
            row = con.execute(
                "SELECT * FROM browser_login_rate WHERE peer_sha256=?",
                (peer,),
            ).fetchone()
            if row is not None and row["locked_until"] > now:
                raise BrowserLoginThrottled(math.ceil(row["locked_until"] - now))
            in_window = bool(row is not None and now - row["window_started"] < WINDOW_SECONDS)
            attempts = int(row["attempts"]) if in_window else 0
            failures = int(row["failures"]) if in_window else 0
            if attempts >= MAX_ATTEMPTS:
                raise BrowserLoginThrottled(math.ceil(
                    WINDOW_SECONDS - (now - row["window_started"])
                ))
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
                (peer, float(row["window_started"]) if in_window else now,
                 attempts + 1, failures, now),
            )

    def failure(self, remote_ip: str | None) -> None:
        now = time.time()
        peer = _peer_digest(remote_ip)
        with self.store._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT failures FROM browser_login_rate WHERE peer_sha256=?",
                (peer,),
            ).fetchone()
            if row is None:
                # A failed uncharged authentication must never bypass limits.
                raise RuntimeError("Browser login attempt not recorded")
            failures = int(row["failures"]) + 1
            lock = now + LOCK_SECONDS if failures >= LOCK_AFTER_FAILURES else 0
            con.execute(
                """UPDATE browser_login_rate SET failures=?,locked_until=?,updated_at=?
                   WHERE peer_sha256=?""",
                (failures, lock, now, peer),
            )

    def success(self, remote_ip: str | None) -> None:
        """Retain the attempt budget; success cannot reset flood protection."""
        with self.store._connect() as con:
            con.execute(
                """UPDATE browser_login_rate SET failures=0,locked_until=0,updated_at=?
                   WHERE peer_sha256=?""",
                (time.time(), _peer_digest(remote_ip)),
            )
