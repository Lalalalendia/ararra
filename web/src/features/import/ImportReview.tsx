import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { ImportReceipt, StageSnapshot } from '../../shared/api/imports';
import { routePaths } from '../../routes/paths';
import { type BookTarget, type DraftChapter, countDecisions } from './model';

type Props = {
  stage: StageSnapshot;
  target: BookTarget;
  rows: DraftChapter[];
  receipt: ImportReceipt | null;
  errors: string[];
  busy: boolean;
  onBack: () => void;
  onConfirm: () => void;
  onNew: () => void;
};

export function ImportReview({ stage, target, rows, receipt, errors, busy,
                               onBack, onConfirm, onNew }: Props) {
  const [checked, setChecked] = useState(false);
  const counts = countDecisions(rows);

  if (receipt) return (
    <section className="import-panel import-success" aria-live="polite">
      <p className="eyebrow">Импорт завершён</p>
      <h2>Главы сохранены в библиотеке</h2>
      <p>Вся выбранная партия записана одной транзакцией. Исходные тексты
        сохранены, переводы и публикация не запускались.</p>
      <dl className="import-summary">
        <div><dt>Книга</dt><dd>{receipt.book_id}</dd></div>
        <div><dt>Записано</dt><dd>{receipt.imported_count}</dd></div>
        <div><dt>Пропущено</dt><dd>{receipt.skipped_count}</dd></div>
        <div><dt>Предисловие</dt>
          <dd>{receipt.unassigned_prefix_preserved ? 'Сохранено отдельно' : 'Не сохранялось'}</dd></div>
      </dl>
      <p className="import-help">ID операции: {receipt.stage_id}. Повторное
        подтверждение того же запроса безопасно возвращает квитанцию.</p>
      <div className="import-actions">
        <Link className="import-link-button" to={routePaths.books}>Открыть библиотеку</Link>
        <button type="button" onClick={onNew}>Импортировать ещё книгу</button>
      </div>
    </section>
  );

  return (
    <section className="import-panel">
      <p className="eyebrow">Шаг 3 · Подтверждение</p>
      <h2>Проверьте перед записью</h2>
      <p>Никакая глава не записана, пока вы не нажмёте «Подтвердить импорт».
        Если хотя бы одна ревизия изменилась, вся операция будет отклонена.</p>
      <div className="import-review-columns">
        <div>
          <dl className="import-summary">
            <div><dt>Книга</dt><dd>{target.id}</dd></div>
            <div><dt>Режим</dt><dd>{target.mode === 'create' ? 'Новая книга' : 'Существующая книга'}</dd></div>
            {target.mode === 'create' && <>
              <div><dt>Название</dt><dd>{target.title}</dd></div>
              <div><dt>Ответственный</dt><dd>{target.owner || 'Не выбран'}</dd></div>
            </>}
            <div><dt>Исходный файл</dt><dd>{stage.preview.filename}</dd></div>
            <div><dt>SHA-256</dt><dd className="import-hash">{stage.file_sha256}</dd></div>
            <div><dt>Предисловие</dt><dd>{stage.unassigned_prefix_saved
              ? target.prefixPolicy === 'preserve' ? 'Сохранить отдельно'
                : target.prefixPolicy === 'discard' ? 'Исключить' : 'Решение не принято'
              : 'Нет'}</dd></div>
          </dl>
          <h3>Решения по главам</h3>
          <div className="import-table-scroll import-review-list">
            <table className="import-table">
              <thead><tr><th>№ исходника</th><th>Действие</th><th>Назначение</th><th>Ревизия</th></tr></thead>
              <tbody>{rows.map(row => <tr key={row.sequence}>
                <td>{row.sequence} · {row.title}</td>
                <td>{row.action === 'create' ? 'Создать' : row.action === 'replace' ? 'Заменить' : 'Пропустить'}</td>
                <td>{row.action === 'skip' ? '—' :
                  row.chapter_index + ' / ' + row.chapter_id}</td>
                <td>{row.action === 'replace' ? String(row.expected_revision ?? 'Не проверена') : '—'}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </div>
        <aside className="import-totals" aria-label="Итоги импорта">
          <h3>Будет выполнено</h3>
          <dl>
            <div><dt>Создать</dt><dd>{counts.create}</dd></div>
            <div><dt>Заменить</dt><dd>{counts.replace}</dd></div>
            <div><dt>Пропустить</dt><dd>{counts.skip}</dd></div>
          </dl>
          <div className="import-atomic">
            <strong>Атомарная запись</strong>
            <p>Любой конфликт, устаревшая версия или неправильный путь
              отменяет всю партию без частичного импорта.</p>
          </div>
          <label className="import-choice import-confirm-check">
            <input type="checkbox" checked={checked}
              onChange={event => setChecked(event.target.checked)}
              disabled={busy}/>
            Я проверил назначения и понимаю, что действие «Заменить» обновит исходник.
          </label>
          <button type="button" disabled={!checked || busy || errors.length > 0}
            onClick={onConfirm}>
            {busy ? 'Проверка ревизий…' : 'Подтвердить импорт'}
          </button>
          <p className="import-help">Вызов Gemini не выполняется. Принятые
            переводы сохраняются в истории, но могут стать неактуальными при замене исходника.</p>
        </aside>
      </div>
      {!!errors.length && <div className="import-alert" role="alert">
        <strong>Подтверждение недоступно:</strong>
        <ul>{errors.slice(0, 10).map((text, i) => <li key={i}>{text}</li>)}</ul>
      </div>}
      <div className="import-actions">
        <button type="button" className="import-secondary" onClick={onBack}
          disabled={busy}>Вернуться к оглавлению</button>
      </div>
    </section>
  );
}
