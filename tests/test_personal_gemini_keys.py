"""Personal Gemini keys: encryption, actor isolation, API, and worker dispatch."""
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.member_gemini_provider import MemberGeminiProviders
from chaptera_server.personal_gemini_keys import (
    GeminiKeyConflict, GeminiKeyMissing, GeminiVaultUnavailable, PersonalGeminiKeys,
)
from chaptera_server.provider import ProviderFailure
from chaptera_server.storage import JobSpec, QueueStore
from chaptera_server.team_credentials import TeamCredentials
from chaptera_server.worker import TranslationWorker

MASTER = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
OTHER_MASTER = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
KEY_ALICE = "AIzaTEST_personal_alice_key_0123456789"
KEY_BOB = "AIzaTEST_personal_bob_key_0123456789"
KEY_ALICE2 = "AIzaTEST_personal_alice_second_project_key"
TEAM = {
    "chief": "test-admin-api-key",
    "alice": "test-alice-api-key",
    "bob": "test-bob-api-key",
    "guest": "test-guest-api-key",
}


def settings(tmp_path, master=MASTER):
    return Settings(
        db_path=tmp_path / "readera.sqlite3",
        team_tokens=TEAM,
        admin_member_id="chief",
        gemini_vault_key=master,
        gemini_rpm=0,
        gemini_tpm=0,
        gemini_reserved_output_tokens=0,
        browser_session_secure=False,
    )


def test_private_api_cabinet_and_secret_not_retrievable(tmp_path):
    app = create_app(settings(tmp_path))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as alice, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as bob:
            assert (await alice.get("/v1/me/gemini-keys")).status_code == 401
            assert (await alice.get("/v1/me/gemini-keys",
                                    headers={"X-API-Key": TEAM["alice"]})).json() == {
                "configured": True, "projects": [],
            }
            login = await alice.post("/v1/session", json={"api_key": TEAM["alice"]})
            assert login.status_code == 201
            # The browser carries a Secure/HttpOnly cookie; no CSRF header
            # means even an authenticated request cannot change a key.
            no_csrf = await alice.post("/v1/me/gemini-keys", json={
                "project_id": "alice-project-001", "api_key": KEY_ALICE,
            })
            assert no_csrf.status_code == 403
            csrf = login.json()["csrf_token"]
            malformed = await alice.post("/v1/me/gemini-keys",
                                         headers={"X-ReadEra-CSRF": csrf},
                                         json={"project_id": "alice-project-001",
                                               "api_key": "private-bad-secret"})
            assert malformed.status_code == 422
            assert "private-bad-secret" not in malformed.text
            saved = await alice.post("/v1/me/gemini-keys",
                                     headers={"X-ReadEra-CSRF": csrf},
                                     json={"project_id": "alice-project-001",
                                           "api_key": KEY_ALICE})
            assert saved.status_code == 201, saved.text
            assert "no-store" in saved.headers["cache-control"]
            assert "api_key" not in saved.json()
            assert KEY_ALICE not in saved.text
            listed = await alice.get("/v1/me/gemini-keys")
            assert listed.status_code == 200
            assert [p["project_id"] for p in listed.json()["projects"]] == ["alice-project-001"]
            assert KEY_ALICE not in listed.text
            assert KEY_ALICE.encode() not in settings(tmp_path).db_path.read_bytes()

            with sqlite3.connect(settings(tmp_path).db_path) as con:
                keyrow = con.execute(
                    "SELECT encrypted_key,nonce,key_fingerprint FROM member_gemini_keys"
                ).fetchone()
                assert keyrow and KEY_ALICE.encode() not in bytes(keyrow[0])
                assert bytes(keyrow[1]) != b""
                assert KEY_ALICE not in keyrow[2]

            assert (await bob.get("/v1/me/gemini-keys",
                                  headers={"X-API-Key": TEAM["bob"]})).json()["projects"] == []
            stolen = await bob.delete("/v1/me/gemini-keys/alice-project-001",
                                      headers={"X-API-Key": TEAM["bob"]})
            assert stolen.status_code == 404
            stolen = await bob.post("/v1/me/gemini-keys",
                headers={"X-API-Key": TEAM["bob"]},
                json={"project_id": "alice-project-001", "api_key": KEY_BOB})
            assert stolen.status_code == 409

            duplicate = await bob.post("/v1/me/gemini-keys",
                headers={"X-API-Key": TEAM["bob"]},
                json={"project_id": "bob-project-002", "api_key": KEY_ALICE})
            assert duplicate.status_code == 409
            ok = await bob.post("/v1/me/gemini-keys",
                headers={"X-API-Key": TEAM["bob"]},
                json={"project_id": "bob-project-002", "api_key": KEY_BOB})
            assert ok.status_code == 201

            removed = await alice.delete("/v1/me/gemini-keys/alice-project-001",
                                         headers={"X-ReadEra-CSRF": csrf})
            assert removed.status_code == 204
            assert (await alice.get("/v1/me/gemini-keys")).json()["projects"] == []

    asyncio.run(run())


def test_encrypt_aad_rotation_restart_and_missing_master(tmp_path):
    conf = settings(tmp_path)
    store = QueueStore(conf.db_path)
    secrets = PersonalGeminiKeys(store, MASTER)
    info = secrets.put("alice", "alice-project-001", KEY_ALICE)
    assert info["project_id"] == "alice-project-001"
    first_fingerprint = secrets.choices("alice")[0][1]
    assert secrets.resolve("alice", "alice-project-001", first_fingerprint) == KEY_ALICE
    assert PersonalGeminiKeys(QueueStore(conf.db_path), MASTER).resolve(
        "alice", "alice-project-001", first_fingerprint
    ) == KEY_ALICE

    # A different host encryption key fails closed and cannot leak the original.
    with pytest.raises(GeminiVaultUnavailable):
        PersonalGeminiKeys(store, OTHER_MASTER).resolve(
            "alice", "alice-project-001", first_fingerprint
        )
    with pytest.raises(GeminiKeyMissing):
        secrets.resolve("bob", "alice-project-001", first_fingerprint)

    secrets.put("alice", "alice-project-001", KEY_ALICE2)
    rotated = secrets.choices("alice")[0][1]
    assert rotated != first_fingerprint
    assert not secrets.is_current("alice", "alice-project-001", first_fingerprint)
    with pytest.raises(GeminiKeyMissing):
        secrets.resolve("alice", "alice-project-001", first_fingerprint)
    assert secrets.resolve("alice", "alice-project-001", rotated) == KEY_ALICE2
    secrets.delete("alice", "alice-project-001")
    assert secrets.choices("alice") == []


def test_master_missing_does_not_accept_plaintext_credentials(tmp_path):
    conf = settings(tmp_path, "")
    app = create_app(conf)
    assert not app.state.personal_gemini_keys.configured

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readera.test"
        ) as client:
            no_vault = await client.get("/v1/me/gemini-keys",
                                        headers={"X-API-Key": TEAM["alice"]})
            assert no_vault.json()["configured"] is False
            denied = await client.post("/v1/me/gemini-keys",
                                      headers={"X-API-Key": TEAM["alice"]},
                                      json={"project_id": "alice-project-001",
                                            "api_key": KEY_ALICE})
            assert denied.status_code == 503
            assert KEY_ALICE not in denied.text
            with sqlite3.connect(conf.db_path) as con:
                assert con.execute("SELECT count(*) FROM member_gemini_keys").fetchone()[0] == 0
    asyncio.run(run())


def test_existing_sqlite_backlog_only_claims_mods_with_personal_keys(tmp_path):
    conf = settings(tmp_path)
    store = QueueStore(conf.db_path)
    keys = PersonalGeminiKeys(store, MASTER)
    TeamCredentials(store)
    unready = store.create_job("guest", JobSpec("guest-01", "<p>x</p>"))
    work = store.create_job("alice", JobSpec("ready-01", "<p>x</p>"))
    # No personal credentials yet: worker must not reserve jobs or attempts.
    assert store.claim_next(require_personal_key=True) is None
    assert store.get_job(unready["id"])["attempts"] == 0
    keys.put("alice", "alice-project-001", KEY_ALICE)
    # Having a credential alone does not authorize unbounded paid calls.
    assert store.claim_next(require_personal_key=True) is None
    keys.set_daily_cap("alice", "alice-project-001", 21)
    chosen, lease = store.claim_next(require_personal_key=True)
    assert chosen["id"] == work["id"]
    assert chosen["actor_id"] == "alice"
    assert store.get_job(unready["id"])["status"] == "queued"
    store.complete(work["id"], lease, "<p>ok</p>", "test", 1)
    assert store.claim_next(require_personal_key=True) is None


def test_per_member_provider_isolation_rotation_and_no_paid_keyless_job(tmp_path, monkeypatch):
    conf = settings(tmp_path)
    store = QueueStore(conf.db_path)
    keys = PersonalGeminiKeys(store, MASTER)
    keys.put("alice", "alice-project-001", KEY_ALICE)
    keys.put("bob", "bob-project-002", KEY_BOB)
    keys.put("alice", "alice-project-003", KEY_ALICE2)
    for member, project in (
        ("alice", "alice-project-001"),
        ("bob", "bob-project-002"),
        ("alice", "alice-project-003"),
    ):
        keys.set_daily_cap(member, project, 21)
    used = []

    class FakeGemini:
        def __init__(self, key, model, **kwargs):
            self.key = key
            self.model = model
            self.completed_calls = 0

        async def generate(self, request):
            used.append(self.key)
            self.completed_calls += 1
            return request.source_html_with_markers.replace("世界正在燃烧。", "Мир горит.")

    monkeypatch.setattr("chaptera_server.member_gemini_provider.GeminiProvider", FakeGemini)
    router = MemberGeminiProviders(store, conf, keys)
    pending_guest = store.create_job("guest", JobSpec("guest-1", "<p>世界正在燃烧。</p>"))
    jobs = [
        store.create_job(owner, JobSpec(f"c-{i}", "<p>世界正在燃烧。</p>"))
        for i, owner in enumerate(("alice", "alice", "bob"))
    ]

    async def run():
        worker = TranslationWorker(store, router)
        assert await worker.run_once()
        assert await worker.run_once()
        assert await worker.run_once()
        assert not await worker.run_once()

    asyncio.run(run())
    assert used == [KEY_BOB, KEY_ALICE, KEY_ALICE2] or used == [
        KEY_ALICE, KEY_BOB, KEY_ALICE2
    ]  # Queue fairness may interleave actor batches.
    assert all(store.get_job(job["id"])["status"] == "completed" for job in jobs)
    assert store.get_job(pending_guest["id"])["attempts"] == 0

    # A cached per-project provider cannot send another request after deletion.
    before = len(used)
    active = router.for_job({"actor_id": "alice"})
    keys.delete("alice", active.project)
    with pytest.raises(GeminiKeyMissing):
        asyncio.run(active.generate(object()))
    assert len(used) == before


def test_deleted_key_during_claim_preserves_queue_retry_budget(tmp_path, monkeypatch):
    conf = settings(tmp_path)
    store = QueueStore(conf.db_path)
    keys = PersonalGeminiKeys(store, MASTER)
    keys.put("alice", "alice-project-001", KEY_ALICE)
    keys.set_daily_cap("alice", "alice-project-001", 21)
    router = MemberGeminiProviders(store, conf, keys)
    job = store.create_job("alice", JobSpec("one", "<p>x</p>"))
    def lose_key(_job):
        keys.delete("alice", "alice-project-001")
        raise GeminiKeyMissing("Credential removed before claim")
    monkeypatch.setattr(router, "for_job", lose_key)
    assert asyncio.run(TranslationWorker(store, router).run_once())
    info = store.get_job(job["id"])
    assert info["status"] == "retry_wait"
    assert info["attempts"] == 0
    assert store.claim_next(require_personal_key=True) is None
