"""RE-02: independent actors, SQLite site limits, and background backpressure.

No test sends third-party network traffic or uses copyrighted prose.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import time

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_catalog_med import validate_url
from chaptera_server.source_collect import catalog_from_html
from chaptera_server.source_job_store import (
    SourceJobStore, SourceJobConflict, MAX_ACTIVE_JOBS_PER_SITE,
)
from chaptera_server.source_job_worker import SourceCollectionWorker
from chaptera_server.source_request_limiter import (
    SourceRequestLimiter, SourceSiteBusy, LEASE_SECONDS,
)
from chaptera_server.source_site_http import PublicHtmlFetcher
from chaptera_server.source_site_profiles import SITE_PROFILES, site_profile
from chaptera_server.storage import QueueStore

BOOK = "https://www.royalroad.com/fiction/142007/aura"
TOC = '<div id="chapters">' + "".join(
    f'<a href="/fiction/142007/aura/chapter/{60000+n}/{n}-part">{n} - Part {n}</a>'
    for n in (1, 2, 3)
) + "</div>"


def catalog():
    return catalog_from_html("royalroad", BOOK, TOC)


def test_profiles_exposed_only_after_team_auth(tmp_path):
    assert set(SITE_PROFILES) == {"royalroad", "webnovel", "uukanshu"}
    for p in SITE_PROFILES.values():
        assert p.minimum_delay_seconds >= 1.5
        assert p.daily_requests <= 240
        assert all("*" not in host for host in p.hosts)
        assert site_profile(p.name) is p
    with pytest.raises(ValueError):
        validate_url("https://127.0.0.1/book/1", "royalroad")
    app = create_app(Settings(db_path=tmp_path/"api.db",
                              team_tokens={"alice":"alice-key"}))
    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            assert (await client.get("/v1/source-books/sites")).status_code == 401
            response = await client.get("/v1/source-books/sites",
                                        headers={"X-API-Key":"alice-key"})
            assert response.status_code == 200
            sites = response.json()["sources"]
            assert {x["id"] for x in sites} == set(SITE_PROFILES)
            assert sites[0]["access"] == "public_html_subject_to_robots"
    asyncio.run(verify())


def test_independent_sqlite_handles_share_site_aliases_and_fence_expired_lease(tmp_path,monkeypatch):
    from chaptera_server import source_request_limiter as admission
    now = [1_800_000_000.0]
    monkeypatch.setattr(admission.time, "time", lambda: now[0])
    db = tmp_path/"shared.db"
    first = SourceRequestLimiter(QueueStore(db),wait_seconds=0)
    other = SourceRequestLimiter(QueueStore(db),wait_seconds=0)
    token1 = first.reserve("royalroad","www.royalroad.com",delay=4)
    with pytest.raises(SourceSiteBusy) as err:
        other.reserve("royalroad","royalroad.com",delay=4)
    assert err.value.reason == "inflight_request"
    assert first.release("royalroad",token1)
    with pytest.raises(SourceSiteBusy) as err:
        other.reserve("royalroad","royalroad.com",delay=4)
    assert err.value.reason == "request_interval"
    now[0] += 4
    token2 = other.reserve("royalroad","royalroad.com",delay=4)
    assert not first.release("royalroad",token1)
    now[0] += LEASE_SECONDS+4
    token3 = first.reserve("royalroad","www.royalroad.com",delay=4)
    assert not other.release("royalroad",token2), "stale lease must not unlock successor"
    assert first.release("royalroad",token3)


def test_parallel_connections_can_admit_exactly_one_request(tmp_path):
    db = tmp_path/"race.db"
    SourceRequestLimiter(QueueStore(db))
    def contender(_):
        limiter = SourceRequestLimiter(QueueStore(db),wait_seconds=0)
        try:
            return ("ok",limiter.reserve("royalroad","royalroad.com",delay=4))
        except SourceSiteBusy as exc:
            return ("busy",exc.reason)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(contender,range(8)))
    assert len([r for r in results if r[0] == "ok"]) == 1,results
    assert all(r[0] == "ok" or r[1] == "inflight_request" for r in results)


def test_utc_daily_budget_and_site_cooldown_survive_reopening(tmp_path,monkeypatch):
    from chaptera_server import source_request_limiter as admission
    now = [1_800_000_000.0]
    monkeypatch.setattr(admission.time,"time",lambda:now[0])
    p = site_profile("royalroad")
    monkeypatch.setitem(SITE_PROFILES,"royalroad",dataclasses.replace(p,daily_requests=2))
    db = tmp_path/"daily.db"
    limiter = SourceRequestLimiter(QueueStore(db),wait_seconds=0)
    for _ in range(2):
        token = limiter.reserve("royalroad","royalroad.com",delay=4)
        assert limiter.release("royalroad",token)
        now[0] += 4
    with pytest.raises(SourceSiteBusy) as err:
        SourceRequestLimiter(QueueStore(db),wait_seconds=0).reserve(
            "royalroad","www.royalroad.com",delay=4)
    assert err.value.reason == "daily_site_budget"
    assert 1 <= err.value.retry_after <= 86400
    now[0] = ((int(now[0]//86400)+1)*86400)+1
    token = limiter.reserve("royalroad","royalroad.com",delay=4)
    limiter.cool_down("royalroad",180)
    assert limiter.release("royalroad",token)
    with pytest.raises(SourceSiteBusy) as err:
        limiter.reserve("royalroad","royalroad.com",delay=4)
    assert err.value.reason == "upstream_cooldown"
    now[0] += 181
    assert limiter.release("royalroad",limiter.reserve("royalroad","royalroad.com",delay=4))


def test_fetcher_uses_shared_db_for_robots_and_remote_429(tmp_path,monkeypatch):
    from chaptera_server import source_request_limiter as admission
    now = [1_800_000_000.0]
    monkeypatch.setattr(admission.time,"time",lambda:now[0])
    repo = QueueStore(tmp_path/"network.db")
    calls = []
    def handler(req):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return httpx.Response(
                200,text="User-agent: *\nAllow: /",
                headers={"content-type":"text/plain"},
            )
        return httpx.Response(429,headers={"Retry-After":"240"})
    fetcher = PublicHtmlFetcher(
        transport_factory=lambda:httpx.MockTransport(handler),
        request_limiter=SourceRequestLimiter(repo,wait_seconds=0),
    )
    async def go():
        with pytest.raises(SourceSiteBusy):
            await fetcher.fetch("royalroad",BOOK)
        assert calls == ["/robots.txt"],"chapter request needs same-site pacing"
        now[0] += 4
        with pytest.raises(SourceSiteBusy) as throttled:
            await fetcher.fetch("royalroad",BOOK)
        assert throttled.value.reason == "upstream_429"
        assert calls == ["/robots.txt","/fiction/142007/aura"]
        other = SourceRequestLimiter(QueueStore(repo.db_path),wait_seconds=0)
        with pytest.raises(SourceSiteBusy) as shared:
            other.reserve("royalroad","royalroad.com",delay=4)
        assert shared.value.reason == "upstream_cooldown"
    asyncio.run(go())


def test_round_robin_across_actors_and_queued_books_is_persistent(tmp_path):
    repo = QueueStore(tmp_path/"fair.db")
    jobs = SourceJobStore(repo)
    cat = catalog()
    actors = ("alice","alice","alice","bob","charlie")
    created = [jobs.create(actor,"catalog",cat,[1,2,3])["job_id"] for actor in actors]
    owners = dict(zip(created,actors))
    claimed = []
    for i in range(8):
        reopened = SourceJobStore(QueueStore(repo.db_path))
        item = reopened.claim()
        assert item
        claimed.append(owners[item["job_id"]])
        reopened.finish(item["job_id"],item["lease_token"],body=(f"Unique text {i} "*18))
    assert claimed[:5] == ["alice","bob","charlie","alice","bob"]
    assert jobs.get("bob",created[3])["completed"] >= 1


def test_per_site_queue_cap_and_idempotent_retry(tmp_path):
    jobs = SourceJobStore(QueueStore(tmp_path/"caps.db"))
    cat = catalog()
    first = jobs.create("moderator0","cat",cat,[1],idempotency_key="same")
    assert jobs.create("moderator0","cat",cat,[1],idempotency_key="same")["job_id"] == first["job_id"]
    for i in range(1,MAX_ACTIVE_JOBS_PER_SITE):
        jobs.create(f"moderator{i}","cat",cat,[1])
    with pytest.raises(SourceJobConflict,match="source site already"):
        jobs.create("last","cat",cat,[1])
    assert jobs.get("moderator0",first["job_id"])["status"] == "queued"


def test_site_busy_defers_background_job_without_failure(tmp_path):
    repo = QueueStore(tmp_path/"deferred.db")
    jobs = SourceJobStore(repo)
    item = jobs.create("alice","cat",catalog(),[1])
    class Busy:
        async def fetch(self,_source,_url):
            raise SourceSiteBusy(120,"daily_site_budget")
    assert asyncio.run(SourceCollectionWorker(jobs,Busy()).run_once())
    state = jobs.get("alice",item["job_id"])
    assert state["status"] == "queued"
    assert state["completed"] == state["failed"] == 0
    assert state["manual_retries"] == 0
    assert state["waiting_reason"] == "daily_site_budget"
    assert state["next_attempt_at"] > time.time()
    assert jobs.claim() is None
    with repo._connect() as con:
        con.execute("UPDATE source_collect_jobs SET ready_at=0 WHERE id=?",
                    (item["job_id"],))
    class Good:
        async def fetch(self,_source,_url):
            return '<div class="chapter-content"><p>'+"A new test chapter. "*25+"</p></div>"
    assert asyncio.run(SourceCollectionWorker(jobs,Good()).run_once())
    done = jobs.get("alice",item["job_id"])
    assert (done["status"],done["failed"],done["completed"]) == ("complete",0,1)


def test_old_source_schema_migrates_additively(tmp_path):
    repo = QueueStore(tmp_path/"legacy.db")
    with repo._connect() as con:
        con.execute("""CREATE TABLE source_collect_jobs (
            id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, catalog_id TEXT NOT NULL,
            idempotency_key TEXT, source TEXT NOT NULL, book_url TEXT NOT NULL,
            catalog_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
            manifest_json TEXT NOT NULL, status TEXT NOT NULL, lease_token TEXT,
            lease_expires_at REAL, current_index INTEGER, manual_retries INTEGER DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(actor_id,idempotency_key))""")
    SourceJobStore(repo)
    with repo._connect() as con:
        fields = {x["name"] for x in con.execute("PRAGMA table_info(source_collect_jobs)")}
    assert {"ready_at","dispatch_turn","defer_reason"} <= fields
