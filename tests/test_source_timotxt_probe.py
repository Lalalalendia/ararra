"""The controlled live metadata probe never downloads or logs chapter bodies."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from chaptera_server.source_collect import catalog_from_html
from scripts import probe_timotxt_metadata as probe

BOOK = "https://www.timotxt.com/2010493463/"
HTML = """<html>
<h1>測試小說 章節列表</h1><p>分類：穿越 章節： 2 更新：今日</p>
<h3>最新章節</h3><ul>
<li><a href="/2010493463/11.html">第2章 新一頁</a></li>
</ul>
<h3>測試小說全部章节</h3>
<ul><li><a href="/2010493463/10.html">第1章 開場</a></li>
<li><a href="/2010493463/11.html">第2章 新一頁</a></li></ul>
</html>"""


def test_receipt_contains_metadata_not_source_prose_or_titles():
    catalog = catalog_from_html("timotxt", BOOK, HTML)
    receipt = probe.metadata_receipt(catalog)
    assert receipt["book_id"] == "2010493463"
    assert receipt["observed_chapter_count"] == 2
    assert receipt["unique_chapter_ids"] == 2
    assert receipt["complete_directory_identity"]
    assert receipt["chapter_body_requests"] == 0
    text = json.dumps(receipt, ensure_ascii=False)
    assert "新一頁" not in text
    assert "開場" not in text
    assert "<html" not in text
    assert "prose_logged" in text and receipt["prose_logged"] is False


def test_no_network_without_explicit_metadata_approval(monkeypatch):
    class Disallowed:
        def __init__(self, *a, **kw):
            raise AssertionError("Probe attempted network setup without operator approval")

    monkeypatch.setattr(probe, "PublicHtmlFetcher", Disallowed)
    monkeypatch.setattr(sys, "argv", [
        "probe_timotxt_metadata.py", "--book-id", "2010493463",
    ])
    with pytest.raises(SystemExit) as err:
        probe.main()
    assert err.value.code == 2


def test_unreviewed_book_id_is_refused_before_fetch():
    for book_id in ("", "1", "abcdef1234", "9999999999", "2010493463/dir"):
        with pytest.raises(ValueError, match="nine reviewed"):
            asyncio.run(probe.inspect(book_id))
    assert len(probe.KNOWN_BOOK_IDS) == 9
