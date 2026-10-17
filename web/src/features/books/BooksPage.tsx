import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { Panel } from '../../shared/ui/Panel';
import { importApi, ImportHttpError, type BookSummary } from '../../shared/api/imports';
import { teamApi, type TeamIdentity } from '../../shared/api/team';
import { routePaths } from '../../routes/paths';
import './books.css';

export function BooksPage() {
  const [key, setKey] = useState('');
  const [identity, setIdentity] = useState<TeamIdentity | null>(null);
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [filter, setFilter] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function load(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setBusy(true); setError('');
    try {
      const who = await teamApi.me(key);
      const list = await importApi.listBooks(key);
      setIdentity(who); setBooks(list);
    } catch (reason) {
      setError(reason instanceof ImportHttpError && reason.status === 401
        ? 'Неверный ключ команды или доступ отключён.'
        : 'Не удалось загрузить библиотеку. Проверьте подключение к API.');
    } finally {
      setBusy(false);
    }
  }
  const visible = books.filter(book =>
    (book.title + ' ' + book.book_id + ' ' + (book.owner_member_id ?? ''))
      .toLowerCase().includes(filter.trim().toLowerCase()),
  );
  return <Panel>
    <div className="books-heading">
      <div><p className="eyebrow">РидЭра / Библиотека</p>
        <h1>Книги команды</h1>
        <p>Общее пространство администраторов и модераторов. Владелец книги
          — ответственный модератор, а не отдельная роль доступа.</p>
      </div>
      <Link className="books-import" to={routePaths.import}>+ Импортировать книгу</Link>
    </div>
    {!identity ? <form className="books-login" onSubmit={load} autoComplete="off">
      <label>Ключ участника
        <input type="password" value={key} onChange={e => setKey(e.target.value)}
          autoComplete="off" spellCheck={false} disabled={busy}
          placeholder="Введите свой командный ключ" required />
      </label>
      <button type="submit" disabled={!key || busy}>{busy ? 'Подключение…' : 'Показать библиотеку'}</button>
      <p>Ключ используется только для запроса к API и остаётся в памяти страницы.</p>
    </form> : <>
      <div className="books-toolbar">
        <div><strong>{identity.member_id}</strong> ·
          {identity.role === 'admin' ? ' Администратор' : ' Модератор'}</div>
        <div className="books-tools">
          <button type="button" disabled={busy} onClick={() => { void load(); }}>
            {busy ? 'Загрузка…' : 'Обновить'}
          </button>
          <button type="button" onClick={() => {
            setIdentity(null); setKey(''); setBooks([]); setError('');
          }}>Выйти</button>
        </div>
      </div>
      <label className="books-filter">Поиск по названию, ID или ответственному
        <input type="search" value={filter} onChange={e => setFilter(e.target.value)}
          placeholder="Найти книгу" />
      </label>
      <div className="books-table-scroll">
        <table className="books-table">
          <thead><tr><th>Название</th><th>ID книги</th>
            <th>Ответственный</th><th>Ревизия назначения</th></tr></thead>
          <tbody>{visible.map(book =>
            <tr key={book.book_id}>
              <td><strong>{book.title}</strong></td>
              <td><code>{book.book_id}</code></td>
              <td>{book.owner_member_id ?? 'Не назначен (старая книга)'}</td>
              <td>{book.owner_revision}</td>
            </tr>)}</tbody>
        </table>
      </div>
      {!visible.length && <div className="books-empty">
        {books.length ? 'По вашему запросу книг не найдено.' :
          'Библиотека пока пуста. Можно загрузить первую книгу через мастер импорта.'}
      </div>}
    </>}
    {error && <p role="alert" className="books-error">{error}</p>}
  </Panel>;
}
