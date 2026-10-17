# RE-06: перенос Gemini scheduler из full_cycle_app

Источник — настольная программа `full_cycle_app`, переданная владельцем.
Рабочая конфигурация: один Gemini API-ключ одного Google-проекта; перенос
не вводит мультиключевую схему и не переписывает рабочие алгоритмы.

## Что перенесено
- `src/chaptera_server/gemini_scheduler.py`: исходные 588 строк
  `full_cycle_app/translation/gemini/api/request_scheduler.py` без изменения
  алгоритмов. Адаптирован только import `OperationCancelledError`.
  SHA-256 исходника (без завершающего LF):
  `8fc1b4c2293b08a9911424ff0b0bdeda714cb3531445cbe7a041ff1e72cc24f5`.
  Тест проверяет точность переноса.
- `ScheduledGeminiProvider`: прослойка вокруг имеющегося серверного
  Gemini/SQLite RPD. Порядок допуска:
  desktop RPM/RPS/TPM -> desktop network controller/circuit ->
  atomic SQLite RPD -> HTTP Gemini. Отказ до HTTP не тратит RPD.
- Роли `translation`, `targeted_repair`, `glossary` и cancellation
  поступают из единого TranslationCore и контекстного preflight.
- Для временного HTTP 429 — desktop cooldown и уменьшение RPM на 25%;
  5xx и сетевые ошибки отслеживаются circuit breaker. Суточный 429
  по-прежнему обрабатывает прежняя постоянная SQLite-квота.
- Настольные настройки текущей модели: RPM 5, TPM 250000, верхняя
  граница output 65536; реальное `maxOutputTokens` ограничено 98%,
  как в настольном Gemini handler. Проверяются до сетевого запроса.

## Следующие завершённые блоки

- **PR #38 — атомные контрольные точки:** уже проверенные исходным
  desktop `CoverageMap` пакеты, включая targeted repair, сохраняются
  в SQLite с lease fencing и ключом точного prompt/payload/role/model.
  Срабатывает восстановление после паузы и перезапуска без повторного
  вызова Gemini. Не переносит ещё streaming или контекстные окна.
- **PR #39 — классификация и отложенные повторы:** из
  `full_cycle_app/.../worker_helpers/error_analyzer.py` без изменения
  взят метод `_classify_exception` с приоритетом условий (включая
  provider unavailable перед network). Серверный
  `gemini_error_bridge.py` только превращает очищенные HTTP-коды в
  существующие `ErrorType` через оригинальные классы исключений.
  `TemporaryRateLimitError` (429/RPM), `ProviderUnavailableError`
  (5xx) и `NetworkError` не расходуют конечные `max_attempts`.
  Постоянный `transient_streak` наращивает паузу до 300 секунд,
  `Retry-After` учитывается до 3600 секунд. 401/404/400
  блокируют обработку новых задач в текущем worker, как desktop
  `ABORT_WORKER`; текущая плохая глава получает явную ошибку.
  Дневная квота по-прежнему идёт через RPD и ждёт Pacific reset.
  Непонятные/искажённые 200/200 MAX_TOKENS не объявляются «сетью»:
  пока доступны только **ограниченные countable повторы**, не
  полноценный partial salvage. Полезная работа не перечёркивается
  при временном отказе; сохранённые атомы из PR #38 остаются доступны.

## Что ещё НЕ перенесено

Полный desktop `ErrorAnalyzer.analyze_and_act` включает действия
`split_v2`, `coverage_v2`, `translation_debts`, `finalize_only`,
сессионный model failover, streaming salvage и обработку нескольких
типов ошибок в рамках общего history. Это **не** заявляется
реализованным: GUI и настольная очередь имеют другую
инфраструктурную оболочку, и их не нужно вслепую копировать
в серверную SQLite. Далее переносим **то же поведение** в серверные
порты с мокированными desktop traces и crash-test.

Scheduler настольной программы хранит RPM/TPM/circuit в памяти процесса,
поэтому при рестарте эти минутные окна сбрасываются. Устойчивое суточное
RPD в облаке **не** сбрасывается: оно остаётся в SQLite. Поддерживается
один worker/key по умолчанию. Не заявляется межпроцессная точность RPM
или полное покрытие тарифов.

Нет вызовов платного Gemini и нет VPS deployment.
