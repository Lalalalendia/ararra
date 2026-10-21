#!/usr/bin/env python3
"""RUST-04: exact source text split boundaries compared with Python.

All fixtures are synthetic and offline. Unicode indexes are Python codepoints;
Rust must preserve identical UTF-8 text, chunk positions and separator bias.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys

from chaptera_translation_core.utils.source_units import split_source_text_node


def corpus() -> list[dict]:
    seeds = [
        "", "a", "hello world",
        "这是一个中文句子。接下来继续写。第三句！",
        "Глава первая. Сюжет продолжается. Конец!", "ReadEra в России.",
        "A paragraph. The next one! And another? End; okay, fine.",
        "I will speak… and ask questions？最后。", "雨" * 500,
        "a" * 479 + " " + "b" * 300,
        "ab" * 360 + "。 " + "cd" * 300,
        "e\u0301" * 520 + " café.",
        "😊" * 720 + " 终章", "𠀀" * 780,
        "  Leading and trailing spaces  " * 55,
        "\n\n" * 650 + "end", "A. " * 700,
        "AB! " * 500, "A? " * 500, "A; " * 500, "A, " * 500,
        "αβγδε" * 160, "漢字𠮷𝄞" * 160,
    ]
    # Tackle rfind endpoint asymmetry: the latest match may end at max+1
    # and be ignored rather than triggering an earlier separator hit.
    for prefix in range(0, 40):
        for symbol in ("。", ". ", "\n\n", "！", "… "):
            seeds.append("a" * (prefix + 2) + symbol + "b" * 61 + " " + "c" * 18)

    cases = []
    for value in seeds:
        for limit in (1, 2, 3, 5, 8, 32, 63, 64, 128, 479, 480):
            cases.append({"text": value, "max_chars": limit})

    randomizer = random.Random(20261021)
    syllables = [
        "汉字", "повесть", "character", "😊", "𠀀", "é", "e\u0301", "。", "！",
        "？", "… ", ". ", "! ", "? ", "; ", ", ", " ", "\n\n",
        "\n", "\t", "“", "»", "第123章", "  ", "—", "A", "b",
    ]
    for _ in range(900):
        chosen = randomizer.choices(syllables, k=randomizer.randrange(1, 190))
        source = "".join(chosen)
        limit = randomizer.choice(
            [1, 2, 3, 7, 8, 11, 16, 31, 32, 47, 64, 96, 127, 128, 199, 480]
        )
        cases.append({"text": source, "max_chars": limit})

    for limit in (-4, -1, 0):
        cases.append({"text": "not allowed", "max_chars": limit})

    # Deterministic dedup by the full text/max contract.
    return list({json.dumps(item, ensure_ascii=False): item for item in cases}.values())


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_source_split.py /path/to/source-split-probe")
    cases = corpus()
    expected = []
    for case in cases:
        source = case["text"]
        n = case["max_chars"]
        try:
            chunks = split_source_text_node(source, max_chars=n)
        except ValueError:
            assert n <= 0
            expected.append({"error": "invalid_max_chars"})
            continue
        assert chunks, case
        assert "".join(chunks) == source, "Python lost source text"
        assert all(len(part) <= n for part in chunks), "Python exceeded limit"
        expected.append({"chunks": chunks})

    batch = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)
    run = subprocess.run(
        [sys.argv[1]], input=batch, text=True, capture_output=True,
        check=True, timeout=75,
    )
    found = [json.loads(s) for s in run.stdout.splitlines()]
    assert len(found) == len(expected), (
        f"Rust generated {len(found)} answers for {len(expected)} inputs"
    )
    differences = []
    for request, reference, actual in zip(cases, expected, found):
        if actual != reference:
            differences.append((request, reference, actual))
    if differences:
        for case, python_answer, rust_answer in differences[:8]:
            print(
                f"source_split_mismatch request={case!r}\n"
                f"python={python_answer!r}\nrust={rust_answer!r}"
            )
        raise AssertionError(
            f"Rust splitter mismatched Python on {len(differences)} of {len(cases)} cases"
        )

    print(f"rust_source_split_parity_cases={len(cases)}")
    print("rust_source_split_unicode_codepoint_boundaries=PASS")
    print("rust_source_split_separator_precedence=PASS")
    print("rust_source_split_byte_preservation=PASS")
    print("rust_source_split_invalid_limit=PASS")
    print("rust_source_split_worker_integration=no")
    print("production_processes_touched=0")
    print("gemini_calls=0")


if __name__ == "__main__":
    main()
