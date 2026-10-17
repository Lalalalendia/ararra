
# RE-06D-EXPORT-01 — проверяемый TXT/HTML экспорт принятых глав

## Контракт и границы

BookLibrary хранит RAW, source_revision, Accepted RU и accepted_source_revision,
но не хранит утверждённое количество глав книги. Поэтому при экспорте
полного произведения оператор **обязательно** задаёт expected_last_chapter.
В данном инкременте допустим только полный непрерывный диапазон 1..N.
Книги с номерами 640..700, намеренные частичные тома и предисловие не
объявляются готовой полной книгой; для них необходим отдельный явно
помеченный режим. Это намеренное fail-closed ограничение.

## HTTP API

Все маршруты требуют командный X-API-Key; владелец книги — ответственность,
а не ACL, и любой участник команды имеет те же права на экспорт.

1. GET /v1/books/{book_id}/export/preview?expected_last_chapter=N
   возвращает book_id, title, expected_last_chapter, source_chapter_count,
   ready_chapter_count, ready, issue_count, issues, manifest_sha256.
   Никакого текста рукописи в preview нет.
2. Если ready=true: GET /v1/books/{book_id}/export?expected_last_chapter=N&format=txt&expected_manifest_sha256=SHA
   либо format=html. Это attachment, Cache-Control: no-store, private,
   nosniff, ETag, X-ReadEra-Content-SHA256 и X-ReadEra-Manifest-SHA256.
3. Скачивание заново читает книгу одной SQLite транзакцией и требует ровно
   тот же manifest SHA-256. При изменении источника, Accepted, глоссария,
   названия или любой части текста после preview — 409 без рукописи.
4. 404 — неизвестная книга; 422 — недопустимые параметры; 409 — пропуски,
   непрошедшие Acceptance, устаревшие Accepted, небезопасный HTML или
   несовпадение receipt. Preview перечисляет missing_source, not_accepted,
   stale_accepted, source_beyond_declared_end, unsafe_accepted_html,
   export_size_or_count_limit. Возвращается до 100 подробных замечаний
   плюс общий issue_count.

## Целостность и чистота текста

- Берутся только главы с принятым RU текущей source revision; статус worker
  completed или наличие Draft не равны Accepted.
- Заголовки создаются из индекса как «Глава N», повторный начальный
  заголовок с тем же номером удаляется.
- HTML экспорт разрешает небольшой набор структурных тегов; все атрибуты,
  внешние ресурсы, ссылки и скрипты запрещены. Явные QA/service блоки,
  комментарии и маркеры — ошибка, а не скрытое удаление.
- Простой абзац с замаскированной редакторской заметкой невозможно
  автоматически отличить от художественного текста; литературная QA
  остаётся необходимой.
- Manifest SHA-256 фиксирует book ID/title, объявленный конец книги,
  glossary revision, chapter ID/index/path, source blob hash/revision,
  Accepted RU hash/revision. Это основа для будущего S3 transport,
  а не реализация S3-загрузки.
- Ограничено 2000 главами, 32 МиБ суммарных RAW+Accepted байтов и
  36 МиБ выходного файла. Большие книги требуют отдельного stream export.

## Приёмка

tests/test_book_export.py: реальный HTTP API без Gemini/оплаты, 401,
полные TXT/HTML и хэши, пропущенные/лишние/старые главы, 409 после изменения
между preview/download, запрет скриптов/QA, предел памяти и подмена данных.
Обязательны Python 3.11/3.12, архитектурный контракт и Chromium.

Вне scope: EPUB, batch submissions, S3/Akula/Google/Яндекс, billing, VPS.
