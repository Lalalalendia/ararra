#!/usr/bin/env python3
"""M1j: explicit localhost proxy client-IP admission and shared Python/Rust lock.

Simulates a correctly overwriting local reverse proxy by sending the dedicated
X-ReadEra-Client-IP header. This is NOT an actual Nginx or VPS acceptance test.
The header is trusted only when the kernel TCP peer is loopback and opt-in=1.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import httpx

from compare_rust_api import launch, ready
from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import QueueStore


IP_A = "198.51.100.10"
IP_B = "203.0.113.20"
IP_V6 = "2001:db8::17"
ADMIN_KEY = "m1j-chief"
MOD_KEY = "m1j-mod"


def require_status(resp: httpx.Response, status: int, reason: str) -> None:
    # No token or response body is ever added to CI error messages.
    assert resp.status_code == status, (
        f"{reason}: expected {status}, got {resp.status_code}"
    )


def proxies(ip: str, forwarded: str | None = None) -> dict[str, str]:
    return {
        "X-ReadEra-Client-IP": ip,
        "X-Forwarded-For": forwarded or "10.10.10.10",
    }


async def exercise(app, db_path: Path, rust_url: str) -> None:
    async with (
        httpx.AsyncClient(base_url=rust_url, timeout=15) as rust,
        httpx.AsyncClient(
            base_url="http://python",
            transport=httpx.ASGITransport(app=app, client=(IP_A, 50001)),
        ) as python_a,
        httpx.AsyncClient(
            base_url="http://python",
            transport=httpx.ASGITransport(app=app, client=(IP_B, 50002)),
        ) as python_b,
    ):
        endpoint = "/v1/session"
        # A trusted proxy must SET one strict header, not forward arbitrary XFF.
        for headers in (
            {},
            {"X-Forwarded-For": IP_A},
            {"X-ReadEra-Client-IP": ""},
            {"X-ReadEra-Client-IP": "garbage"},
            {"X-ReadEra-Client-IP": f"{IP_A}, {IP_B}"},
            {"X-ReadEra-Client-IP": "0.0.0.0"},
            {"X-ReadEra-Client-IP": "198.51.100.1:443"},
        ):
            missing = await rust.post(
                endpoint, headers=headers, json={"api_key": MOD_KEY},
            )
            require_status(missing, 400, "proxy malformed or missing identity")
            assert missing.json()["detail"] == "Trusted client IP unavailable"
            assert "set-cookie" not in missing.headers

        duplicates = await rust.post(
            endpoint,
            headers=[
                ("X-ReadEra-Client-IP", IP_A),
                ("X-ReadEra-Client-IP", IP_B),
            ],
            json={"api_key": MOD_KEY},
        )
        require_status(duplicates, 400, "duplicate identity not accepted")
        with sqlite3.connect(db_path) as db:
            assert db.execute(
                "SELECT COUNT(*) FROM browser_login_rate"
            ).fetchone()[0] == 0, "rejected proxy identities must not spend login slots"

        # One valid local proxy identity creates an independent shared bucket.
        a_success = await rust.post(
            endpoint, headers=proxies(IP_A, IP_B),
            json={"api_key": MOD_KEY},
        )
        require_status(a_success, 201, "Rust login at identity A")
        assert a_success.json()["member_id"] == "mod"
        assert a_success.cookies.get("readera_session")
        b_success = await rust.post(
            endpoint, headers=proxies(IP_B, IP_A),
            json={"api_key": MOD_KEY},
        )
        require_status(b_success, 201, "Rust login at identity B")
        assert b_success.json()["member_id"] == "mod"

        # Validate the actual data keys: same Python/Rust IP hash scheme,
        # no new row for a spoofed X-Forwarded-For prefix.
        def ip_digest(ip: str) -> str:
            return hashlib.sha256(
                ("readera:login:ip:" + ip).encode("utf-8"),
            ).hexdigest()

        with sqlite3.connect(db_path) as db:
            entries = db.execute(
                "SELECT peer_sha256,attempts,failures FROM browser_login_rate"
            ).fetchall()
            assert {item[0] for item in entries} == {
                ip_digest(IP_A), ip_digest(IP_B)
            }
            assert all(row[1] == 1 and row[2] == 0 for row in entries)

        # Alternate Python ASGI and Rust login attempts with the SAME source IP,
        # proving shared rate-limit accounting and exact 8-failure lockout.
        # Misleading XFF values are ignored by the Rust adapter.
        for attempt in range(8):
            if attempt % 2 == 0:
                failed = await rust.post(
                    endpoint,
                    headers=proxies(IP_A, f"203.0.113.{100 + attempt}"),
                    json={"api_key": "wrong-key"},
                )
            else:
                failed = await python_a.post(
                    endpoint, json={"api_key": "wrong-key"},
                )
            require_status(failed, 401, "invalid login increments bucket A")
        blocked_rust = await rust.post(
            endpoint, headers=proxies(IP_A, IP_B),
            json={"api_key": ADMIN_KEY},
        )
        require_status(blocked_rust, 429, "Rust A lockout")
        assert int(blocked_rust.headers["retry-after"]) >= 1
        blocked_python = await python_a.post(
            endpoint, json={"api_key": ADMIN_KEY},
        )
        require_status(blocked_python, 429, "Python observes same A lockout")
        assert int(blocked_python.headers["retry-after"]) >= 1

        # A spoofed XFF field cannot evade lockout by claiming to be B.
        require_status(await rust.post(
            endpoint, headers=proxies(IP_A, IP_B),
            json={"api_key": MOD_KEY},
        ), 429, "spoofed XFF does not bypass A lockout")

        b_after_lock = await rust.post(
            endpoint, headers=proxies(IP_B, IP_A),
            json={"api_key": MOD_KEY},
        )
        require_status(b_after_lock, 201, "legitimate B is not blocked by A")
        require_status(await python_b.post(
            endpoint, json={"api_key": MOD_KEY},
        ), 201, "Python B is also independent")

        # An IPv6 client must be accepted without concatenated values or a port.
        v6 = await rust.post(
            endpoint, headers=proxies(IP_V6),
            json={"api_key": MOD_KEY},
        )
        require_status(v6, 201, "IPv6 proxy identity")
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                "SELECT peer_sha256,attempts,failures,locked_until "
                "FROM browser_login_rate"
            ).fetchall()
            assert len(rows) == 3
            lookups = {row[0]: row for row in rows}
            assert set(lookups) == {
                ip_digest(IP_A), ip_digest(IP_B), ip_digest(IP_V6)
            }
            assert lookups[ip_digest(IP_A)][2] == 8
            assert lookups[ip_digest(IP_A)][3] > 0
            assert lookups[ip_digest(IP_B)][2] == 0
            assert lookups[ip_digest(IP_V6)][2] == 0
        contents = db_path.read_bytes()
        for raw in (IP_A, IP_B, IP_V6, MOD_KEY, ADMIN_KEY):
            assert raw.encode() not in contents

        print("rust_proxy_missing_and_malformed_header_fail_closed=PASS")
        print("rust_proxy_spoofed_xff_and_duplicate_rejected=PASS")
        print("rust_proxy_python_rust_shared_ip_rate_limit=PASS")
        print("rust_proxy_independent_ipv4_and_ipv6_buckets=PASS")
        print("rust_proxy_no_cleartext_credentials_or_ips_on_disk=PASS")
        print("nginx_live_configuration_verified=NO")
        print("production_sqlite_access=NO")
        print("gemini_calls=0")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_proxy_login_ip.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-m1j-proxy-") as directory:
        root = Path(directory)
        path = root / "disposable.sqlite3"
        settings = Settings(
            db_path=path,
            team_tokens={"chief": ADMIN_KEY, "mod": MOD_KEY},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, QueueStore(path))
        env = dict(os.environ)
        for key in (
            "CREDENTIALS_DIRECTORY",
            "CHAPTERA_GEMINI_API_KEY",
            "CHAPTERA_GEMINI_VAULT_KEY",
        ):
            env.pop(key, None)
        env.update({
            "CHAPTERA_DB_PATH": str(path),
            "CHAPTERA_TEAM_TOKENS_JSON": json.dumps(settings.team_tokens),
            "CHAPTERA_ADMIN_MEMBER_ID": "chief",
            "CHAPTERA_BROWSER_SESSION_SECURE": "0",
            "ERIDA_RUST_ADDR": "127.0.0.1:18491",
            "ERIDA_RUST_TEST_WRITES": "1",
            "ERIDA_RUST_DISPOSABLE_ROOT": str(root),
            "ERIDA_RUST_TRUST_LOOPBACK_PROXY_CLIENT_IP": "1",
        })
        process = launch([binary], env)
        try:
            ready("http://127.0.0.1:18491", process)
            asyncio.run(exercise(app, path, "http://127.0.0.1:18491"))
            assert process.poll() is None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
