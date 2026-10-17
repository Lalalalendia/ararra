import { useState, type ChangeEvent, type DragEvent, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { Panel } from '../../shared/ui/Panel';
import { routePaths } from '../../routes/paths';
import { teamApi, TeamApiError, type TeamIdentity, type TeamMember } from '../../shared/api/team';
import {
  importApi, ImportHttpError, MAX_IMPORT_BYTES, SUPPORTED_IMPORT_EXTENSIONS,
  type BookSummary, type ImportReceipt, type StageSnapshot,
} from '../../shared/api/imports';
import {
  newDraft, checkedReplacement, validateImport, type BookTarget, type DraftChapter,
} from './model';
import { ImportMapping } from './ImportMapping';
import { ImportReview } from './ImportReview';
import './import.css';

const initialTarget: BookTarget = {
  mode: 'create', id: '', title: '', owner: '', prefixPolicy: 'reject',
};

function explain(error: unknown): string {
  if (error instanceof ImportHttpError || error instanceof TeamApiError) {
    if (error.status === 401) return 'Ключ недействителен или доступ участника отключён.';
    if (error.status === 403) return 'Эта операция не разрешена вашей роли.';
    if (error.status === 404) return 'Книга, глава или временная сессия не найдена.';
    if (error.status === 410) return 'Срок хранения сессии истёк. Загрузите файл заново.';
    if (error.status === 413) return 'Превышен лимит файла или временных сессий.';
    if (error.status === 409) return 'Конфликт: ' + error.message +
      ' Проверьте назначение главы и снова загрузите ревизию для замены.';
    if (error.status === 422) return 'Исходник или выбор глав не принят: ' + error.message;
    return 'Ошибка API (' + error.status + '): ' + error.message;
  }
  return 'Сервер недоступен или ответ потерян. Можно повторить запрос с теми же данными.';
}

export function ImportPage() {
  // Private bearer key: React memory for this page only, never persisted.
  const [key, setKey] = useState('');
  const [identity, setIdentity] = useState<TeamIdentity | null>(null);
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [moderators, setModerators] = useState<TeamMember[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [uploadKey, setUploadKey] = useState('');
  const [resumeId, setResumeId] = useState('');
  const [stage, setStage] = useState<StageSnapshot | null>(null);
  const [rows, setRows] = useState<DraftChapter[]>([]);
  const [target, setTarget] = useState<BookTarget>(initialTarget);
  const [receipt, setReceipt] = useState<ImportReceipt | null>(null);
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [working, setWorking] = useState(false);
  const [verifying, setVerifying] = useState<number | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [message, setMessage] = useState('');

  function resetStage() {
    setFile(null); setUploadKey(''); setStage(null); setRows([]);
    setReceipt(null); setStep(1); setTarget(initialTarget);
    setErrors([]); setMessage(''); setResumeId('');
  }
  function signOut() {
    resetStage(); setKey(''); setIdentity(null); setBooks([]); setModerators([]);
  }
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setWorking(true); setMessage('');
    try {
      const who = await teamApi.me(key);
      const library = await importApi.listBooks(key);
      const members = who.role === 'admin' ? await teamApi.members(key) : [];
      setIdentity(who); setBooks(library); setModerators(members);
    } catch (error) {
      setMessage(explain(error));
    } finally { setWorking(false); }
  }

  function chooseFile(candidate: File | null) {
    setFile(candidate); setUploadKey(candidate ? crypto.randomUUID() : '');
    setMessage('');
    if (!candidate) return;
    const name = candidate.name.toLowerCase();
    if (!SUPPORTED_IMPORT_EXTENSIONS.some(ext => name.endsWith(ext))) {
      setMessage('Поддерживаются TXT, HTML, XHTML, EPUB и DOCX. JSON Rulate — только метаданные, без текста глав.');
    } else if (!candidate.size || candidate.size > MAX_IMPORT_BYTES) {
      setMessage('Файл пуст или превышает лимит 12 МиБ.');
    }
  }

  function fileChanged(event: ChangeEvent<HTMLInputElement>) {
    chooseFile(event.target.files?.[0] ?? null);
  }
  function dropFile(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    if (working) return;
    chooseFile(event.dataTransfer.files[0] ?? null);
  }

  function showStage(snapshot: StageSnapshot) {
    setStage(snapshot); setResumeId(snapshot.stage_id);
    setRows(newDraft(snapshot.preview)); setReceipt(snapshot.commit_result ?? null);
    setTarget({
      ...initialTarget, title: snapshot.preview.title,
      id: 'novel-' + crypto.randomUUID().slice(0, 8),
      owner: identity?.role === 'moderator' ? identity.member_id : '',
    });
    setStep(snapshot.status === 'committed' ? 3 : 2);
    setErrors([]);
    setMessage(snapshot.status === 'committed'
      ? 'Эта загрузка уже была подтверждена — показана сохранённая квитанция.'
      : 'Источник временно сохранён. Главы ещё не импортированы.');
  }

  async function upload() {
    if (!file || !identity || working) return;
    const supported = SUPPORTED_IMPORT_EXTENSIONS.some(ext => file.name.toLowerCase().endsWith(ext));
    if (!supported || !file.size || file.size > MAX_IMPORT_BYTES) {
      setMessage('Выберите поддерживаемый файл размером до 12 МиБ.');
      return;
    }
    setWorking(true); setMessage('');
    try {
      const opened = await importApi.stage(key, file, uploadKey);
      const detailed = await importApi.stageDetails(key, opened.stage_id);
      showStage(detailed);
    } catch (error) { setMessage(explain(error)); }
    finally { setWorking(false); }
  }

  async function abortStage() {
    if (!stage || receipt) { resetStage(); return; }
    setWorking(true); setMessage('');
    try {
      await importApi.cancelStage(key, stage.stage_id);
      resetStage();
      setMessage('Временная загрузка отменена, место освобождено.');
    } catch (error) { setMessage(explain(error)); }
    finally { setWorking(false); }
  }

  async function resume() {
    if (!identity || working || !resumeId.trim()) return;
    setWorking(true); setMessage('');
    try {
      const snapshot = await importApi.stageDetails(key, resumeId.trim());
      showStage(snapshot);
      if (snapshot.status === 'staged') {
        setMessage('Сессия восстановлена. Сопоставления необходимо проверить заново.');
      }
    } catch (error) { setMessage(explain(error)); }
    finally { setWorking(false); }
  }

  function changeTarget(next: BookTarget) {
    if (next.mode !== target.mode || next.id !== target.id) {
      // A checked replacement only belongs to the exact selected book.
      setRows(list => list.map(c => c.action === 'replace' ? {
        ...c, expected_revision: null, verified_book_id: null, verified_chapter_id: null,
      } : c));
    }
    setTarget(next); setErrors([]); setMessage('');
  }
  function changeRows(update: (value: DraftChapter[]) => DraftChapter[]) {
    setRows(old => update(old)); setErrors([]); setMessage('');
  }

  async function verifyReplacement(sequence: number) {
    const row = rows.find(c => c.sequence === sequence);
    if (!row || target.mode !== 'existing' || !target.id.trim() || !row.chapter_id.trim()) {
      setMessage('Для замены выберите существующую книгу и ID главы.');
      return;
    }
    setVerifying(sequence); setMessage(''); setErrors([]);
    try {
      const data = await importApi.chapter(key, target.id.trim(), row.chapter_id.trim());
      setRows(previous => previous.map(c => c.sequence === sequence
        ? checkedReplacement(c, target.id.trim(), data) : c));
    } catch (error) { setMessage(explain(error)); }
    finally { setVerifying(null); }
  }

  function review() {
    if (!stage) return;
    const issues = validateImport(rows, target, stage.unassigned_prefix_saved,
      Date.now() / 1000, stage.expires_at);
    setErrors(issues);
    if (!issues.length) { setStep(3); setMessage(''); }
  }

  async function confirm() {
    if (!stage || !identity || working || receipt) return;
    const issues = validateImport(rows, target, stage.unassigned_prefix_saved,
      Date.now() / 1000, stage.expires_at);
    if (issues.length) { setErrors(issues); return; }
    setWorking(true); setMessage(''); setErrors([]);
    try {
      const { buildConfirmation } = await import('./model');
      const saved = await importApi.confirm(key, stage.stage_id,
        buildConfirmation(rows, target, stage.unassigned_prefix_saved));
      setReceipt(saved);
      setMessage('Импорт подтверждён сервером.');
    } catch (error) {
      setMessage(explain(error));
      setErrors(['Запись не подтверждена. Проверьте сообщение сервера; при конфликте вернитесь на шаг 2.']);
    } finally { setWorking(false); }
  }

  return <Panel>
    <div className="import-title">
      <div><p className="eyebrow">РидЭра / Импорт книги</p>
        <h1>Загрузка и сопоставление глав</h1>
        <p>Проверка исходников перед записью — без автоматического перевода или публикации.</p>
      </div>
      <Link to={routePaths.books} className="import-text-link">Библиотека</Link>
    </div>
    <ol className="import-steps" aria-label="Этапы импорта">
      {['Загрузка', 'Оглавление', 'Подтверждение'].map((label, i) =>
        <li key={label} aria-current={step === i + 1 ? 'step' : undefined}
          className={step === i + 1 ? 'is-current' : step > i + 1 ? 'is-done' : ''}>
          <span>{i + 1}</span>{label}
        </li>)}
    </ol>

    {!identity && <form className="import-panel import-login" onSubmit={connect} autoComplete="off">
      <h2>Вход в рабочее пространство</h2>
      <p>Используйте свой командный ключ. Он остаётся только в памяти этой страницы.</p>
      <label>Ключ участника
        <input type="password" value={key} disabled={working} required
          autoComplete="off" spellCheck={false}
          onChange={event => setKey(event.target.value)}
          placeholder="Ключ администратора или модератора"/>
      </label>
      <button type="submit" disabled={working || !key}>Подключиться</button>
    </form>}

    {identity && <div className="import-identity">
      <span>Участник: <strong>{identity.member_id}</strong> ·
        {identity.role === 'admin' ? ' Администратор' : ' Модератор'}</span>
      <button type="button" className="import-secondary" onClick={signOut}
        disabled={working || verifying !== null}>Выйти</button>
    </div>}
    {message && <div role="status" className="import-message">{message}</div>}

    {identity && step === 1 && <div className="import-panel import-upload">
      <h2>Выберите файл</h2>
      <p>TXT, HTML/XHTML, EPUB, DOCX · до 12 МиБ. JSON-каталоги Rulate
        не содержат исходного текста и не могут создавать главы.</p>
      <label className="import-drop" onDragOver={event => event.preventDefault()} onDrop={dropFile}>
        <span>Перетащите файл сюда или выберите с компьютера</span>
        <input type="file" disabled={working}
          accept=".txt,.html,.htm,.xhtml,.epub,.docx" onChange={fileChanged} />
      </label>
      {file && <p>Файл: <strong>{file.name}</strong> · {(file.size / 1024).toFixed(1)} КиБ</p>}
      <button type="button" disabled={!file || working || !uploadKey}
        onClick={upload}>{working ? 'Обработка файла…' : 'Загрузить и показать оглавление'}</button>
      <div className="import-resume">
        <label>Восстановить временный импорт по ID
          <input value={resumeId} disabled={working} onChange={e => setResumeId(e.target.value)}
            placeholder="ID вашей сессии, действителен до 30 минут"/>
        </label>
        <button type="button" className="import-secondary" disabled={working || !resumeId.trim()}
          onClick={resume}>Восстановить</button>
      </div>
    </div>}

    {identity && stage && step !== 1 && <div className="import-stage-meta">
      <span>Файл: <strong>{stage.preview.filename}</strong> · {stage.preview.chapter_count} глав</span>
      <span>ID сессии: <code>{stage.stage_id}</code></span>
      <span>Действует до {new Date(stage.expires_at * 1000).toLocaleString('ru-RU')}</span>
    </div>}
    {identity && stage && step === 2 && <ImportMapping
      stage={stage} rows={rows} target={target} identity={identity}
      books={books} moderators={moderators} busy={working || verifying !== null}
      verifying={verifying} errors={errors}
      onTarget={changeTarget} onRows={changeRows} onVerify={verifyReplacement}
      onNext={review} onBack={() => { void abortStage(); }}/>}
    {identity && stage && step === 3 && <ImportReview
      stage={stage} rows={rows} target={target} receipt={receipt}
      errors={errors} busy={working} onBack={() => { setErrors([]); setStep(2); }}
      onConfirm={confirm} onNew={resetStage}/>}
  </Panel>;
}
