import type { BookSummary, StageSnapshot } from '../../shared/api/imports';
import type { TeamIdentity, TeamMember } from '../../shared/api/team';
import {
  type BookTarget, type DraftChapter, selectAction, editDestination,
} from './model';

type Props = {
  stage: StageSnapshot;
  rows: DraftChapter[];
  target: BookTarget;
  identity: TeamIdentity;
  books: BookSummary[];
  moderators: TeamMember[];
  busy: boolean;
  verifying: number | null;
  errors: string[];
  onTarget: (next: BookTarget) => void;
  onRows: (change: (rows: DraftChapter[]) => DraftChapter[]) => void;
  onVerify: (sequence: number) => void;
  onNext: () => void;
  onBack: () => void;
};

export function ImportMapping({
  stage, rows, target, identity, books, moderators, busy, verifying,
  errors, onTarget, onRows, onVerify, onNext, onBack,
}: Props) {
  function updateRow(sequence: number, patch: (c: DraftChapter) => DraftChapter) {
    onRows(current => current.map(c => c.sequence === sequence ? patch(c) : c));
  }
  const hasPrefix = stage.unassigned_prefix_saved;
  const activeModerators = moderators.filter(m => m.role === 'moderator' && m.status === 'active');
  return (
    <div className="import-screen">
      <div className="import-panel">
        <p className="eyebrow">Шаг 2 · Сопоставление</p>
        <h2>Назначение книги и глав</h2>
        <p>Предпросмотр не записывает главы в библиотеку. Проверьте номера,
          расположение файлов и действия до подтверждения.</p>

        <fieldset className="import-fieldset" disabled={busy}>
          <legend>Куда импортировать?</legend>
          <label className="import-choice">
            <input type="radio" name="book-mode" checked={target.mode === 'create'}
              onChange={() => onTarget({ ...target, mode: 'create', id: '', owner: identity.role === 'moderator' ? identity.member_id : '' })}/>
            Создать новую книгу
          </label>
          <label className="import-choice">
            <input type="radio" name="book-mode" checked={target.mode === 'existing'}
              onChange={() => onTarget({ ...target, mode: 'existing', id: books[0]?.book_id ?? '', owner: '' })}/>
            Дополнить существующую
          </label>

          {target.mode === 'create' ? (
            <div className="import-grid-fields">
              <label>Название книги
                <input value={target.title} maxLength={500}
                  onChange={e => onTarget({ ...target, title: e.target.value })}
                  placeholder="Название исходника" />
              </label>
              <label>ID книги
                <input value={target.id} maxLength={128}
                  onChange={e => onTarget({ ...target, id: e.target.value })}
                  placeholder="Например: novel-2026" />
              </label>
              {identity.role === 'admin' ? (
                <label>Ответственный модератор
                  <select value={target.owner} onChange={e => onTarget({ ...target, owner: e.target.value })}>
                    <option value="">Выберите модератора</option>
                    {activeModerators.map(m => <option key={m.member_id} value={m.member_id}>{m.member_id}</option>)}
                  </select>
                  {!activeModerators.length &&
                    <span className="import-help">Сначала создайте модератора через раздел «Команда».</span>}
                </label>
              ) : (
                <p className="import-owner">Ответственный модератор: <strong>{identity.member_id}</strong></p>
              )}
            </div>
          ) : (
            <label className="import-book-select">Существующая книга
              <select value={target.id} onChange={e => onTarget({ ...target, id: e.target.value })}>
                <option value="">Выберите книгу</option>
                {books.map(b => <option value={b.book_id} key={b.book_id}>{b.title} · {b.book_id}</option>)}
              </select>
              {!books.length && <span className="import-help">В библиотеке пока нет книг.</span>}
            </label>
          )}
        </fieldset>

        {!!stage.preview.warnings.length && <div className="import-warnings" role="group" aria-label="Замечания к исходнику">
          <strong>Требуют проверки</strong>
          <ul>{stage.preview.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>
        </div>}

        {hasPrefix && <fieldset className="import-fieldset import-prefix" disabled={busy}>
          <legend>Текст до первой главы</legend>
          <p>Предисловие не привязывается автоматически к главе.</p>
          <textarea readOnly aria-label="Исходное предисловие (HTML)"
            value={stage.unassigned_prefix_html || stage.preview.unassigned_prefix_sample}
            rows={4} />
          <label className="import-choice">
            <input type="radio" name="prefix" checked={target.prefixPolicy === 'preserve'}
              onChange={() => onTarget({ ...target, prefixPolicy: 'preserve' })}/>
            Сохранить отдельно
          </label>
          <label className="import-choice">
            <input type="radio" name="prefix" checked={target.prefixPolicy === 'discard'}
              onChange={() => onTarget({ ...target, prefixPolicy: 'discard' })}/>
            Исключить осознанно
          </label>
        </fieldset>}
      </div>

      <div className="import-panel">
        <div className="import-table-heading">
          <div><h2>Оглавление · {rows.length} глав</h2>
            <p className="import-help">Для дубликатов и глав без номера назначения пусты: заполните вручную или пропустите.</p>
          </div>
        </div>
        <div className="import-table-scroll">
          <table className="import-table">
            <thead><tr><th>Исходник</th><th>Название и пример</th><th>Действие</th>
              <th>ID главы</th><th>№ назначения</th><th>Путь</th><th>Ревизия</th></tr></thead>
            <tbody>{rows.map(row => (
              <tr key={row.sequence}>
                <td className="import-number">
                  <strong>#{row.sequence}</strong><span>{row.detected_number === null
                    ? 'Без номера' : 'Глава ' + row.detected_number}</span>
                </td>
                <td className="import-chapter-title">
                  <strong title={row.title}>{row.title}</strong>
                  <small>{row.characters.toLocaleString('ru-RU')} символов</small>
                  <p title={row.sample}>{row.sample || 'Нет текстового примера'}</p>
                </td>
                <td><select aria-label={'Действие для главы ' + row.sequence} disabled={busy}
                    value={row.action}
                    onChange={e => updateRow(row.sequence, c => selectAction(c, e.target.value as DraftChapter['action']))}>
                    <option value="create">Создать</option>
                    <option value="replace" disabled={target.mode === 'create'}>Заменить</option>
                    <option value="skip">Пропустить</option>
                  </select></td>
                <td><input aria-label={'ID главы ' + row.sequence} value={row.chapter_id}
                    disabled={row.action === 'skip' || busy} maxLength={255}
                    onChange={e => updateRow(row.sequence, c => editDestination(c, 'chapter_id', e.target.value))}/></td>
                <td><input aria-label={'Номер назначения ' + row.sequence} type="text" inputMode="numeric"
                    value={row.chapter_index} disabled={row.action === 'skip' || busy}
                    onChange={e => updateRow(row.sequence, c => editDestination(c, 'chapter_index', e.target.value))}/></td>
                <td><input aria-label={'Путь главы ' + row.sequence} value={row.chapter_path}
                    disabled={row.action === 'skip' || busy} maxLength={1024}
                    onChange={e => updateRow(row.sequence, c => editDestination(c, 'chapter_path', e.target.value))}/></td>
                <td>{row.action === 'replace' ? (
                  <div className="import-revision">
                    <span>{row.expected_revision !== null && row.verified_book_id === target.id
                      ? 'rev ' + row.expected_revision : 'Не проверена'}</span>
                    <button type="button" disabled={busy || !target.id || verifying !== null} className="import-secondary"
                      onClick={() => onVerify(row.sequence)}>
                      {verifying === row.sequence ? 'Загрузка…' : 'Проверить'}
                    </button>
                  </div>
                ) : <span className="import-muted">{row.action === 'skip' ? '—' : 'Новая'}</span>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>
      {!!errors.length && <div role="alert" className="import-alert">
        <strong>Перед продолжением исправьте:</strong>
        <ul>{errors.slice(0, 14).map((error, i) => <li key={i}>{error}</li>)}</ul>
        {errors.length > 14 && <p>И ещё замечаний: {errors.length - 14}</p>}
      </div>}
      <div className="import-actions">
        <button type="button" className="import-secondary" onClick={onBack} disabled={busy}>Назад</button>
        <button type="button" onClick={onNext} disabled={busy}>Проверить и продолжить</button>
      </div>
    </div>
  );
}
