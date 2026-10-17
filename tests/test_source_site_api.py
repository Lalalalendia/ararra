"""RE-02 public-site browser/API collector acceptance without external traffic."""
from __future__ import annotations

import asyncio

import httpcore
import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_site_http import (
    PublicHtmlFetcher, PinnedPublicNetworkBackend, is_public_ip,
)
from chaptera_server.storage import QueueStore

BOOK="https://www.royalroad.com/fiction/142007/aura"
TOC="""<div id="chapters">
<a href="/fiction/142007/aura/chapter/60001/1-first">1 - First chapter</a>
<a href="/fiction/142007/aura/chapter/60002/2-second">2 - Second chapter</a>
</div>"""
ONE='<div class="chapter-content"><p>'+("First hero walks. "*25)+'</p><p>Глава 999. Это текст, не заголовок</p></div>'
TWO='<div class="chapter-content"><p>'+("Second hero arrives. "*25)+'</p></div>'
AUTH={"X-API-Key":"alice-key"}
BOB={"X-API-Key":"bob-key"}


class FakeSite:
    def __init__(self, *, bad_second=False):
        self.calls=[]
        self.bad_second=bad_second

    async def fetch(self, source, url):
        self.calls.append((source,url))
        if url==BOOK:
            return TOC
        if "/60001/" in url:
            return ONE
        if "/60002/" in url:
            return "<h1>Just a moment, verify you are human</h1>" if self.bad_second else TWO
        raise AssertionError("Unexpected URL: "+url)


def test_ip_boundary_rejects_private_mixed_and_rebinding():
    for address in ("127.0.0.1","10.0.0.2","192.168.1.1","169.254.0.1",
                    "0.0.0.0","::1","fc00::1","::ffff:127.0.0.1","100.64.0.1"):
        assert not is_public_ip(address),address
    assert is_public_ip("8.8.8.8")

    class Spy:
        def __init__(self): self.hosts=[]
        async def connect_tcp(self,host,port,**kwargs):
            self.hosts.append(host)
            return object()

    async def scenario():
        spy=Spy()
        async def good(host,port): return ("8.8.8.8","1.1.1.1")
        async def mixed(host,port): return ("8.8.8.8","127.0.0.1")
        backend=PinnedPublicNetworkBackend(backend=spy,resolver=good)
        await backend.connect_tcp("royalroad.com",443)
        assert spy.hosts==["8.8.8.8"],spy.hosts
        backend=PinnedPublicNetworkBackend(backend=spy,resolver=mixed)
        with pytest.raises(httpcore.ConnectError,match="not public"):
            await backend.connect_tcp("royalroad.com",443)
        assert spy.hosts==["8.8.8.8"]
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_unix_socket("/tmp/trap")
    asyncio.run(scenario())


def test_network_rejects_redirect_and_oversize_without_chapter_text(monkeypatch):
    from chaptera_server import source_site_http as site
    monkeypatch.setattr(site,"MIN_REQUEST_INTERVAL",0)
    def handler(request):
        if request.url.path=="/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /",headers={"content-type":"text/plain"})
        if request.url.path.endswith("/1-first"):
            return httpx.Response(302, headers={"location":"http://127.0.0.1/admin"})
        return httpx.Response(200,content=b"x"*(2*1024*1024+1),headers={"content-type":"text/html"})
    fetcher=PublicHtmlFetcher(transport_factory=lambda:httpx.MockTransport(handler))
    async def scenario():
        from chaptera_server.source_catalog_med import IngestError
        with pytest.raises(IngestError,match="302"):
            await fetcher.fetch("royalroad","https://royalroad.com/fiction/142007/aura/chapter/60001/1-first")
        with pytest.raises(IngestError,match="size limit"):
            await fetcher.fetch("royalroad","https://royalroad.com/fiction/142007/aura/chapter/60002/2-second")
    asyncio.run(scenario())


def test_network_robots_disallow_rejects_without_page_fetch(monkeypatch):
    from chaptera_server import source_site_http as site
    monkeypatch.setattr(site,"MIN_REQUEST_INTERVAL",0)
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,text="User-agent: *\nDisallow: /fiction/",headers={"content-type":"text/plain"})
    fetcher=PublicHtmlFetcher(transport_factory=lambda:httpx.MockTransport(handler))
    async def scenario():
        from chaptera_server.source_catalog_med import IngestError
        with pytest.raises(IngestError,match="robots"):
            await fetcher.fetch("royalroad",BOOK)
    asyncio.run(scenario())
    assert calls==["/robots.txt"]


def test_site_toc_to_real_import_stage_to_book_and_provenance(tmp_path):
    db=QueueStore(tmp_path/"site.sqlite3")
    app=create_app(Settings(db_path=db.db_path,team_tokens={"alice":"alice-key","bob":"bob-key"}),db)
    fake=FakeSite()
    app.state.public_source_fetcher=fake

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            assert (await client.get("/v1/source-books/sites")).status_code==401
            site=(await client.get("/v1/source-books/sites",headers=AUTH))
            assert site.status_code==200
            d=await client.post("/v1/source-books/catalogs",headers=AUTH,json={
                "source":"royalroad","book_url":BOOK})
            assert d.status_code==201,d.text
            cat=d.json()
            assert cat["chapter_count"]==2
            key=cat["catalog_id"]
            assert (await client.get(f"/v1/source-books/catalogs/{key}",headers=BOB)).status_code==404
            assert (await client.post(f"/v1/source-books/catalogs/{key}/collect",headers=BOB,
                json={"ordinals":[1],"expected_catalog_sha256":cat["catalog_sha256"]})).status_code==404
            stale=await client.post(f"/v1/source-books/catalogs/{key}/collect",headers=AUTH,json={
                "ordinals":[1],"expected_catalog_sha256":"0"*64})
            assert stale.status_code==409
            out=await client.post(f"/v1/source-books/catalogs/{key}/collect",headers=AUTH,json={
                "ordinals":[2,1],"expected_catalog_sha256":cat["catalog_sha256"]})
            assert out.status_code==201,out.text
            assert out.json()["status"]=="staged"
            stage_id=out.json()["stage_id"]
            view=await client.get(f"/v1/imports/stages/{stage_id}",headers=AUTH)
            assert view.json()["preview"]["chapter_count"]==2,view.text
            provenance=await client.get(f"/v1/source-books/stages/{stage_id}/provenance",headers=AUTH)
            assert provenance.status_code==200
            assert [x["ordinal"] for x in provenance.json()["chapters"]]==[1,2]
            assert "First hero" not in provenance.text
            payload={
                "book_id":"site-demo","book_mode":"create","title":"Из сайта",
                "prefix_policy":"reject","chapters":[
                    {"sequence":1,"action":"create","chapter_id":"c1",
                     "chapter_index":1,"chapter_path":"chapters/1.xhtml","expected_revision":0},
                    {"sequence":2,"action":"create","chapter_id":"c2",
                     "chapter_index":2,"chapter_path":"chapters/2.xhtml","expected_revision":0},
                ],
            }
            committed=await client.post(f"/v1/imports/stages/{stage_id}/confirm",
                headers=AUTH,json=payload)
            assert committed.status_code==200,committed.text
            chapter=await client.get("/v1/books/site-demo/chapters/c1",headers=AUTH)
            assert chapter.status_code==200
            assert "Глава 999. Это текст" in chapter.json()["source_html"]
            assert (await client.get(f"/v1/source-books/stages/{stage_id}/provenance",headers=BOB)).status_code==404
    asyncio.run(scenario())


def test_partial_download_never_creates_import_stage(tmp_path):
    db=QueueStore(tmp_path/"partial.sqlite3")
    app=create_app(Settings(db_path=db.db_path,team_tokens={"alice":"alice-key"}),db)
    app.state.public_source_fetcher=FakeSite(bad_second=True)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            cat=(await client.post("/v1/source-books/catalogs",headers=AUTH,
                json={"source":"royalroad","book_url":BOOK})).json()
            out=await client.post(f"/v1/source-books/catalogs/{cat['catalog_id']}/collect",
                headers=AUTH,json={"ordinals":[1,2],
                "expected_catalog_sha256":cat["catalog_sha256"]})
            assert out.status_code==201,out.text
            assert out.json()["status"]=="partial"
            assert out.json()["failed_count"]==1
            assert out.json()["stage_id"] is None
    asyncio.run(scenario())
    with db._connect() as con:
        assert con.execute("SELECT COUNT(*) FROM book_import_stages").fetchone()[0]==0
        assert con.execute("SELECT COUNT(*) FROM source_book_stage_receipts").fetchone()[0]==0


def test_cross_site_url_rejected_pre_network(tmp_path):
    db=QueueStore(tmp_path/"url.sqlite3")
    app=create_app(Settings(db_path=db.db_path,team_tokens={"alice":"alice-key"}),db)
    app.state.public_source_fetcher=FakeSite()
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            reply=await client.post("/v1/source-books/catalogs",headers=AUTH,json={
                "source":"royalroad","book_url":"https://169.254.169.254/latest/meta-data"})
            assert reply.status_code==422
            assert not app.state.public_source_fetcher.calls
    asyncio.run(scenario())
