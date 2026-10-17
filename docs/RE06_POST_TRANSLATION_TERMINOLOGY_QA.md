# RE-06 — послепереводная проверка терминологии (diagnostic-only)

**Источник алгоритма:** настольный translation/gemini/utils/translation_gates.py, сверенный со свежим экспортом кода от 17.10.2026. Перенесены оригинальные правила морфологии, обязательных glossary-терминов, alias drift и подмены персонажей. Меняются только зависимости от контекста и подготовленных source/target slots; PyQt, менеджеры проектов и API-ключей не импортируются.

## Серверное поведение

- После штатной обязательной structural/source-owned проверки TranslationCore вызывает исходный run_translation_gates.
- Для atoms_v1 source→target matching допускается только после полной проверки atom coverage, DOM render domains и provenance. Для html_v0 alignment_proven=False: число видимых HTML-нод не является доказательством позиционного соответствия.
- ServerGateContext переиспользует текущие scoped_glossary, select_source_bindings и infer_enforcement. ExactSourceSpanMatcher выбирает **самое длинное непересекающееся исходное выражение**, поэтому составной CJK-термин подавляет вложенный ключ (включая required и advisory).
- Каталог обязательных имен включает персонажей, отсутствующих в исходнике главы, чтобы обнаружить подмену одного установленного имени другим.
- События задания: glossary_qa_summary (количество findings и would_block), glossary_qa_finding (код, severity, флаг would_block, target slot ID) и glossary_qa_unavailable (тип исключения). В события не копируется текст исходника, результата или ключ Gemini.
- Никаких дополнительных Gemini-запросов, отдельных лимитеров, изменения ролей и API-поверхности. По умолчанию по-прежнему **один ключ от одного Google-проекта**.

## Почему не включена автоматическая блокировка

Даже finding с историческим desktop blocking=True в этой итерации представляет только **потенциальное** нарушение. Он не блокирует перевод, не изменяет accepted_html и не вызывает новый billable repair. Если QA-модуль не готов, возвращается glossary_qa_unavailable, а уже прошедшая старые проверки глава доступна для ручной редакции. Это не полноценная публичная терминологическая приёмка.

Настольные золотые тесты и свежий источник имеют разный возраст: language_tools.py поменялся между архивом тестов и исходным дампом. Необходимо доказать source-target slot binding и исключить ложную блокировку правильных склонений до автоматического enforcement.

## Адаптированные regression cases

tests/test_desktop_terminology_gates.py проверяет морфологию из главы 206; advisory; вложенный longest-exact CJK; правильную анафору персонажа; substitution Старик Ланселот → Коул Ланселот; alias drift; Божественного Дворца; Людей Икс; адамантия; вибраниумовой; невидимый; flexible синоним; missing именованной группы; литературную short form только после canonical anchor; формы мутант/мутантка/антимутантские; анафору Лос-Анджелес/там; отсутствие source→target proof; игнорирование HTML-атрибутов и настоящий mocked atom translation без утечки текстов в диагностические события.

**Acceptance gate:** Python 3.11/3.12, native Web/Jinja и настоящий Chromium. Все Gemini-вызовы в тестах offline.

## Остаётся отдельной задачей

1. Безопасное сохранение детальных findings с source snapshot/revision/lease в защищённой области и read-only модераторский интерфейс.
2. Ручная отметка reviewed/waived без потери accepted history.
3. Согласованные строгие blocking-классы только после корпусных false-positive golden tests, затем точечный repair по установленным source-owned slots.
4. Recursive split_v2 и multi-chapter batch/export по плану RE-06.

Ссылки: Issue #33, docs/RE06_DESKTOP_TEST_PARITY_AUDIT.md, PR в текущей ветке.
