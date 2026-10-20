#!/usr/bin/env python3
"""RUST-03c: Python BeautifulSoup vs Rust restricted source cleaning.

This tests only immediately-adjacent plain paragraphs. It does not imply
general HTML source cleanup, serialization, or permission to write live data.
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys

from chaptera_translation_core.core.worker_helpers.translation_completeness import (
    strip_source_boilerplate,
)


SAFE_TEXTS = (
    "故事开始。",
    "故事后来继续。",
    "他轻轻地推开门。",
    "完整叙述在这一段。",
    "Это обычная речь героя.",
    "События развиваются без спешки.",
    "An ordinary sentence.",
    "The narrative carries on.",
    "“会发生什么？”",
    "Толпа замолчала.",
    "最新小说",
    "记住本站",
    "本章完",
    "101kanshu.com",
    "下一章",
    "下章",
    "next chapter",
    "溫馨提示：",
    "温馨提示:",
    "最新章节",
    "首发 101read.net",
    "普通故事后面的文字。",
    "Ｎｅｘｔ　Ｃｈａｐｔｅｒ",
    "《 கதையல்ல 》",  # unsupported language: excluded below
)
VALID_TEXTS = tuple(item for item in SAFE_TEXTS if "க" not in item)


def compact(nodes: tuple[str, ...]) -> str:
    return "".join("<p>" + text + "</p>" for text in nodes)


def corpus() -> list[dict[str, str]]:
    seeds = [
        ("故事开始。",),
        ("本章完",),
        ("下一章",),
        ("故事开始。", "下一章", "新章节不属于本章。"),
        ("下一章", "故事继续。"),
        ("记住本站", "普通故事。"),
        ("溫馨提示：", "故事内容。"),
        ("故事内容。", "溫馨提示："),
        ("第一段。", "本章完", "附录正文。"),
        ("Stories continue.", "next chapter", "not part of this book"),
        ("A paragraph.", "Next Chapter: More", "Next book's content"),
        ("Аудиокнига начинается.", "最新小说", "普通人物对话。"),
        ("记住本站", "本章完"),
        ("记住本站", "下一章", "后面的不在本章"),
        ("最新小说", "记住本站"),
        ("“会发生什么？”", "Толпа замолчала。"),
        ("故事开始。", "101kanshu.com", "结束。"),
        ("故事开始。", "温馨提示:", "下一章"),
        ("Ｎｅｘｔ　Ｃｈａｐｔｅｒ",),
    ]
    rng = random.Random(20261020)
    for _ in range(500):
        n = rng.randint(1, 6)
        seeds.append(tuple(rng.choice(VALID_TEXTS) for _ in range(n)))
    cases = []
    for nodes in seeds:
        cases.append({"html": compact(nodes), "chapter_path": "parts/story.xhtml"})
    cases += [
        {"html": "<p>Inner <em>nested</em> markup</p>", "chapter_path": "parts/story.xhtml"},
        {"html": "<p>Encoded &amp; entities</p>", "chapter_path": "parts/story.xhtml"},
        {"html": "<div>Not a plain paragraph</div>", "chapter_path": "parts/story.xhtml"},
        {"html": "<p>Chapter 42</p>", "chapter_path": "parts/story.xhtml"},
        {"html": "<p>合法内容。</p>", "chapter_path": "chapter-42.xhtml"},
        {"html": "<p> </p>", "chapter_path": "parts/story.xhtml"},
        {"html": "<p>🧑‍💻 emoji outside validated grammar</p>", "chapter_path": "parts/story.xhtml"},
        {"html": "", "chapter_path": "parts/story.xhtml"},
    ]
    return list({json.dumps(c, ensure_ascii=False): c for c in cases}.values())


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_source_clean.py /path/to/source-clean-probe")
    cases = corpus()
    unsupported = cases[-8:]
    supported = cases[:-8]
    payload = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases)
    run = subprocess.run(
        [sys.argv[1]], input=payload, capture_output=True,
        text=True, timeout=60, check=True,
    )
    output = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(output) == len(cases), "Source-clean oracle output length mismatch"
    for case, result in zip(supported, output[:len(supported)]):
        assert result["status"] == "ok", case
        original = case["html"]
        clean = strip_source_boilerplate(
            original, current_chapter_path=case["chapter_path"]
        )
        digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        assert result["cleaned_html"] == clean, (
            f"Rust/Python cleaned HTML mismatch for synthetic input {original!r}"
        )
        assert result["source_hash"] == digest, (
            f"Rust/Python source hash mismatch for {original!r}"
        )
        assert result["removed_nodes"] <= original.count("<p>")
    for case, result in zip(unsupported, output[-len(unsupported):]):
        assert result["status"] == "unsupported", case
    print(f"rust_plain_source_clean_supported_cases={len(supported)}")
    print(f"rust_plain_source_clean_rejected_cases={len(unsupported)}")
    print("rust_plain_source_clean_hash_parity=PASS")
    print("rust_plain_source_clean_removed_boilerplate_parity=PASS")
    print("rust_plain_source_clean_numeric_context_fail_closed=PASS")
    print("rust_plain_source_clean_writer_integration=no")
    print("rust_plain_source_clean_malformed_html=NOT_SUPPORTED")
    print("production_processes_touched=0")


if __name__ == "__main__":
    main()
