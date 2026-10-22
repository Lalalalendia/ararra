#!/usr/bin/env python3
"""M1p-HASH: compare canonical staged TXT source hashes to the Python oracle.

The Rust candidate is proof-only, not used by a SQLite writer or server route.
Use actual Python book_import candidate HTML and Python's production source
cleaner to determine byte-exact normalized HTML and SHA256 per chapter.
"""
from __future__ import annotations

from html import escape as escape_html
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from chaptera_server.book_import import parse_book_import
from chaptera_translation_core.core.worker_helpers.translation_completeness import (
    strip_source_boilerplate,
)

RUSSIAN = "История продолжается в долине между двумя городами. " * 12
ENGLISH = "The travelers followed the river through the ancient valley. " * 12
CHINESE = "夜色慢慢笼罩村庄而远处的灯火仍然亮着。" * 30


def verified_inputs() -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    seeds = [RUSSIAN, ENGLISH, CHINESE,
             RUSSIAN + "Всё шло своим чередом. ",
             ENGLISH + "Nobody expected any changes. ",
             CHINESE + "旅人继续前行。"]
    for i, text in enumerate(seeds):
        index = i + 1
        heading = f"Глава {index}" if i % 2 == 0 else f"Chapter {index}"
        raw = heading + "\n" + text
        preview, candidates, _prefix = parse_book_import("story.txt", raw.encode())
        assert preview["chapter_count"] == 1 and len(candidates) == 1
        chapter = candidates[0]
        assert chapter.number == index, (heading, chapter)
        canonical = "<h1>" + escape_html(chapter.title) + "</h1>\n" + chapter.html
        for path in (f"chapters/{index}.xhtml", "stories/story.xhtml",
                     f"volumes/chapter-{index}.xhtml"):
            cases.append({"html": canonical, "chapter_path": path})

    for i in range(1, 22):
        heading = f"Глава {i}" if i % 2 else f"Chapter {i}"
        first = seeds[i % len(seeds)]
        second = seeds[(i + 2) % len(seeds)]
        raw = heading + "\n" + first + "\n\n" + second
        preview, chapters, _ = parse_book_import("novel.txt", raw.encode())
        assert preview["chapter_count"] == 1 and len(chapters) == 1
        entry = chapters[0]
        canonical = "<h1>" + escape_html(entry.title) + "</h1>\n" + entry.html
        cases.append({
            "html": canonical, "chapter_path": f"chapters/{i}.xhtml",
        })
    # A multi-chapter import: every source entry receives an independent
    # canonical H1 and is hashed with its own destination path.
    multi = "Глава 31\n" + RUSSIAN + "\nГлава 32\n" + ENGLISH
    prev, entries, _ = parse_book_import("combined.txt", multi.encode())
    assert prev["chapter_count"] == len(entries) == 2
    for entry in entries:
        cases.append({
            "html": "<h1>" + escape_html(entry.title) + "</h1>\n" + entry.html,
            "chapter_path": f"chapters/{entry.number}.xhtml",
        })
    return cases


def rejected_inputs() -> list[dict[str, str]]:
    return [
        {"html": "<h1>Глава 1. Название</h1>\n<p>Текст</p>",
         "chapter_path": "chapters/1.xhtml"},
        {"html": "<h1>Глава 2</h1>\n<p>A &amp; B</p>",
         "chapter_path": "chapters/2.xhtml"},
        {"html": "<h1>Chapter 3</h1>\n<p>Первая<br/>вторая</p>",
         "chapter_path": "chapters/3.xhtml"},
        {"html": "<h1>Глава 4</h1>\n<p><script>alert(1)</script></p>",
         "chapter_path": "chapters/4.xhtml"},
        {"html": "<h1>Глава 5</h1>\n<p>故事继续。</p>\n下一章",
         "chapter_path": "chapters/5.xhtml"},
        {"html": "<h1>Глава 6</h1>\n<p>记住本站</p>",
         "chapter_path": "chapters/6.xhtml"},
        {"html": "<h1>Глава 7</h1>\n<p>Text.</p>",
         "chapter_path": "../private.xhtml"},
        {"html": "<h1>Chapter 8</h1>\n<p>Text.</p>",
         "chapter_path": "/root/secret.xhtml"},
        {"html": "<h1>Глава 0</h1>\n<p>Text.</p>",
         "chapter_path": "chapters/0.xhtml"},
        {"html": "<h1>Chapter 9</h1>\n<p>下一章</p>",
         "chapter_path": "chapters/9.xhtml"},
        {"html": "<h1>Глава 10</h1>\n<p>Content.</p>\n<p>A &amp; B.</p>",
         "chapter_path": "chapters/10.xhtml"},
        {"html": "<h1>Глава 11</h1>\n<p>Short</p><p>bad\nline</p>",
         "chapter_path": "chapters/11.xhtml"},
    ]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_import_hash.py rust/source-import-probe")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    positives = verified_inputs()
    negatives = rejected_inputs()
    cases = positives + negatives
    result = subprocess.run(
        [binary], input="".join(
            json.dumps(case, ensure_ascii=False) + "\n" for case in cases
        ), text=True, encoding="utf-8", capture_output=True, check=True,
    )
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(rows) == len(cases), (
        "Mismatched source-import-probe JSONL output count", len(rows), len(cases)
    )
    for i, (candidate, rust) in enumerate(zip(positives, rows)):
        original = candidate["html"]
        path = candidate["chapter_path"]
        expected_html = strip_source_boilerplate(
            original, current_chapter_path=path,
        )
        expected_hash = hashlib.sha256(expected_html.encode()).hexdigest()
        assert rust["status"] == "ok", (i, path, rust)
        assert rust["cleaned_html"] == expected_html, (
            i, path, repr(expected_html), repr(rust["cleaned_html"])
        )
        assert rust["source_hash"] == expected_hash, (
            i, path, expected_hash, rust["source_hash"]
        )
        assert rust["removed_nodes"] == 1, (
            i, path, rust["removed_nodes"]
        )

    for i, rejected in enumerate(rows[len(positives):], len(positives)):
        assert rejected == {"status": "unsupported"}, (i, rejected)

    print(f"rust_import_txt_hash_python_byte_parity={len(positives)}_PASS")
    print(f"rust_import_txt_hash_fail_closed_unproved_cases={len(negatives)}_PASS")
    print("rust_import_txt_hash_sqlite_writes=ZERO")
    print("rust_import_txt_hash_production_writer_enabled=NO")


if __name__ == "__main__":
    main()
