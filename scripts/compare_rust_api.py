#!/usr/bin/env python3
"""Exact read-route parity against the existing Python API, then CI-only RSS.

No production DB, network source, Gemini, Google Drive or live credentials.
The Rust prototype is not a replacement for the authenticated write API.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.storage import JobSpec, QueueStore


def launch(command: list[str], environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ready(url: str, process: subprocess.Popen) -> None:
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError(f"Disposable local server exited with {process.returncode}")
        try:
            if httpx.get(url + "/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Disposable local server did not start")


def resident(pid: int) -> dict[str, str]:
    values = {"rss_mib": "unavailable", "pss_mib": "unavailable", "hwm_mib": "unavailable"}
    status = Path(f"/proc/{pid}/status")
    if status.is_file():
        for row in status.read_text(encoding="utf-8").splitlines():
            if row.startswith("VmRSS:"):
                values["rss_mib"] = str((int(row.split()[1]) + 1023) // 1024)
            if row.startswith("VmHWM:"):
                values["hwm_mib"] = str((int(row.split()[1]) + 1023) // 1024)
    smaps = Path(f"/proc/{pid}/smaps_rollup")
    try:
        for row in smaps.read_text(encoding="utf-8").splitlines():
            if row.startswith("Pss:"):
                values["pss_mib"] = str((int(row.split()[1]) + 1023) // 1024)
                break
    except (OSError, ValueError):
        pass
    return values


async def run_parity(
    app, rust_base: str, job_id: str, managed_token: str, cookie: str, settings: Settings
) -> None:
    python_transport = httpx.ASGITransport(app=app)
    admin = {"X-API-Key": settings.team_tokens["chief"]}
    mod = {"X-API-Key": settings.team_tokens["mod"]}
    managed = {"X-API-Key": managed_token}
    browser = {"Cookie": "readera_session=" + cookie}
    bad = {"X-API-Key": "invalid-secret"}
    invalid_wins = {**browser, **bad}
    paths = [
        "/healthz",
        "/v1/me",
        "/v1/admin/members",
        "/v1/jobs",
        "/v1/jobs?limit=1",
        "/v1/jobs?limit=0",
        "/v1/jobs?limit=1000",
        "/v1/jobs/" + job_id,
        "/v1/jobs/" + job_id + "/events",
        "/v1/jobs/non-existent",
        "/v1/jobs/non-existent/events",
    ]
    count = 0
    async with (
        httpx.AsyncClient(transport=python_transport, base_url="http://python") as python,
        httpx.AsyncClient(base_url=rust_base, timeout=5) as rust,
    ):
        async def comparison(path: str, headers: dict[str, str]) -> None:
            nonlocal count
            original = await python.get(path, headers=headers)
            candidate = await rust.get(path, headers=headers)
            if original.status_code != candidate.status_code or original.json() != candidate.json():
                # Deliberately do not leak tokens, session values, book text or responses.
                raise AssertionError(
                    f"API parity failed on {path}: "
                    f"python={original.status_code} rust={candidate.status_code}"
                )
            count += 1

        for headers in ({}, admin, mod, managed, browser, bad, invalid_wins):
            for path in paths:
                await comparison(path, headers)

        # Rotate a static moderator credential and ensure session invalidation
        # and header precedence immediately match in both processes.
        # Administrative browser sessions can read but never leak secrets.
        admin_cookie = app.state.team_sessions.issue(
            member_id="chief", team_key=settings.team_tokens["chief"],
            ttl_seconds=3600,
        )
        await comparison("/v1/admin/members", {
            "Cookie": "readera_session=" + admin_cookie,
        })
        await comparison("/v1/admin/members", {
            "Cookie": "readera_session=" + admin_cookie,
            "X-API-Key": "invalid-secret",
        })
        rotated = app.state.team_credentials.rotate("chief", "mod", settings)
        await comparison("/v1/me", mod)
        await comparison("/v1/me", browser)
        await comparison("/v1/me", {"X-API-Key": rotated["access_token"]})
        await comparison("/v1/admin/members", admin)
        app.state.team_credentials.disable("chief", "new_mod", settings)
        await comparison("/v1/me", managed)
        await comparison("/v1/admin/members", admin)
        app.state.team_sessions.revoke(cookie)
        await comparison("/v1/jobs", browser)

        # This is intentionally a read-only API; POST is not implemented.
        readonly = await rust.post("/v1/jobs", headers=admin, json={"chapter_id": "never"})
        assert readonly.status_code == 405, readonly.status_code
    print(f"rust_api_parity_cases={count}")
    print("rust_api_parity=PASS")
    print("rust_api_mutation_routes=disabled")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_api.py /path/to/erida-api")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    with tempfile.TemporaryDirectory(prefix="erida-rust-compare-") as folder:
        db_path = Path(folder) / "isolated.sqlite3"
        store = QueueStore(db_path)
        settings = Settings(
            db_path=db_path,
            team_tokens={"chief": "benchmark-chief-key", "mod": "benchmark-mod-key"},
            admin_member_id="chief",
            browser_session_secure=False,
        )
        app = create_app(settings, store)
        job = store.create_job(
            "mod",
            JobSpec(chapter_id="chapter-1", source_html="<p>Synthetic source only.</p>"),
        )
        store.create_job(
            "mod",
            JobSpec(chapter_id="chapter-2", source_html="<p>Synthetic second source.</p>"),
        )
        managed_token = app.state.team_credentials.issue("chief", "new_mod", settings)["access_token"]
        cookie = app.state.team_sessions.issue(
            member_id="mod", team_key=settings.team_tokens["mod"], ttl_seconds=3600
        )
        env = dict(os.environ)
        env.pop("CREDENTIALS_DIRECTORY", None)
        env.pop("CHAPTERA_GEMINI_API_KEY", None)
        env.pop("CHAPTERA_GEMINI_VAULT_KEY", None)
        env["CHAPTERA_DB_PATH"] = str(db_path)
        env["CHAPTERA_TEAM_TOKENS_JSON"] = json.dumps(settings.team_tokens)
        env["CHAPTERA_ADMIN_MEMBER_ID"] = "chief"
        env["CHAPTERA_BROWSER_SESSION_SECURE"] = "0"
        env["ERIDA_RUST_ADDR"] = "127.0.0.1:18471"
        rust = launch([binary], env)
        python = None
        try:
            ready("http://127.0.0.1:18471", rust)
            asyncio.run(run_parity(
                app, "http://127.0.0.1:18471", job["id"], managed_token, cookie, settings
            ))
            python = launch(
                [sys.executable, "-m", "uvicorn", "chaptera_server.api:create_default_app",
                 "--factory", "--host", "127.0.0.1", "--port", "18472"],
                env,
            )
            ready("http://127.0.0.1:18472", python)
            # Same warm-up HTTP read workload; Rust supports only a subset of
            # the Python API, so these are experimental, NOT equal-feature metrics.
            for _ in range(8):
                for url in ("http://127.0.0.1:18471", "http://127.0.0.1:18472"):
                    assert httpx.get(url + "/healthz", timeout=5).status_code == 200
                    assert httpx.get(
                        url + "/v1/jobs", headers={"X-API-Key": settings.team_tokens["chief"]},
                        timeout=5,
                    ).status_code == 200
            for prefix, process in (("rust_readonly", rust), ("python_full", python)):
                assert process.poll() is None
                for key, value in resident(process.pid).items():
                    print(f"benchmark_{prefix}_{key}={value}")
            print("benchmark_scope=idle_plus_authenticated_get_jobs")
            print("benchmark_feature_equivalence=NO_python_api_has_write_and_book_routes")
            print("production_processes_touched=0")
        finally:
            for process in (python, rust):
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
