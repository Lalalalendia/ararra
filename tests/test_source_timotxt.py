"""Timotxt support: full index, correct book identity, multi-part RAW, and safe staging.

Synthetic, locally owned fixtures only. No external requests or paid translation.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.source_catalog_med import (
    IngestError, chapter_identity, make_adapter, source_book_id, validate_url,
)
from chaptera_server.source_chapter_parts import fetch_complete_chapter
from chaptera_server.source_collect import catalog_from_html, collect_selected, fetch_catalog
from chaptera_server.source_job_store import SourceJobStore
from chaptera_server.source_job_worker import SourceCollectionWorker
from chaptera_server.source_request_limiter import SourceSiteBusy
from chaptera_server.source_site_profiles import SITE_PROFILES
from chaptera_server.storage import QueueStore

ROOT = "https://www.timotxt.com/2010493463/"
DIR = ROOT + "dir"
ONE = ROOT + "10.html"
ONE_PART2 = ROOT + "10_2.html"
TWO = ROOT + "11.html"
EXTRA = ROOT + "12.html"
SERVICE = ROOT + "13.html"
FOREIGN = "https://www.timotxt.com/2010493464/14.html"
AUTH = {"X-API-Key": "mod-key"}
INTRO = f"""<html><h1>測試小說</h1>
<ul><li><a href="{SERVICE}">完本感言</a></li></ul>
<a href="/2010493463/dir">查看完整章節目錄</a>
<a href="/2010493463/10.html">開始閱讀</a></html>"""
DIRECTORY = f"""<html><h1>測試小說 章節列表</h1>
<p>作者： 某人 分類：穿越 章節： 4 更新： 2026-10-20</p>
<h3>最新章節</h3>
<ul><li><a href="{SERVICE}">完本感言</a></li>
<li><a href="{EXTRA}">番外：短篇</a></li>
<li><a href="{TWO}">第二章 又一段</a></li></ul>
<h3>測試小說全部章节</h3>
<ul class="chapter-list">
<li><a href="/2010493463/10.html">第一章 開場</a></li>
<li><a href="/2010493463/11.html">第二章 又一段</a></li>
<li><a href="/2010493463/12.html">番外：短篇</a></li>
<li><a href="/2010493463/13.html">完本感言</a></li>
<li><a href="{FOREIGN}">外站其他書</a></li>
<li><a href="https://evil.invalid/2010493463/88.html">異域</a></li>
</ul></html>"""
FIRST_TEXT = "門外樹影搖晃，巡夜的人沿著石板路走進城裡。" * 12
SECOND_TEXT = "另一個人從遠處返回，說起夜間發生的事情。" * 12
NEXT_TEXT = "到第二章時，天色已亮，眾人繼續前行。" * 20


def page(body, *, next_part=None):
    navigation = (
        '<div class="foot-nav"><a href="' + next_part + '">下一頁</a></div>'
        if next_part else '<div class="foot-nav"><a href="' + TWO + '">下一章</a></div>'
    )
    return ('<html><h1>閱讀頁</h1><div class="content">'
            '<p>' + body + '</p><p>' + body + '</p>'
            '<div class="ad">廣告不要收集</div></div>'
            + navigation + '</html>')


FIRST = page(FIRST_TEXT, next_part="/2010493463/10_2.html")
PART2 = page(SECOND_TEXT)
SECOND = page(NEXT_TEXT)


class FakeTimotxt:
    def __init__(self, *, throttled_part=False, obfuscated=False):
        self.calls = []
        self.throttled_part = throttled_part
        self.obfuscated = obfuscated

    async def fetch(self, source, url):
        assert source == "timotxt"
        self.calls.append(url)
        if url == ROOT:
            return INTRO
        if url == DIR:
            return DIRECTORY
        if url == ONE:
            return FIRST
        if url == ONE_PART2:
            if self.throttled_part:
                raise SourceSiteBusy(65, "upstream_429")
            return page(("故事中꺗出現了字元놖꺗。" * 25)) if self.obfuscated else PART2
        if url == TWO:
            return SECOND
        if url in (EXTRA, SERVICE):
            return page("此處是明確標記的額外資料。" * 18)
        raise AssertionError("Undeclared Timotxt URL: " + url)


def catalog():
    return catalog_from_html("timotxt", ROOT, DIRECTORY)


def test_timotxt_profile_and_chapter_url_provenance():
    p = SITE_PROFILES["timotxt"]
    assert p.hosts == frozenset({"www.timotxt.com", "timotxt.com"})
    assert p.minimum_delay_seconds >= 6
    assert p.daily_requests <= 240
    assert source_book_id(ROOT, "timotxt") == "2010493463"
    assert source_book_id(ONE_PART2, "timotxt") == "2010493463"
    assert chapter_identity(ONE, "timotxt", "2010493463") == "10"
    assert chapter_identity(ONE_PART2, "timotxt", "2010493463") is None
    assert chapter_identity(FOREIGN, "timotxt", "2010493463") is None


@pytest.mark.parametrize("url", [
    "http://www.timotxt.com/2010493463/",
    "https://evil.invalid/2010493463/",
    "https://www.timotxt.com@evil.invalid/2010493463/",
    "https://www.timotxt.com.evil.invalid/2010493463/",
    "https://www.timotxt.com/2010493463/?page=2",
    "https://www.timotxt.com/2010493463/#fragment",
    "https://www.timotxt.com/2010493463/%2e%2e/10.html",
    "https://www.timotxt.com/2010493463/10.html?view=all",
    "https://www.timotxt.com/2010493463/10_2.html#hack",
    "https://www.timotxt.com/2010493463:8080/",
])
def test_timotxt_urls_fail_closed(url):
    with pytest.raises(ValueError):
        validate_url(url, "timotxt")


def test_only_book_root_may_start_discovery():
    calls = []
    async def fetch(url):
        calls.append(url)
        raise AssertionError("Must refuse before network")
    for invalid in (DIR, ONE, ONE_PART2):
        with pytest.raises(ValueError, match="book root"):
            asyncio.run(fetch_catalog("timotxt", invalid, fetch))
    assert not calls


def test_explicit_directory_link_and_ascending_full_list_not_latest_previews():
    calls = []
    async def fetch(url):
        calls.append(url)
        if url == ROOT: return INTRO
        if url == DIR: return DIRECTORY
        raise AssertionError(url)
    cat = asyncio.run(fetch_catalog("timotxt", ROOT, fetch))
    assert calls == [ROOT, DIR]
    assert cat.page_urls == (ROOT, DIR)
    assert cat.declared_chapter_count == 4
    assert cat.coverage_state == "declared_count_matched"
    assert [(c.ordinal, c.source_id) for c in cat.chapters] == [
        (1, "10"), (2, "11"), (3, "12"), (4, "13"),
    ]
    assert [c.entry_type for c in cat.chapters] == [
        "chapter", "chapter", "extra", "service",
    ]
    assert [c.author_number for c in cat.chapters] == [1, 2, None, None]
    assert all(c.url.startswith(ROOT) for c in cat.chapters)


def test_toc_identity_and_declared_count_fail_closed():
    with pytest.raises(IngestError, match="advertises 5 chapters"):
        catalog_from_html("timotxt", ROOT, DIRECTORY.replace("章節： 4", "章節： 5"))
    with pytest.raises(IngestError, match="directory"):
        catalog_from_html("timotxt", ROOT, DIRECTORY.replace("全部章节", "最新章節"))
    # Publisher numbering and website chapter-list positions are different.
    duplicated_author_label = DIRECTORY.replace("第二章 又一段", "第一章 又一段")
    cat = catalog_from_html("timotxt", ROOT, duplicated_author_label)
    assert [(c.ordinal, c.author_number) for c in cat.chapters[:2]] == [
        (1, 1), (2, 1),
    ]


def test_no_dir_link_means_no_unreviewed_guess_or_false_full_book():
    calls = []
    async def fetch(url):
        calls.append(url)
        return INTRO.replace('<a href="/2010493463/dir">查看完整章節目錄</a>', '')
    with pytest.raises(IngestError, match="directory link"):
        asyncio.run(fetch_catalog("timotxt", ROOT, fetch))
    assert calls == [ROOT]


def test_timotxt_partial_chapter_link_identity_and_assembly():
    fake = FakeTimotxt()
    async def fetch(url):
        return await fake.fetch("timotxt", url)
    body = asyncio.run(fetch_complete_chapter("timotxt", ROOT, ONE, fetch))
    assert FIRST_TEXT in body and SECOND_TEXT in body
    assert "廣告不要收集" not in body
    assert fake.calls == [ONE, ONE_PART2]
    assert body.index(FIRST_TEXT) < body.index(SECOND_TEXT)


def test_unverified_part_links_cannot_turn_partial_raw_into_complete():
    async def fetch(url):
        if url == ONE:
            return page(FIRST_TEXT, next_part="/2010493463/10_3.html")
        raise AssertionError("Must not fetch skipped part")
    with pytest.raises(IngestError, match="part skipped"):
        asyncio.run(fetch_complete_chapter("timotxt", ROOT, ONE, fetch))
    for target in (
        "https://evil.invalid/2010493463/10_2.html",
        "/2010493464/10_2.html",
        "/2010493463/10_2.html?adult=yes",
    ):
        async def bad_fetch(url):
            if url == ONE:
                return page(FIRST_TEXT, next_part=target)
            raise AssertionError("Invalid part was fetched: " + url)
        with pytest.raises(IngestError, match="next-page|continuation|Unsafe"):
            asyncio.run(fetch_complete_chapter("timotxt", ROOT, ONE, bad_fetch))


def test_obfuscated_hangul_and_pua_are_not_misrepresented_as_verified_cn_raw():
    adapter = make_adapter("timotxt", ROOT)
    for text in (
        "這是一段正常中文" * 50 + "놖꺗껣",
        "這是一段正常中文" * 50 + "\ue014\ue015\ue016",
    ):
        with pytest.raises(IngestError, match="obfuscated"):
            adapter.parse_body(page(text))
    with pytest.raises(IngestError, match="challenge"):
        adapter.parse_body("<h1>Too Many Requests</h1>")


def test_selected_chapters_and_429_second_part_do_not_stage_partial_text():
    fake = FakeTimotxt(throttled_part=True)
    async def fetch(url):
        return await fake.fetch("timotxt", url)
    with pytest.raises(SourceSiteBusy) as raised:
        asyncio.run(collect_selected(
            catalog(), [1], fetch, expected_catalog_sha256=catalog().catalog_sha256,
        ))
    assert raised.value.reason == "upstream_429"
    assert fake.calls == [ONE, ONE_PART2]
    fake.throttled_part = False
    collected = asyncio.run(collect_selected(
        catalog(), [1, 2], fetch, expected_catalog_sha256=catalog().catalog_sha256,
    ))
    assert collected.complete
    assert [c.source_id for c in collected.chapters] == ["10", "11"]
    assert FIRST_TEXT in collected.chapters[0].body
    assert SECOND_TEXT in collected.chapters[0].body
    assert NEXT_TEXT in collected.chapters[1].body


def test_durable_worker_429_part_preserves_zero_completed_and_retries(tmp_path):
    queue = QueueStore(tmp_path / "worker.sqlite3")
    jobs = SourceJobStore(queue)
    created = jobs.create("mod", "timotxt-catalog", catalog(), [1])
    fake = FakeTimotxt(throttled_part=True)
    worker = SourceCollectionWorker(jobs, fake)
    assert asyncio.run(worker.run_once(only_job_id=created["job_id"]))
    state = jobs.get("mod", created["job_id"])
    assert state["completed"] == 0
    assert state["failed"] == 0
    with queue._connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM source_collect_items WHERE job_id=?",
            (created["job_id"],),
        ).fetchone()[0]
        assert count == 0


def test_api_book_to_full_directory_to_staged_verified_chapter(tmp_path):
    queue = QueueStore(tmp_path / "end-to-end.sqlite3")
    app = create_app(Settings(
        db_path=queue.db_path, team_tokens={"mod": "mod-key"},
    ), queue)
    fake = FakeTimotxt()
    app.state.public_source_fetcher = fake

    async def test():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            listing = await client.get("/v1/source-books/sites", headers=AUTH)
            assert listing.status_code == 200
            assert "timotxt" in {x["id"] for x in listing.json()["sources"]}
            resp = await client.post(
                "/v1/source-books/catalogs", headers=AUTH,
                json={"source": "timotxt", "book_url": ROOT},
            )
            assert resp.status_code == 201, resp.text
            preview = resp.json()
            assert preview["chapter_count"] == 4
            assert preview["declared_chapter_count"] == 4
            assert preview["coverage_state"] == "declared_count_matched"
            catalog_id = preview["catalog_id"]
            stale = await client.post(
                f"/v1/source-books/catalogs/{catalog_id}/collect", headers=AUTH,
                json={"ordinals": [1], "expected_catalog_sha256": "0" * 64},
            )
            assert stale.status_code == 409
            result = await client.post(
                f"/v1/source-books/catalogs/{catalog_id}/collect", headers=AUTH,
                json={"ordinals": [1, 2], "expected_catalog_sha256": preview["catalog_sha256"]},
            )
            assert result.status_code == 201, result.text
            assert result.json()["status"] == "staged"
            stage = await client.get(
                f"/v1/imports/stages/{result.json()['stage_id']}", headers=AUTH,
            )
            assert stage.status_code == 200
            assert stage.json()["preview"]["chapter_count"] == 2
            provenance = await client.get(
                f"/v1/source-books/stages/{result.json()['stage_id']}/provenance", headers=AUTH,
            )
            assert provenance.status_code == 200
            assert [x["ordinal"] for x in provenance.json()["chapters"]] == [1, 2]
            assert FIRST_TEXT not in provenance.text
            assert fake.calls == [ROOT, DIR, ONE, ONE_PART2, TWO]
    asyncio.run(test())
