#!/usr/bin/env python3
"""RUST-03f: byte-exact nested-inline cleanup vs Python BeautifulSoup.

This is a narrowly scoped READ-ONLY differential gate, not a production
source_hash writer. Never use this success to approve arbitrary HTML.
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

PATH = "parts/story.xhtml"
PROMOS = [
    "<p>记住本站</p>",
    "<p>最新小说</p>",
    "<p>本章完</p>",
    "<p>101kks.net</p>",
]
TAILS = [
    "<p>下一章</p>",
    "<p>next chapter</p>",
    "<p>下一页</p>",
]
HINTS = ["<p>溫馨提示：</p>", "<p>温馨提示:</p>"]
INLINE = [
    "<p>Привет <strong>друг</strong>, сказал он.</p>",
    "<p>故事 <em>继续</em>。</p>",
    "<p>A <b>bold</b> new world.</p>",
    "<p>Wind <i>whispered</i> across the field.</p>",
    "<p>他说，<span>很好</span>。</p>",
    "<p>Слова <strong>были <em>важны</em></strong> для неё.</p>",
    "<p><span>雨落在地上。</span></p>",
    "<p>Testing <b>bold</b> then <span>spans</span> at night.</p>",
    "<p>“Да, <i>конечно</i>”, — ответил он.</p>",
]
PLAIN = [
    "<p>Что произошло дальше?</p>",
    "<p>他走进了房间。</p>",
    "<p>Story continues after the storm.</p>",
    "<p>后来一切都改变了。</p>",
]


def sample(html: str, path: str = PATH) -> dict[str, str]:
    return {"html": html, "chapter_path": path}


def corpus() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    good: list[dict[str, str]] = []
    # Preserve genuinely nested body in every supported example.
    for p in INLINE:
        good.append(sample(p))
        for promo in PROMOS:
            good.append(sample(p + promo))
            good.append(sample(p + promo + PLAIN[0]))
            good.append(sample(PLAIN[0] + promo + p))
        for marker in TAILS:
            good.append(sample(p + marker + INLINE[1]))
            good.append(sample(PLAIN[0] + p + marker + INLINE[1]))
        for hint in HINTS:
            good.append(sample(p + hint))
            good.append(sample(hint + p))
        good.append(sample(PLAIN[0] + p + PLAIN[1]))
    # Python searches from node zero when it is the sole visible text node.
    # The first node of a multi-node document remains an ordinary content node.
    for boundary in TAILS:
        good.append(sample(boundary))
        good.append(sample(boundary + PLAIN[0]))
    rng = random.Random(20261021)
    for _ in range(280):
        elements = [rng.choice(INLINE), rng.choice(INLINE + PLAIN)]
        if rng.randrange(3) == 0:
            elements.insert(1, rng.choice(PROMOS))
        if rng.randrange(4) == 0:
            elements.append(rng.choice(TAILS))
            elements.append(rng.choice(INLINE))
        if rng.randrange(4) == 0:
            elements.append(rng.choice(HINTS))
        good.append(sample("".join(elements)))

    unsupported = [
        "<p>Before <b>记住本站</b> after.</p>",
        "<p>Before <em>下一章</em> after.</p>",
        "<p><b>记住本站</b></p>",
        "<p><span>本章完</span></p>",
        "<p>Open <strong>without closing.</p>",
        "<p><b>Wrong close</i></p>",
        "<p><b>Nested <em>text</b></em>.</p>",
        "<p>A &amp; B</p>",
        "<p>A &#20013; B</p>",
        '<p class="body">Class attribute.</p>',
        "<div><p>Wrapped in a div.</p></div>",
        "<p>Source <script>hidden</script> word.</p>",
        "<p>Source <a href='x'>link</a> word.</p>",
        "<p>Chapter 7 <i>heading</i></p>",
        "<p>第七章 <i>开始</i></p>",
        "<p> \t </p>",
        "<p>Only an <i></i> empty tag.</p>",
        "<p>Emoji 🧑‍💻 <b>word</b></p>",
        "<p>One <b>bold</b></p>\n<p>Next</p>",
        "<p>Short <br/> line.</p>",
    ]
    wrong_paths = [
        sample(INLINE[0], "chapters/7.xhtml"),
        sample(INLINE[0], "第九章.xhtml"),
        sample(INLINE[0], "chapter-01.html"),
    ]
    good = list({json.dumps(item, ensure_ascii=False): item for item in good}.values())
    bad = [sample(html) for html in unsupported] + wrong_paths
    return good, bad


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_inline_clean.py /path/to/source-inline-probe")
    supported, rejected = corpus()
    cases = supported + rejected
    proc = subprocess.run(
        [sys.argv[1]],
        input="".join(json.dumps(item, ensure_ascii=False) + "\n" for item in cases),
        text=True,
        capture_output=True,
        timeout=50,
        check=True,
    )
    results = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(results) == len(cases), (
        f"Result length {len(results)} != expected {len(cases)}"
    )
    mismatches = []
    for item, result in zip(supported, results[:len(supported)]):
        raw = item["html"]
        cleaned = strip_source_boilerplate(
            raw, current_chapter_path=item["chapter_path"],
        )
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        if result.get("status") != "ok" or result.get("cleaned_html") != cleaned or result.get("source_hash") != digest:
            mismatches.append((raw, cleaned, result))
    if mismatches:
        for raw, expected, result in mismatches[:12]:
            print(
                f"RUST-03f mismatch input={raw!r}"
                f"\npython_clean={expected!r}\nrust={result!r}"
            )
        raise AssertionError(
            f"Nested inline cleaner differs from Python in {len(mismatches)} cases"
        )
    for item, result in zip(rejected, results[len(supported):]):
        assert result == {"status": "unsupported"}, (
            f"Invalid HTML/context unexpectedly accepted {item!r}: {result}"
        )
    print(f"rust_inline_source_clean_parity_cases={len(supported)}")
    print(f"rust_inline_source_clean_failclosed_cases={len(rejected)}")
    print("rust_inline_source_clean_hash_parity=PASS")
    print("rust_inline_source_clean_html_byte_parity=PASS")
    print("rust_inline_source_clean_nested_marker_rejected=PASS")
    print("rust_inline_source_clean_arbitrary_dom=NOT_SUPPORTED")
    print("rust_inline_source_clean_writer_integration=no")
    print("production_processes_touched=0")


if __name__ == "__main__":
    main()
