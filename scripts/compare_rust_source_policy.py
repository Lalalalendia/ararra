#!/usr/bin/env python3
"""Compare Rust standalone source classification against Python source_units.

Offline, synthetic and deterministic. This covers STANDALONE text only, not
BeautifulSoup DOM order, chapter heading role or normalized source_hash.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys

from chaptera_translation_core.utils.source_units import (
    classify_source_text,
    is_source_tail_boilerplate_text,
)


def corpus() -> list[str]:
    seeds = [
        "", " ", "\n", "你好，世界", "Русская проза", "Hello, world!",
        "本章完", "(本章完)", "（本章完）", "本章完!", "最初的故事",
        "溫馨提示：", "温馨提示:", "温馨提示: в конце предложения",
        "101kanshu.com", "101kks.org", "101read.cn",
        "101看书网", "101看書網", "小说里出现 101kanshu",
        "a character mentioned 101kanshu in narrative",
        "记住本站", "記住我們", "最新小说", "最新小說",
        "本书首发", "本書首發", "最新章节", "最新章節",
        "全文阅读", "全文閱讀", "无错章节", "無錯章節",
        "无乱序", "無亂序", "记得分享", "記得分享",
        "更新不易", "小说站", "小説站", "最快更新",
        "only on our site", "without missing chapters", "latest chapters",
        "first published", "самыесвежиеглавы", "толькоздесь",
        "свежие главы", "без пропусков и ошибок", "верныеглавы",
        "101kanshu.com 看书网书库多", "101kks.com 超顺畅",
        "101kanshu.com 看书网伴你读", "首发 101read.net",
        "下一章", "下章", "下一節", "下一节", "下一回",
        "下一頁", "下一页", "下頁", "下页",
        "next chapter", "Next Chapter: More", "NEXT  CHAPTER",
        "next chapter 45", "下一章：续", "下一章] 小结",
        "第2章", "Глава 2", "chapter 2", "Слово 'глава' в рассказе",
        "A story about the next chapter of life", "第2章爱情",
        "最新章节就出现在正文里了", "下一章是我的最爱",
        "Ｎｅｘｔ　Ｃｈａｐｔｅｒ", "＆ｎｂｓｐ；", "123，！",
        "<p>next chapter</p>", "【下一章】", "（下一章）", "[next chapter]",
        "101kanshu.com", "101kanshu.com!!", "记住本站!",
        "Onlyonoursite", "onlyonoursite" + "X" * 300,
        "最新小说" + "学" * 260, "首发" + "中文" * 260,
        "&amp; 101kks.net", "&#x672c;&#x7ae0;&#x5b8c;",
        "\u200b本\u200c章\u200d完", "最新·小说",
    ]
    rng = random.Random(20261020)
    variants = list(seeds)
    for seed in seeds:
        variants.append("  " + seed + "  ")
        variants.append("【" + seed + "】")
        variants.append(seed + "。")
    # Fuzz the classification boundaries with deterministic ordinary prose,
    # fullwidth forms, promotion tails and ambiguous domain fragments.
    alphabet = [
        "青龙", "любовь", "next chapter", "小说", "101kanshu",
        "记住本站", "和你", "___", "◆", "全文阅读", "温馨提示",
        "the story", "chapter 12", "Chapter", "本章完", "下一页",
        "😊", "一件故事", "之", " ", "𠀀",
    ]
    for _ in range(600):
        parts = rng.choices(alphabet, k=rng.randint(1, 8))
        variants.append(rng.choice(["", "[", "《", "("]) + "".join(parts))
    return list(dict.fromkeys(variants))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_source_policy.py /path/to/source-policy-probe")
    samples = corpus()
    expected = [
        {
            **classify_source_text(sample),
            "tail_hint": is_source_tail_boilerplate_text(sample),
        }
        for sample in samples
    ]
    payload = "".join(json.dumps(sample, ensure_ascii=False) + "\n" for sample in samples)
    proc = subprocess.run(
        [sys.argv[1]], input=payload, text=True,
        capture_output=True, check=True, timeout=30,
    )
    outputs = proc.stdout.splitlines()
    assert len(outputs) == len(expected), (
        f"Source policy result count mismatch {len(outputs)} != {len(expected)}"
    )
    mismatches = []
    for sample, reference, line in zip(samples, expected, outputs):
        current = json.loads(line)
        if current != reference:
            mismatches.append((sample, reference, current))
    if mismatches:
        # These are only synthetic text strings, never user manuscripts.
        for sample, reference, current in mismatches[:10]:
            print(f"mismatch sample={sample!r} python={reference!r} rust={current!r}")
        raise AssertionError(f"Standalone source classification differs on {len(mismatches)} fixtures")
    print(f"rust_source_policy_parity_cases={len(samples)}")
    print("rust_source_policy_standalone_text=PASS")
    print("rust_source_policy_dom_parity=NOT_YET_PORTED")
    print("rust_source_policy_writer_integration=no")
    print("production_processes_touched=0")


if __name__ == "__main__":
    main()
