# RE-06 — аудит тестов настольного full_cycle_app и контракты РидЭры

**Контрольная точка:** 17.10.2026. **Владелец алгоритмов:** существующая настольная программа. **Цель:** проверяемый перенос поведения, а не слепое копирование GUI или новой системы квот.

## Проверенный материал

- Архив `full_cycle_app31.zip` от 15.10.2026: **190** Python-файлов `tests/test_*.py`, **970** функций `test_*` (AST-индекс, не число pytest-параметризаций). Исходная папка `tests/` и воспроизводимые fixtures изучены отдельно от исходного кода.
- Более свежий полный экспорт **471 исходного файла** от 17.10.2026; в этом экспорте тестов нет. Из **96** Gemini-модулей AST-структура **86** эквивалентна архиву, **10** изменились: `api/config.py`, `api/model_discovery.py`, `core/translation_text_salvage.py`, `utils/chapter_context.py`, `utils/glossary_enforcement.py`, `utils/language_tools.py`, `utils/rpd_ledger.py`, `utils/semantic_preflight.py`, `utils/similarity.py`, `utils/txt_importer.py`. Устаревшие ожидания для этих файлов нельзя принимать без проверки.
- Текущий `ararra/main`: отдельный FastAPI + Jinja2 + SQLite, **34** Python-файла тестов перед началом данного PR, с обязательными Python 3.11/3.12 и настоящим Chromium. Количество файлов НЕ является метрикой покрытия. Offline PHPUnit/PyQt тесты не обязаны запускаться в веб-проекте.

## Проверки по функциональным группам

| Настольные тесты (из полного архива) | Проверяемое поведение | Адаптация к серверу | Приоритет/статус |
|---|---|---|---|
| `test_translation_contamination_guards.py::test_later_overlapping_cover_does_not_erase_first_proven_segment` | Первый корректный atom proof сохраняется, позднее повторное покрытие отбрасывается; непокрытый сосед должен получить debt | `parse_packet_response` до этого ошибочно отклонял **весь** ответ. Портирован алгоритмический случай + реальный `TranslationCore`/targeted repair. | **P0; исправлено в этом PR** |
| `test_translation_response_identity_normalization.py` (9) | ID/job/segment принадлежат источнику; `}{` JSON, пунктуация, comma-packed ID; неизвестный атом не угадывать | `reconcile_coverage_response` уже общий desktop core. Добавлены регрессии на провайдерские ID/JSON и отрицательные ID cases; **multi-job** отложен до пакетного перевода. | P0; частично добавлено |
| `test_translation_recovery_false_positive_regression.py` (15), `test_translation_contamination_guards.py` (9) | Семантическое дублирование двух *разных* исходников, сохранение авторских повторов, короткие атрибуции, структурные phase-shift окна | Переиспользовать `_recovery_duplicate_debts`, `target_contamination_suspected`, `repair_domains`, а не создавать новый classifier. В новом наборе — парафразы, авторский повтор и сохранение первой правки. | **P0; оставшиеся границы — следующий PR** |
| `test_translation_text_salvage.py` (7) | HTML/Markdown-пыль, математика, literal markup, внутренние TSEG, безопасная локальная санация ответа | Pure desktop `salvage_translation_text` уже подключён к atom reconciler. Необходимо проверить точные fixtures и отсутствие публикации некорректных маркеров в Jinja/экспорте. | P0; дополнительные golden cases |
| `test_app37_deterministic_coverage_authority.py` (6), `test_translation_compact_atom_protocol.py` (6) | Домен рендера важнее просьбы в prompt; корректная columnar-упаковка, восстановление без ложной позиции | `atoms_v1` и `build_render_plan` уже используют desktop. Перенести mutation/golden проверки, особенно потенциальное разрастание >24 атомов. | P0/P1; частично покрыто |
| `test_translation_debt_scope_isolation.py` (8) | Нельзя применить чужой debt, исходный scope hash должен переживать смену mutable target context | SQLite checkpoints источника/модели + lease уже есть (#38–44). Реальное cross-chapter debt pack пока отсутствует, нельзя имитировать этот сценарий фиктивно. | P0; отдельный RE-06C |
| `test_gemini_network_resilience.py` (18) | Partial-stream, 503/network, circuit state, UTF-8 по кускам, idle timeout, нет двойной оплаты | #39/#42/#43 покрыли классификацию, partial atom recovery и mock SSE. Остаются нестриминговый усечённый JSON, trace DNS/TLS и stall/non-offline тонкости. | **P0**, тестировать через `httpx.MockTransport` |
| `test_provider_error_taxonomy.py` (16) | 400/401/403/404 vs 429/503, machine-readable error, ключ не в URL/логах | `tests/test_desktop_error_bridge.py`, `tests/test_provider.py`, один key/project. **Не переносить** desktop rotation/key-pool поведение. Проверить детализацию 400 `API_KEY_INVALID`. | P0; дополнить |
| `test_rpd_sqlite_ledger.py` (4), `test_adaptive_key_spend.py` (6), `test_rpd_snapshot_scheduler.py` (6) | Atomic admission и лимит суточных вызовов, time window, справедливые расходы | `tests/test_gemini_daily_quota.py` проверяет SQLite RPD 21 под одновременными вызовами и Pacific/DST. Desktop **multi-key snapshots** и автоматическое вращение ключей не переносим при одном ключе. | P0/P1; частично |
| `test_chapter_context_pipeline.py` (28) | Source/glossary/predecessor fingerprint, недоверие к позиционным ответам и stale context | `tests/test_windowed_context.py` проверяет оконность, restart и source fence. Дополнить provider response ambiguity и alias/evidence cases из desktop. | P1 |
| `test_glossary_runtime_quality_contracts.py` (26), `test_semantic_preflight.py` (12) | Project-local обязательные термины vs advisory, русские падежи и местоимения без false positive, семантический owner | `prompt_policy.py` переносит authority. Но prompt binding сам по себе **не доказывает** послепереводную лексическую приёмку: нужен отдельный offline QA-набор и решение, какие проверки блокируют публикацию. | **P0/P1; пробел** |
| `test_semantic_invariants_overlay.py` (12), `test_translation_storage.py` (6) | Source refresh не стирает ручную правку; stale worker CAS; атомарные claims, WAL/backup | Серверные `tests/test_editorial.py`, `tests/test_queue_store.py`, `tests/test_worker.py`; адаптировать новые сценарии на **книгу, модератора, ревизии и SQLite**, не на PyQt project. | P1 |
| `test_translation_pack_scope_isolation.py` (7), `test_translation_cost_authority.py` (4) | Request-local IDs, пустой/дублированный pack, единственный Gemini request для нескольких работ, оценка затрат | Сейчас каждая книга/глава имеет независимое job и quota ledger. Пакеты — отдельная RE-06C фаза с immutable input, idempotency и честным RPD. **Не включать** до доказательств scope isolation. | P1; ещё не реализовано |
| `test_source_* / test_*_source_parser / test_rulate_*` | Сайты, браузер, парсинг, контент с внешних сервисов | Не переносить слепо: РидЭра — командный веб-редактор с подтверждённым импортом. Использовать лишь общие invariants: порядок глав, отсутствие тихой потери, дубли заголовков, корректность импорта. | P2; специфика другого продукта |
| Desktop PyQt/Chrome/Boosty/статистика и многочисленные тесты окон | Рабочий стол, Qt event bus, настройки UI, сайты и сервисы | В вебе эквивалент — `httpx.ASGITransport`, Jinja/JS, настоящий Chromium, админ + модераторы. React/Vite не возвращать. | P2 / не переносится |

## Правила перевода тестов

1. Для каждого кандидата записать: **исходный файл/функция → проверяемый invariant → актуален ли на свежем desktop → соответствующий server слой → позитивный/негативный кейс → CI результат**.
2. Одинаковая функция Python ещё не означает одинаковый контракт. Desktop task manager, PyQt bus, key-pool, multi-job packs, browser scraping и Windows session не участвуют в серверном тесте. Их *суть* портируется только там, где функция существует в РидЭре.
3. Все проверенные text/coverage/repair результаты привязывать к immutable исходнику, модели, glossary/context revision, активной lease и owner книги. Не принимать покрытие чужой главы или лишний JSON как финальный перевод.
4. При частичном результате: сохранять **доказанное**, долги отмечать отдельно, retry выдавать исключительно на недостающие source-owned domains; `finalize_only` — 0 Gemini-вызовов. Не маскировать пропуск красивой финализацией HTML.
5. Квоты: **один ключ Gemini / один Google-проект**, модель и локальные RPD/RPM/TPM параметры явные. Ошибки и временные блокировки проверять без реального Google API и без расходов.
6. Продуктовые роли: администратор и модераторы; у книги сохраняется владелец-модератор, он переназначаемый. Ревизии ручных переводов и source CAS не должны исчезать от автоматического повтора.

## Последовательность следующих PR

- **#45 (этот PR):** источник владеет атомами; позднее перекрытие не стирает первый доказанный сегмент; изолированные отрицательные паритетные регрессии на неизвестные/несмежные ID, punctuation/JSON salvage и contamination. Обязателен реальный Python 3.11/3.12, Web/Jinja, Chromium 4/4.
- **RE-06-PARITY-QA-02:** перенести 15 recovery false-positive и 26 glossary QA сценариев как bounded offline golden tests. Обязательно разделить deterministic blocking и advisory, не выдать эвристику за истину.
- **RE-06-PARITY-NET-03:** malformed non-stream JSON partial, network reason/status/idle and quota reset table. Source proof + RPD invariants; никаких параллельных новых лимитеров.
- **RE-06-PARITY-STATE-04:** ручные/принятые редакции, owner transfer, source refresh, source stale/CAS и crash-recovery SQLite; реалистичный HTTP/SQL сценарий.
- **RE-06C:** multi-chapter batch, request-local job identity и бюджет. Копирование `multi-key` невозможно без изменения пользовательской архитектуры — не цель.
- **RE-06D:** безопасный TXT/HTML экспорт только из текущих принятых глав, с детальным отказом при дубликатах, пропусках и устаревшем исходнике.

**Никакие тесты из архива не считаются PASS в облаке, пока они не адаптированы и не запущены в ararra CI.** Основание для решения — наблюдаемые assertions, не количество файлов.
