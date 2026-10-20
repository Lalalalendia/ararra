#!/usr/bin/env python3
"""Compare Rust DOM text-node extraction with Python BeautifulSoup HTML parser.

RUST-03b covers read-only, well-formed, bounded HTML shapes only. This probe
does NOT claim Python source hash, HTML serialization, or document-policy parity.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys

from bs4 import BeautifulSoup, Comment


def reference_nodes(html: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    return [
        str(node)
        for node in root.find_all(string=True)
        if not isinstance(node, Comment)
        and str(node).strip()
        and not node.find_parent(["script", "style"])
    ]


def corpus() -> list[str]:
    seeds = [
        "",
        " ",
        "<p>Первая глава.</p>",
        "<p>第一章</p><p>故事继续。</p>",
        "<h1>Chapter 1</h1><p>In the beginning.</p>",
        "<h2>Глава 2</h2><div>One.</div><div>Two.</div>",
        "<p>Before <b>bold</b> after.</p>",
        "<p>Here <em>was <strong>nested</strong></em> text.</p>",
        "<blockquote><p>Предисловие.</p><p>Продолжение.</p></blockquote>",
        "<ul><li>First</li><li>Second</li></ul>",
        "<div><p>本章完</p><p>下一章</p></div>",
        "<p data-id='c1' class='text'>Атрибуты сохранены.</p>",
        "<p>Line 1\nLine 2</p>",
        "<p>\t  </p><p> \u00a0 </p><p>Real content</p>",
        "<!-- ignored --><p>Visible.</p>",
        "<p>Visible.</p><script>hidden()</script><p>Visible again.</p>",
        "<div><style>h1 { color: red; }</style><p>Body</p></div>",
        "<p>Escape &amp; &lt;world&gt; &quot;quotes&quot;</p>",
        "<p>Numerical &#20013;&#25991; and &#x1F389;.</p>",
        "<p>nbsp&nbsp;mid</p>",
        "<p>emoji😊</p><p>𠀀 remote CJK</p>",
        "<html><head><title>Hidden title</title></head><body><h1>Source title</h1><p>Body.</p></body></html>",
        "<html><body><section><p>Inside</p></section></body></html>",
        "<html><head><title>Document title</title></head><body>Intro <p>Body.</p> outro</body></html>",
        "<p><span>One</span> <span>Two</span></p>",
        "<div>Prefix <span>inner <i>nested</i> text</span> suffix</div>",
        "<div>head<strong>middle</strong>tail</div>",
        "<p><!-- comment inside -->Visible</p>",
        "<p>  Hello  </p><p> World  </p>",
        "<h3>第3章</h3><p>Body</p>",
        "<pre>two spaces  inside</pre>",
        "<p>Chapter four; next chapter: 5.</p>",
        "<p>Watermark 101kanshu.com in narrative</p>",
    ]
    rng = random.Random(20261020)
    words = ["Hello", "你好", "مرحبا", "Глава", "main", "  ", "✨", "雨", "&amp;"]
    for _ in range(180):
        chunks = rng.choices(words, k=rng.randrange(1, 8))
        first = "<p>" + " ".join(chunks) + "</p>"
        tail = rng.choice([
            "<p>" + rng.choice(words) + "</p>",
            "<p><b>" + rng.choice(words) + "</b>" + rng.choice(words) + "</p>",
            "<!-- comment -->",
            "<blockquote>" + rng.choice(words) + "</blockquote>",
            "<p> </p>",
        ])
        seeds.append(first + tail)
    return list(dict.fromkeys(seeds))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_source_dom.py /path/to/source-dom-probe")
    cases = corpus()
    expected = [reference_nodes(html) for html in cases]
    proc = subprocess.run(
        [sys.argv[1]],
        input="".join(json.dumps(html, ensure_ascii=False) + "\n" for html in cases),
        text=True, capture_output=True, timeout=40, check=True,
    )
    lines = proc.stdout.splitlines()
    assert len(lines) == len(cases), (
        f"DOM corpus count mismatch: {len(lines)} vs {len(cases)}"
    )
    differences = []
    for html, answer, line in zip(cases, expected, lines):
        actual = json.loads(line)["nodes"]
        if actual != answer:
            differences.append((html, answer, actual))
    if differences:
        for html, answer, actual in differences[:10]:
            print(f"DOM mismatch HTML={html!r}\npython={answer!r}\nrust={actual!r}")
        raise AssertionError(
            f"Read-only Rust DOM text extraction differs on {len(differences)} cases"
        )
    print(f"rust_dom_text_node_parity_cases={len(cases)}")
    print("rust_dom_visible_text_nodes=PASS")
    print("rust_dom_html_serialization_parity=NOT_PROVEN")
    print("rust_dom_malformed_html_parity=NOT_PROVEN")
    print("rust_dom_writer_integration=no")
    print("production_processes_touched=0")


if __name__ == "__main__":
    main()
