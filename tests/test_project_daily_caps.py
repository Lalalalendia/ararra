"""RE-06C: explicit personal-project RPD caps, paid-call gates and batch admission.

All provider calls are synthetic. No Google HTTP, permanent services or secrets
are used in these tests.
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.chapter_batches import BatchConflict
from chaptera_server.config import Settings
from chaptera_server.member_gemini_provider import MemberGeminiProviders
from chaptera_server.personal_gemini_keys import (
    GeminiKeyMissing, PersonalGeminiKeys,
)
from chaptera_server.quota import DailyQuotaExhausted, DailyQuotaLedger
from chaptera_server.storage import QueueStore

MASTER = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
HEADERS = {
    "alice": {"X-API-Key": "alice-local-key"},
    "bob": {"X-API-Key": "bob-local-key"},
}
KEYS = {
    "alice": "AIzaTEST_alice_project_budget_0123456",
    "bob": "AIzaTEST_bob_project_budget_987654321",
}


def setup(tmp_path, host_limit=0):
    settings = Settings(
        db_path=tmp_path / "readera.sqlite3",
        team_tokens={key: value["X-API-Key"] for key, value in HEADERS.items()},
        gemini_vault_key=MASTER,
        gemini_requests_per_day=host_limit,
        gemini_model="test-model",
        gemini_rpm=0, gemini_tpm=0, gemini_reserved_output_tokens=0,
        browser_session_secure=False,
    )
    store = QueueStore(settings.db_path)
    app = create_app(settings, store)
    return settings, store, app, app.state.personal_gemini_keys


def test_only_own_project_cap_can_be_set_and_read_without_google_calls(tmp_path):
    _, store, app, keys = setup(tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test",
        ) as client:
            keys.put("alice", "alice-project-001", KEYS["alice"])
            keys.put("bob", "bob-project-002", KEYS["bob"])
            empty = await client.get("/v1/me/gemini-keys", headers=HEADERS["alice"])
            assert empty.json()["projects"][0]["daily_request_cap"] is None
            assert empty.json()["projects"][0]["local_quota"] is None

            target="/v1/me/gemini-keys/alice-project-001/daily-cap"
            assert (await client.put(target, json={"daily_request_cap":3})).status_code==401
            assert (await client.put(target, headers=HEADERS["bob"],
                                     json={"daily_request_cap":3})).status_code==404
            for invalid in (0, -1, 1000001, "x"):
                denied=await client.put(target, headers=HEADERS["alice"],
                                       json={"daily_request_cap":invalid})
                assert denied.status_code==422
            allowed=await client.put(target, headers=HEADERS["alice"],
                                     json={"daily_request_cap":3})
            assert allowed.status_code==200
            assert allowed.json()["daily_request_cap"]==3
            assert "no-store" in allowed.headers["cache-control"]

            listed=await client.get("/v1/me/gemini-keys",headers=HEADERS["alice"])
            assert listed.status_code==200
            one=listed.json()["projects"][0]
            assert one["daily_request_cap"]==3
            assert one["local_quota"]["limit"]==3
            assert one["local_quota"]["remaining"]==3
            assert one["local_quota"]["provider_remaining_known"] is False
            assert "bob-project" not in listed.text
            assert KEYS["alice"] not in listed.text
            assert (await client.get("/v1/me/gemini-keys",
                        headers=HEADERS["bob"])).json()["projects"][0]["daily_request_cap"] is None

            DailyQuotaLedger(store,"alice-project-001","test-model",3).reserve()
            changed=await client.get("/v1/me/gemini-keys",headers=HEADERS["alice"])
            assert changed.json()["projects"][0]["local_quota"]["used"]==1
            assert changed.json()["projects"][0]["local_quota"]["remaining"]==2
            deleted=await client.delete("/v1/me/gemini-keys/alice-project-001",
                                        headers=HEADERS["alice"])
            assert deleted.status_code==204
            with store._connect() as con:
                assert con.execute(
                    "SELECT 1 FROM member_gemini_budgets WHERE project_id=?",
                    ("alice-project-001",),
                ).fetchone() is None
    asyncio.run(run())


def test_browser_cookie_mutation_requires_csrf(tmp_path):
    _, _, app, keys = setup(tmp_path)
    keys.put("alice", "alice-project-001", KEYS["alice"])

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test",
        ) as client:
            login=await client.post("/v1/session",json={"api_key":HEADERS["alice"]["X-API-Key"]})
            assert login.status_code==201
            target="/v1/me/gemini-keys/alice-project-001/daily-cap"
            denied=await client.put(target,json={"daily_request_cap":2})
            assert denied.status_code==403
            good=await client.put(target,headers={
                "X-ReadEra-CSRF":login.json()["csrf_token"],
            },json={"daily_request_cap":2})
            assert good.status_code==200
            assert keys.daily_cap_for("alice","alice-project-001")==2
    asyncio.run(run())


def test_missing_cap_never_sends_and_lowered_limit_stops_cached_provider(tmp_path, monkeypatch):
    settings, store, _, keys = setup(tmp_path)
    keys.put("alice","alice-project-001",KEYS["alice"])
    sent=[]

    class FakeGemini:
        def __init__(self, key, model, **_kwargs):
            self.key=key
            self.completed_calls=0
        async def generate(self, request):
            sent.append((self.key,request.prompt))
            self.completed_calls+=1
            return "fake response"

    monkeypatch.setattr("chaptera_server.member_gemini_provider.GeminiProvider",FakeGemini)
    providers=MemberGeminiProviders(store, settings, keys)
    with pytest.raises(GeminiKeyMissing,match="daily cap"):
        providers.for_job({"actor_id":"alice"})
    assert sent==[]
    keys.set_daily_cap("alice","alice-project-001",3)
    provider=providers.for_job({"actor_id":"alice"})
    request=SimpleNamespace(prompt="synthetic",request_role="translation")
    assert asyncio.run(provider.generate(request))=="fake response"
    assert len(sent)==1
    # This is the SAME cached provider, not a new job; cap lowering is checked
    # in the SQLite reservation transaction immediately before network IO.
    keys.set_daily_cap("alice","alice-project-001",1)
    with pytest.raises(DailyQuotaExhausted):
        asyncio.run(provider.generate(request))
    assert len(sent)==1
    assert DailyQuotaLedger(store,"alice-project-001","test-model",1).status()["used"]==1
    # A new call after raising the cap is permitted with the new per-job wrapper.
    keys.set_daily_cap("alice","alice-project-001",2)
    provider2=providers.for_job({"actor_id":"alice"})
    assert asyncio.run(provider2.generate(request))=="fake response"
    assert len(sent)==2
    with pytest.raises(DailyQuotaExhausted):
        asyncio.run(provider2.generate(request))
    assert len(sent)==2


def test_host_ceiling_is_never_relaxed_by_member_cap(tmp_path, monkeypatch):
    settings, store, _, keys=setup(tmp_path,host_limit=1)
    keys.put("alice","alice-project-001",KEYS["alice"])
    keys.set_daily_cap("alice","alice-project-001",21)
    attempts=[]
    class FakeGemini:
        def __init__(self,*_args,**_kwargs): pass
        async def generate(self,_request):
            attempts.append(1)
            return "ok"
    monkeypatch.setattr("chaptera_server.member_gemini_provider.GeminiProvider",FakeGemini)
    provider=MemberGeminiProviders(store,settings,keys).for_job({"actor_id":"alice"})
    request=SimpleNamespace(prompt="bounded")
    assert asyncio.run(provider.generate(request))=="ok"
    with pytest.raises(DailyQuotaExhausted):
        asyncio.run(provider.generate(request))
    assert attempts==[1]
    # Returning to the cabinet is NOT a way to override an ops limit.
    keys.set_daily_cap("alice","alice-project-001",100)
    with pytest.raises(DailyQuotaExhausted):
        asyncio.run(provider.generate(request))


def test_batch_admission_fails_closed_but_idempotent_replay_survives_revocation(tmp_path):
    _, store, app, keys=setup(tmp_path)
    library=app.state.library
    library.create_book("novel","Novel",owner_member_id="alice",actor_id="alice")
    library.put_chapter("novel","c1",chapter_index=1,chapter_path="chapters/1.xhtml",
                        source_html="<p>original</p>",expected_revision=0)
    payload={
        "expected_glossary_revision":0,
        "chapters":[{"chapter_id":"c1","expected_source_revision":1}],
        "source_language":"auto","target_language":"Russian",
        "style_instructions":"","max_attempts":3,"transport":"html_v0",
    }
    batches=app.state.translation_batches
    sha=batches.preview("novel",payload)["manifest_sha256"]
    confirmed={**payload,"expected_manifest_sha256":sha}
    with pytest.raises(BatchConflict,match="daily request cap"):
        batches.submit("novel",confirmed,actor_id="alice",idempotency_key="one")
    keys.put("alice","alice-project-001",KEYS["alice"])
    with pytest.raises(BatchConflict,match="daily request cap"):
        batches.submit("novel",confirmed,actor_id="alice",idempotency_key="one")
    assert store.list_jobs()==[]
    keys.set_daily_cap("alice","alice-project-001",1)
    batch=batches.submit("novel",confirmed,actor_id="alice",idempotency_key="one")
    assert batch["chapter_count"]==1
    keys.delete("alice","alice-project-001")
    replay=batches.submit("novel",confirmed,actor_id="alice",idempotency_key="one")
    assert replay["batch_id"]==batch["batch_id"]
    assert replay["chapters"][0]["job_id"]==batch["chapters"][0]["job_id"]
    with pytest.raises(BatchConflict,match="daily request cap"):
        batches.submit("novel",confirmed,actor_id="alice",idempotency_key="new")
    assert len(store.list_jobs())==1


def test_project_selection_uses_remaining_local_daily_allowance(tmp_path):
    settings, store, _, keys=setup(tmp_path)
    keys.put("alice","alice-project-001",KEYS["alice"])
    keys.put("alice","alice-project-003","AIzaTEST_alice_extra_project_budget_987654321")
    keys.set_daily_cap("alice","alice-project-001",1)
    keys.set_daily_cap("alice","alice-project-003",2)
    DailyQuotaLedger(store,"alice-project-001","test-model",1).reserve()
    chooser=MemberGeminiProviders(store,settings,keys)
    selected=chooser.for_job({"actor_id":"alice"})
    assert selected.project=="alice-project-003"
    # A second project is explicitly blocked by Google despite unused local
    # count: provider choice must not waste the next chapter on that project.
    DailyQuotaLedger(store,"alice-project-003","test-model",2).block_day()
    # All projects exhausted; choose an exhausted one to trigger normal
    # Pacific-day deferral, never to overspend beyond the local ceiling.
    assert chooser.for_job({"actor_id":"alice"}).project in {
        "alice-project-001","alice-project-003",
    }
