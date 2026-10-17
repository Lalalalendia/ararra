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

## Принципиальные границы
Это **первый завершённый блок миграции**, а не вся настольная подсистема.
Desktop `ErrorAnalyzer`, streaming partial salvage, `coverage_v2`,
`translation_debts`, `split_v2`, `finalize_only` и per-atom durable
checkpoints пока НЕ подключены к SQLite очереди РидЭры. Следующими PR
переносим их контракты и сравниваем одинаковые mocked error/response traces.

Scheduler настольной программы хранит RPM/TPM/circuit в памяти процесса,
поэтому при рестарте эти минутные окна сбрасываются. Устойчивое суточное
RPD в облаке **не** сбрасывается: оно остаётся в SQLite. Поддерживается
один worker/key по умолчанию. Не заявляется межпроцессная точность RPM
или полное покрытие тарифов.

Нет вызовов платного Gemini и нет VPS deployment.
