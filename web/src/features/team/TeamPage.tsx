import { useState, type FormEvent } from 'react';
import { Panel } from '../../shared/ui/Panel';
import { teamApi, TeamApiError, type TeamMember, type TokenIssued } from '../../shared/api/team';
import './team.css';

type Pending = { memberId: string; action: 'rotate' | 'disable' };

export function TeamPage() {
  // This bearer credential stays only in React state for the current tab.
  const [adminKey, setAdminKey] = useState('');
  const [connected, setConnected] = useState(false);
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [memberId, setMemberId] = useState('');
  const [issued, setIssued] = useState<TokenIssued | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  function resetPrivateState() {
    setConnected(false);
    setMembers([]);
    setIssued(null);
    setPending(null);
    setError('');
    setNotice('');
  }

  function describeError(reason: unknown): string {
    if (reason instanceof TeamApiError) {
      if (reason.status === 401) return 'Ключ недействителен или был отозван.';
      if (reason.status === 403) return 'Доступно только администратору «РидЭры».';
      if (reason.status === 409) return 'Конфликт: ' + reason.message;
      return 'Ошибка API (' + reason.status + '): ' + reason.message;
    }
    return 'Нет соединения с сервером. Проверьте доступность API.';
  }

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    resetPrivateState();
    setBusy(true);
    try {
      const identity = await teamApi.me(adminKey);
      if (identity.role !== 'admin') {
        setError('Это ключ модератора. Управление доступами разрешено только администратору.');
        return;
      }
      const roster = await teamApi.members(adminKey);
      setMembers(roster);
      setConnected(true);
      setNotice('Административный доступ подтверждён.');
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function refresh(key = adminKey) {
    setMembers(await teamApi.members(key));
  }

  async function invite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connected || busy) return;
    setIssued(null);
    setError('');
    setNotice('');
    setBusy(true);
    try {
      const token = await teamApi.invite(adminKey, memberId.trim());
      setIssued(token);
      setMemberId('');
      await refresh();
      setNotice('Модератор создан. Передайте новый ключ по защищённому каналу.');
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function confirmAction() {
    if (!pending || !connected || busy) return;
    const { action, memberId: target } = pending;
    setBusy(true);
    setError('');
    setNotice('');
    setIssued(null);
    setPending(null);
    try {
      if (action === 'rotate') {
        const token = await teamApi.rotate(adminKey, target);
        setIssued(token);
        setNotice('Старый ключ отключён. Передайте новый модератору.');
      } else {
        await teamApi.disable(adminKey, target);
        setNotice('Доступ модератора отключён. Ожидающие задания отменены.');
      }
      await refresh();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function copyToken() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.access_token);
      setNotice('Ключ скопирован. Не отправляйте его в публичный чат или issue.');
    } catch {
      setNotice('Копирование недоступно. Выделите ключ и скопируйте вручную.');
    }
  }

  function signOut() {
    setAdminKey('');
    resetPrivateState();
    setMemberId('');
  }

  return (
    <Panel>
      <p className="eyebrow">РидЭра / Администрирование</p>
      <h1>Участники команды</h1>
      <p className="team-lead">
        В проекте две глобальные роли: администратор и модераторы.
        Модераторы работают с общей библиотекой, импортом и переводами.
        Эта страница управляет только доступом команды.
      </p>

      <form className="team-auth" onSubmit={connect} autoComplete="off">
        <label htmlFor="team-admin-key">Ключ администратора</label>
        <div className="team-auth-row">
          <input
            id="team-admin-key"
            type="password"
            name="reidera-admin-key"
            autoComplete="off"
            placeholder="Введите ключ команды для текущего сеанса"
            value={adminKey}
            onChange={event => { setAdminKey(event.target.value); resetPrivateState(); }}
            spellCheck={false}
            required
          />
          <button type="submit" disabled={!adminKey || busy}>
            {busy && !connected ? 'Проверка…' : 'Войти'}
          </button>
          {connected && <button type="button" className="team-secondary" onClick={signOut}>Выйти</button>}
        </div>
        <p className="team-small">Ключ хранится только в памяти этой вкладки и исчезает при обновлении страницы. На сервере должен быть явно назначен администратор.</p>
      </form>

      {error && <p className="team-alert team-error" role="alert">{error}</p>}
      {notice && <p className="team-alert team-notice" role="status">{notice}</p>}

      {connected && (
        <div className="team-panel">
          <div className="team-header">
            <div>
              <h2>Модераторы</h2>
              <p className="team-small">Выданные ключи нельзя просмотреть повторно — только перевыпустить.</p>
            </div>
            <button className="team-secondary" type="button" disabled={busy}
                    onClick={() => {
                      setBusy(true);
                      refresh().catch(e => setError(describeError(e))).finally(() => setBusy(false));
                    }}>
              Обновить
            </button>
          </div>

          <form className="team-invite" onSubmit={invite}>
            <label htmlFor="team-new-member">Добавить модератора</label>
            <div className="team-auth-row">
              <input id="team-new-member" type="text" value={memberId}
                     onChange={event => setMemberId(event.target.value)}
                     placeholder="Идентификатор, например: editor2"
                     minLength={1} maxLength={64} autoComplete="off" required
                     pattern="[A-Za-z][A-Za-z0-9._-]{0,63}" />
              <button type="submit" disabled={busy || !memberId.trim()}>Создать ключ</button>
            </div>
          </form>

          {issued && (
            <div className="team-secret" role="region" aria-label="Новый ключ модератора">
              <h3>Новый ключ: {issued.member_id}</h3>
              <p>Показывается только сейчас. Сохраните и передайте модератору по защищённому каналу.</p>
              <code className="team-secret-value">{issued.access_token}</code>
              <div className="team-actions">
                <button type="button" onClick={copyToken}>Скопировать</button>
                <button type="button" className="team-secondary"
                        onClick={() => setIssued(null)}>Скрыть ключ</button>
              </div>
            </div>
          )}

          <div className="team-table-wrap">
            <table className="team-table">
              <thead><tr><th scope="col">Участник</th><th scope="col">Роль</th>
                <th scope="col">Состояние</th><th scope="col">Ключ</th>
                <th scope="col">Действия</th></tr></thead>
              <tbody>
                {members.map(member => (
                  <tr key={member.member_id}>
                    <td><strong>{member.member_id}</strong></td>
                    <td>{member.role === 'admin' ? 'Администратор' : 'Модератор'}</td>
                    <td><span className={'team-state ' + (member.status === 'active' ? 'is-active' : 'is-disabled')}>
                      {member.status === 'active' ? 'Активен' : 'Отключён'}
                    </span></td>
                    <td>{member.credential_source === 'managed' ? 'Выдан в РидЭре' : 'Настроен на сервере'}</td>
                    <td>
                      {member.role === 'moderator' ? (
                        <div className="team-actions">
                          <button type="button" className="team-secondary" disabled={busy}
                            onClick={() => { setIssued(null); setPending({ memberId: member.member_id, action: 'rotate' }); }}>
                            Перевыпустить
                          </button>
                          {member.status === 'active' && <button type="button" className="team-danger"
                            disabled={busy}
                            onClick={() => { setIssued(null); setPending({ memberId: member.member_id, action: 'disable' }); }}>
                            Отключить
                          </button>}
                        </div>
                      ) : <span className="team-small">Только через настройки сервера</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {pending && (
            <div className="team-confirm" role="group" aria-label="Подтверждение изменения доступа">
              <p>
                {pending.action === 'disable'
                  ? 'Отключить модератора «' + pending.memberId + '»? Его ключ перестанет работать, а ожидающие задания будут отменены.'
                  : 'Перевыпустить ключ «' + pending.memberId + '»? Прежний ключ перестанет работать немедленно.'}
              </p>
              <div className="team-actions">
                <button type="button" className={pending.action === 'disable' ? 'team-danger' : ''}
                        disabled={busy} onClick={confirmAction}>Подтвердить</button>
                <button type="button" className="team-secondary"
                        onClick={() => setPending(null)} disabled={busy}>Отмена</button>
              </div>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
