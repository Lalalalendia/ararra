"""Server golden fixtures adapted from desktop glossary runtime quality tests.

These are SOURCE-DERIVED contracts, not new regex heuristics. The original
desktop gate supplies inflection/lexical family, entity, coreference and
advisory decisions. Here its Qt context is a per-chapter ServerGateContext.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from chaptera_translation_core import ChapterInput, TranslationCore
from chaptera_translation_core.post_translation_context import (
    ServerGateContext, build_gate_context, compile_translation_context,
)
from chaptera_translation_core.translation_gates import run_translation_gates


def local(target: str, note: str = "Термин", **extras) -> dict:
    return dict(rus=target, note=note, scope="project", authority="required",
                provenance={"kind": "project"}, **extras)


def global_advisory(target: str) -> dict:
    return dict(rus=target, note="", scope="global", authority="advisory",
                provenance={"kind": "cross_project_consensus"})


def semantic(target: str, note: str, kind: str = "other") -> dict:
    return dict(
        rus=target, note=note, scope="project", authority="required",
        provenance=dict(source="semantic_preflight_glossary_promotion",
                        promoted_from="session_canonical", semantic_kind=kind,
                        evidence_count=1, confidence=0.75),
    )


def check(source: str, target: str, glossary: dict, *,
          proven: bool = True) -> list[dict]:
    return run_translation_gates(
        source, target, ServerGateContext(glossary),
        ["chapter.xhtml"], alignment_proven=proven,
    )


def findings_of(findings, code):
    return [item for item in findings if item["code"] == code]


def test_desktop_chapter_206_six_real_russian_inflections_do_not_flag_missing():
    glossary = {
        "世界樹": local("Древо Мира", "Система; Концепт; Грамматика: Ср. род"),
        "大源": local("Великий Исток", "Система; Космология; Грамматика: Муж.род"),
        "帽匠": local("Шляпники", "Организация; Грамматика: Мн.число; Одуш."),
        "魔王之手原型機": local("Прототип Руки Владыки Демонов", "Артефакт; Муж.род"),
        "七環": local("седьмое кольцо", "Ранг; Ср. род; Уровень силы"),
        "魔族": local("демоны", "Раса; Грамматика: Мн.число"),
    }
    source = "<body><p>世界樹</p><p>大源</p><p>帽匠</p><p>魔王之手原型機</p><p>七環</p><p>魔族</p></body>"
    target = ("<body><p>силой Древа Мира</p><p>благословение Великого Истока</p>"
              "<p>отряд Шляпников</p><p>тринадцать Прототипов Руки Владыки Демонов</p>"
              "<p>силы седьмого кольца</p><p>армия демонов</p></body>")
    assert not findings_of(check(source, target, glossary), "TERM_REQUIRED_MISSING")


def test_global_advisory_is_not_project_required():
    glossary = {"教團": global_advisory("Культ")}
    source = "<p>邪神教團已經消失。</p>"
    target = "<p>Секты злых богов почти исчезли.</p>"
    compiled = compile_translation_context(ServerGateContext(glossary), source)
    assert compiled["required"] == []
    assert [term["s"] for term in compiled["advisory"]] == ["教團"]
    assert not [item for item in check(source, target, glossary)
                if item["code"].startswith("TERM_")]


def test_longest_exact_source_span_suppresses_nested_project_term():
    glossary = {
        "灰燼教團": local("Пепельный Орден", "Организация"),
        "教團": local("Культ", "Организация"),
    }
    source = "<p>灰燼教團</p>"
    ctx = ServerGateContext(glossary)
    assert {matched[-1] for matched in
            ctx.regex_service.find_exact_matches_with_spans_in_nodes(["灰燼教團"])} == {
                "灰燼教團",
            }
    compiled = compile_translation_context(ctx, source)
    assert [row["s"] for row in compiled["required"]] == ["灰燼教團"]
    assert not findings_of(check(source, "<p>Пришёл Пепельный Орден.</p>", glossary),
                           "TERM_REQUIRED_MISSING")


def test_nested_source_subterm_not_required_inside_long_named_artifact():
    glossary = {
        "葬寶境": local("Мир Захороненных Сокровищ", "Локация"),
        "葬寶境控制令": local("жетон управления Миром Захороненных Сокровищ", "Артефакт"),
    }
    source = "<p>葬寶境控制令在他手中。</p>"
    target = "<p>Жетон управления Миром Захороненных Сокровищ был у него в руках.</p>"
    assert not findings_of(check(source, target, glossary), "TERM_REQUIRED_MISSING")


def test_desktop_person_coreference_after_canonical_name_is_allowed():
    g = {"羅蘭": local("Роланд", "Персонаж; Мужчина; Роланд (Муж.род; Скл.)")}
    f = check("<body><p>羅蘭 вошёл.</p><p>羅蘭 ответил.</p></body>",
              "<body><p>Роланд вошёл.</p><p>Он ответил.</p></body>", g)
    assert not [z for z in f if z["code"].startswith("TERM_ENTITY_")]


def test_wrong_project_local_person_is_source_owned_entity_substitution():
    glossary = {
        "老蘭斯洛": local("Старик Ланселот", "Персонаж; Мужчина"),
        "考爾·蘭斯洛": local("Коул Ланселот", "Персонаж; Мужчина"),
    }
    findings = check("<p>老蘭斯洛留下了文字。</p>",
                     "<p>Коул Ланселот оставил запись.</p>", glossary)
    f = findings_of(findings, "TERM_ENTITY_SUBSTITUTION")
    assert len(f) == 1 and f[0]["blocking"]
    assert f[0]["evidence"]["source"] == "老蘭斯洛"
    assert f[0]["evidence"]["substitutions"] == [
        {"source": "考爾·蘭斯洛", "target": "Коул Ланселот"},
    ]
    assert f[0]["evidence"]["target_slot_ids"] == ["TGT_000000"]


def test_project_person_alias_drift_detected_but_later_pronoun_not_rejected():
    g = {"紅魔鬼": semantic("Азазель", "type=person; Красный Дьявол", "person")}
    f = check("<body><p>紅魔鬼站在门口。</p><p>紅魔鬼没有回答。</p></body>",
              "<body><p>Красный Дьявол стоял у двери.</p><p>Он не ответил.</p></body>", g)
    drift = findings_of(f, "TERM_ENTITY_ALIAS_DRIFT")
    assert len(drift) == 1 and drift[0]["blocking"]
    assert drift[0]["evidence"]["target_slot_ids"] == ["TGT_000000"]
    assert all("TGT_000001" not in z["evidence"].get("target_slot_ids", []) for z in drift)


def test_bozhestvenniy_dvorets_irregular_fleeting_vowel_accepted():
    g = {"神府": local("Божественный Дворец", "Локация; Муж.род")}
    assert not findings_of(check("<p>他回到了神府。</p>",
                                 "<p>Он вернулся из Божественного Дворца.</p>", g),
                           "TERM_REQUIRED_MISSING")


@pytest.mark.parametrize(("key", "target", "source", "translated"), [
    ("X戰警", "Люди Икс", "<p>X戰警 появились.</p>",
     "<p>Он сражался против Людей Икс.</p>"),
    ("艾德曼合金", "адамантий", "<p>艾德曼合金非常坚固。</p>",
     "<p>Скелет был выплавлен из адамантия.</p>"),
    ("振金", "вибраниум", "<p>振金矿石非常珍贵。</p>",
     "<p>Ваканда владела запасами вибраниумовой руды.</p>"),
    ("隱身", "невидимость", "<p>隱身的變種人发动袭击。</p>",
     "<p>Невидимый мутант внезапно атаковал.</p>"),
])
def test_desktop_real_world_inflection_and_lexical_family(key, target, source, translated):
    row = semantic(target, "type=technique; способность мутанта", "technique") if key == "隱身" else (
        semantic(target, "металл из Ваканды") if key == "振金" else local(target))
    assert not findings_of(check(source, translated, {key: row}), "TERM_REQUIRED_MISSING")


def test_semantic_generic_flexible_term_can_use_nonliteral_synonym():
    g = {"吸血鬼": semantic("вампир", "основная угроза в главе")}
    assert not findings_of(check("<p>吸血鬼扑了过来。</p>",
                                 "<p>Кровосос бросился вперёд.</p>", g),
                           "TERM_REQUIRED_MISSING")


def test_named_group_missing_is_high_and_locatable_without_auto_rejecting():
    g = {"哈鬼族": semantic("хагуйцзу", "люди-слуги вампиров")}
    f = findings_of(check("<p>哈鬼族挡住了去路。</p>",
                          "<p>Прислужник преградил путь.</p>", g),
                    "TERM_REQUIRED_MISSING")
    assert len(f) == 1 and f[0]["blocking"]
    assert f[0]["evidence"]["target_slot_ids"] == ["TGT_000000"]


def test_material_omission_is_not_confused_with_legitimate_adjective_form():
    g = {"振金": semantic("вибраниум", "металл из Ваканды")}
    f = findings_of(check("<p>振金是关键材料。</p>",
                          "<p>Без такой защиты пришлось искать другой выход.</p>", g),
                    "TERM_REQUIRED_MISSING")
    assert len(f) == 1 and f[0]["blocking"]
    assert f[0]["evidence"]["enforcement"] == "lexical_family"


def test_established_multiword_short_form_is_info_but_unestablished_is_missing():
    g = {"陰魔": local("темный демон", "Раса")}
    f = check("<body><p>陰魔 появился.</p><p>陰魔 отступили.</p></body>",
              "<body><p>Появился темный демон.</p><p>Демоны отступили.</p></body>", g)
    assert not findings_of(f, "TERM_REQUIRED_MISSING")
    short = findings_of(f, "TERM_CANONICAL_SHORT_FORM")
    assert len(short) == 1 and short[0]["severity"] == "info"
    assert short[0]["evidence"]["slot_evidence"][0]["observed_core_surface"] == "Демоны"
    assert len(findings_of(check("<p>陰魔 отступили.</p>",
                                 "<p>Демоны отступили.</p>", g),
                           "TERM_REQUIRED_MISSING")) == 1


def test_mutant_family_requires_initial_canonical_establishment():
    g = {"變種人": local("мутант", "Раса")}
    source = "<body><p>變種人 появился.</p><p>變種人 бежала.</p><p>變種人 протесты.</p></body>"
    target = ("<body><p>Появился мутант.</p><p>Мутантка бежала.</p>"
              "<p>Начались антимутантские протесты.</p></body>")
    f = check(source, target, g)
    assert not findings_of(f, "TERM_REQUIRED_MISSING")
    surfaces = [e["observed_core_surface"] for finding in
                findings_of(f, "TERM_CANONICAL_SHORT_FORM")
                for e in finding["evidence"]["slot_evidence"]]
    assert "Мутантка" in surfaces
    assert "антимутантские" in surfaces
    bad = check("<p>變種人 бежала.</p>", "<p>Мутантка бежала.</p>", g)
    assert len(findings_of(bad, "TERM_REQUIRED_MISSING")) == 1


def test_adjacent_anaphora_works_only_after_canonical_slot():
    g = {"洛杉磯": local("Лос-Анджелес", "Локация")}
    f = check("<body><p>洛杉磯.</p><p>洛杉磯 будет целью.</p></body>",
              "<body><p>Это Лос-Анджелес.</p><p>Он устроит нечто именно там.</p></body>", g)
    assert not findings_of(f, "TERM_REQUIRED_MISSING")
    short = findings_of(f, "TERM_CANONICAL_SHORT_FORM")
    assert len(short) == 1
    assert short[0]["evidence"]["slot_evidence"][0]["observed_core_surface"] == "там"
    missing = check("<p>洛杉磯 будет целью.</p>",
                    "<p>Он устроит нечто именно там.</p>", g)
    assert len(findings_of(missing, "TERM_REQUIRED_MISSING")) == 1


def test_missing_alignment_is_warning_only_not_strict_gate():
    g = {"老蘭斯洛": local("Старик Ланселот", "Персонаж")}
    findings = check("<p>老蘭斯洛 ушёл.</p>",
                     "<p>Коул Ланселот ушёл.</p>", g, proven=False)
    assert findings_of(findings, "ALIGNMENT_UNPROVEN_TERM_QA")
    assert not any(f["blocking"] for f in findings)


def test_source_attribute_does_not_oblige_glossary_word():
    g = {"世界樹": local("Древо Мира")}
    findings = check('<p data-hidden="世界樹">Другой текст.</p>',
                     "<p>Другой текст.</p>", g)
    assert not findings_of(findings, "TERM_REQUIRED_MISSING")


def test_integrated_atomic_translation_emits_diagnostics_without_rejecting_result():
    class Provider:
        calls = 0
        async def generate(self, request):
            self.calls += 1
            packet = json.loads(request.source_html_with_markers)
            indexes = {name: idx for idx, name in enumerate(packet["columns"])}
            groups = []
            for row in packet["rows"]:
                item = {name: row[idx] for name, idx in indexes.items()}
                if groups and groups[-1][0]["render_domain_id"] == item["render_domain_id"]:
                    groups[-1].append(item)
                else:
                    groups.append([item])
            return json.dumps({"segments": [
                {"covers": [x["atom_id"] for x in g],
                 "translation": "Коул Ланселот оставил запись."}
                for g in groups
            ]}, ensure_ascii=False)

    events = []
    chapter = ChapterInput(
        "ch1", "<body><p>老蘭斯洛 оставил запись.</p></body>",
        glossary={
            "老蘭斯洛": local("Старик Ланселот", "Персонаж"),
            "考爾·蘭斯洛": local("Коул Ланселот", "Персонаж"),
        },
        chapter_path="chapter.xhtml", transport="atoms_v1",
    )
    provider = Provider()
    result = asyncio.run(TranslationCore(
        provider, on_event=lambda event: events.append(event),
    ).translate(chapter))
    assert "Коул Ланселот" in result.translated_html
    assert provider.calls == 1
    summaries = [e.detail for e in events if e.stage == "glossary_qa_summary"]
    assert len(summaries) == 1 and "would_block=1" in summaries[0]
    finding_events = [e.detail for e in events if e.stage == "glossary_qa_finding"]
    assert any("TERM_ENTITY_SUBSTITUTION" in msg for msg in finding_events)
    assert all("Коул Ланселот" not in msg for msg in finding_events)
    assert all("старик" not in msg.casefold() for msg in finding_events)


def test_integrated_html_v0_never_claims_proven_positional_alignment():
    # The HTML-v0 provider is under no atom/DOM scope ownership; do not
    # assert a source→target slot correspondence based on matching counts.
    class Provider:
        async def generate(self, request):
            raise AssertionError("Not a glossary gate test of HTML-v0 provider")

    from chaptera_translation_core.service import TranslationCore
    events = []
    core = TranslationCore(Provider(), on_event=events.append)
    chapter = ChapterInput("c", "<p>老蘭斯洛</p>",
                           glossary={"老蘭斯洛": local("Старик Ланселот", "Персонаж")})
    core._diagnose_terminology(chapter, "<p>老蘭斯洛</p>",
                               "<p>Псевдоним</p>", alignment_proven=False)
    assert any(e.stage == "glossary_qa_finding" and "ALIGNMENT_UNPROVEN_TERM_QA" in e.detail
               for e in events)
    assert not any("would_block=1" in e.detail for e in events)
