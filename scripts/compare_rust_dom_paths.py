#!/usr/bin/env python3
"""RUST-03e: verify DOM text ordinals against Python source_units.

Read-only, synthetic, well-formed HTML only. This is not an acceptance
gate for source cleanup, translation and Python/html5ever serialization.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys

from bs4 import BeautifulSoup
from chaptera_translation_core.utils.source_units import (
    _source_node_dom_path,
    _source_text_nodes,
)


def reference(html: str) -> list[dict[str, str]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    root = soup.body or soup
    nodes = _source_text_nodes(root)
    return [
        {"text": str(node), "dom_path": _source_node_dom_path(root, node)}
        for node in nodes
    ]


def corpus() -> list[str]:
    cases = [
        "",
        "<p>A</p>",
        "<p>Русская глава</p><p>中文第一章。</p>",
        "<p>Before <b>bold</b> after</p>",
        "<p>0<i>italic</i>1<b>bold</b>2</p>",
        "<p><!-- ignored -->Visible</p>",
        "<div> A<!-- comment -->B<i>X</i> C</div>",
        "<p>  Hello  </p><p> World  </p>",
        "<p>A<span> </span>B</p>",
        "<p><em>one</em><strong>two</strong></p>",
        "<p>A &amp; B <b>bold &lt; quote</b> C</p>",
        "<div><h1>Title</h1><p>First</p><p>Second</p></div>",
        "<blockquote><p>Quoted.</p><p>Continuation.</p></blockquote>",
        "<section><article><h2>Chapter</h2><p>Body.</p></article></section>",
        "<ul><li>first</li><li>second</li></ul>",
        "<p>Start.</p><script>secret()</script><p>End.</p>",
        "<p>Start.</p><style>p{color:red}</style><p>End.</p>",
        "<p>Visible.</p><p><!-- comment --></p><p>More.</p>",
        "<html><head><title>Hidden</title></head><body><p>Visible.</p></body></html>",
        "<html><head><title>H</title></head><body><h1>Title</h1><p>A <b>B</b> C</p></body></html>",
        "<html><body><div><p>Body.</p><p>Tail.</p></div></body></html>",
    ]
    rng = random.Random(20261021)
    fragments = ("北风", "a", "Глава", "雨", "Hello", "&amp;", " ", "中", "X")
    tags = ("span", "strong", "i", "em")
    for _ in range(300):
        first = rng.choice(fragments)
        second = rng.choice(fragments)
        third = rng.choice(fragments)
        inline = rng.choice(tags)
        paragraph = f"<p>{first}<{inline}>{second}</{inline}>{third}</p>"
        more = f"<p>{rng.choice(fragments)}</p>"
        cases.append(
            rng.choice((
                paragraph + more,
                f"<div>{paragraph}{more}</div>",
                f"<article><h2>Title</h2>{paragraph}{more}</article>",
                f"<section><blockquote>{paragraph}</blockquote>{more}</section>",
                f"<html><head><title>meta</title></head>"
                f"<body><div>{paragraph}{more}</div></body></html>",
            ))
        )
    return list(dict.fromkeys(cases))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_dom_paths.py /path/to/source-dom-probe")
    cases = corpus()
    expected = [reference(case) for case in cases]
    proc = subprocess.run(
        [sys.argv[1]],
        input="".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        text=True, capture_output=True, timeout=50, check=True,
    )
    outputs = [json.loads(line) for line in proc.stdout.splitlines()]
    assert len(outputs) == len(cases), (
        f"Result count {len(outputs)} does not match corpus {len(cases)}"
    )
    differences = []
    for html, oracle, actual in zip(cases, expected, outputs):
        found = actual["text_paths"]
        if found != oracle:
            differences.append((html, oracle, found))
    if differences:
        for html, python_answer, rust_answer in differences[:12]:
            print(
                f"dom_path_mismatch input={html!r}"
                f"\npython={python_answer!r}\nrust={rust_answer!r}"
            )
        raise AssertionError(
            f"Rust DOM ordinal path mismatch in {len(differences)} of {len(cases)} inputs"
        )
    # Unsupported implicit/malformed full documents may not be passed off
    # as exact Python structural parity.
    unsupported = "<html><head><title>no body</title></head></html>"
    bad = subprocess.run(
        [sys.argv[1]], input=json.dumps(unsupported) + "\n",
        text=True, capture_output=True, timeout=10, check=True,
    )
    assert json.loads(bad.stdout)["text_paths"] is None
    print(f"rust_dom_path_parity_cases={len(cases)}")
    print("rust_dom_path_ordinals=PASS")
    print("rust_dom_path_malformed_full_html=UNSUPPORTED")
    print("rust_dom_path_writer_integration=no")
    print("production_processes_touched=0")


if __name__ == "__main__":
    main()
