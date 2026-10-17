# РидЭра — рабочий импорт книг на Jinja2 (RE-02/03/05)

**Актуальная реализация:** FastAPI `/app/import`, Jinja2 +
`src/chaptera_server/web_ui/static/js/readera.mjs` +
`import-model.mjs`. Старый React/Vite импорт из PR #19 служил пилотом;
он не поддерживается и удалён после
[инженерной RE-05 приёмки](READ_READERA_RE05_VISUAL_ACCEPTANCE.md).

## Сценарий

1. Открыть `/app/import` после входа в общий закрытый кабинет. Ключ
   участника остаётся только в памяти вкладки, передаётся API через
   `X-API-Key`. Нет localStorage, cookie-login, токенов в URL.
2. Выбрать или перетащить TXT, HTML/HTM/XHTML, EPUB либо DOCX до **12 МиБ**.
   Rulate JSON — только метаданные, без текстов глав: через staged import
   не создаёт главы. `POST /v1/imports/stages` создаёт actor-owned stage
   на 30 минут, с безопасным preview. Временные квоты и размер проверяет
   **сервер**, а не только форма.
3. Просмотреть номера, названия и samples. Для дубликатов/отсутствующих
   номеров не угадывать destination. Каждой главе назначить
   `create / replace / skip`; префикс/предисловие — только с явным решением.
4. Новая книга получает ID, название и активного модератора-владельца;
   без третьей роли и без per-book ACL. При замене явно загрузить и
   проверить существующую ревизию для **точного** book/chapter назначения.
   Изменение destination или книги сбрасывает проверенную ревизию.
5. На отдельном экране подтверждения проверить сводку, поставить галку и
   выполнить `POST /v1/imports/stages/{stage_id}/confirm`.
   SQLite `BEGIN IMMEDIATE` обеспечивает атомарность; при конкурирующей
   правке версия даёт **409**, не silent overwrite. Успех даёт квитанцию,
   но не запускает Gemini и не означает публикацию.
6. Отмена временной сессии использует
   `DELETE /v1/imports/stages/{stage_id}`; никогда не удаляет уже
   подтверждённую книгу. Stage можно восстановить по ID в пределах TTL,
   но неотправленные браузерные сопоставления после reload нужно проверить
   и ввести заново.

Книга появляется в `/app/books` с фактическими SQLite-счётчиками глав,
актуально принятых ревизий и возможностью открыть оглавление/редактор.

## Источники и испытания

| Компонент | Реализация | Проверка |
|---|---|---|
| Stage/confirm/abort | `src/chaptera_server/import_sessions.py`, `http/routers/imports.py` | `tests/test_import_sessions.py`, HTTP 401/403/409/410/413 |
| Решения и безопасные destination | `web_ui/static/js/import-model.mjs` | `import-model.test.mjs` через Node 22 |
| Общий вход и импорт | `web_ui/templates/shell.html`, `static/js/readera.mjs` | `tests/test_jinja_ui.py`, реальный Chromium `tests/browser/smoke.mjs` |
| Библиотека/оглавление | `web_ui/static/js/library-view.mjs`, `library.py` | `tests/test_library_catalog.py`, Chromium |
| Визуал четырёх страниц | [RE-05 визуальная матрица](READ_READERA_RE05_VISUAL_ACCEPTANCE.md) | Скриншоты GitHub Actions (без секретов) |

**Security:** серверный CAS окончателен; клиентская валидация —
вспомогательная. Все пользовательские тексты вставляются безопасным
`textContent`, не через исполняемый HTML. Изменение source делает
прежний Accepted потенциально устаревшим, не переписывая историю.

## Отдельные задачи

- Полноценные записи каталога Rulate metadata/внешние URL (текущий JSON
  остаётся только preview).
- Редакторский Published/workflow, внешний релиз и экспорт не реализованы
  самим мастером импорта.
- Постоянное развёртывание за HTTPS/VPN требует
  [chaptera-site #46](https://github.com/Lalalalendia/chaptera-site/issues/46)
  и **не** выполняется CI.
