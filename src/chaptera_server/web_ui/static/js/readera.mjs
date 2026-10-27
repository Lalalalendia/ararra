/* ReadEra browser controller. Jinja2 supplies markup (ported from
 * books_platform), vanilla JS binds it to the existing authenticated API.
 * No React, client-side template library or persistent credential storage.
 */
import {
  createDraft, changeAction, destinationEdited, verifiedReplacement,
  validateDraft, confirmation, importWarningText, sourceChapterLabel,
  sourceArchiveSummary,
} from './import-model.mjs';
import {initChapterEditor} from './editor.mjs';
import {createLibraryView} from './library-view.mjs';
import {createBookExportView} from './book-export-view.mjs';
import {mountBookBatchView} from './book-batch-bind.mjs';
import {createSourceBookImportView} from './source-book-import-view.mjs';
import {renderDailyQuota,renderJobs} from './jobs-view.mjs';
import {rowInput} from './import-inputs.mjs';
import {createSessionClient} from './session-client.mjs';
import {installAuthView} from './login-view.mjs';
import {createGeminiCabinet} from './gemini-cabinet-view.mjs';

const $ = id => document.getElementById(id);
const sections = Array.from(document.querySelectorAll('[data-page]'));
const knownPages = new Set(['overview','books','import','team','jobs','editor','cabinet']);
let editor=null;
let shownPage=null;
const auth=createSessionClient();
const request=auth.request;
let identity = null;
const geminiCabinet=createGeminiCabinet({request: (...args)=>request(...args), flash});
let books = [];
let members = [];
let staged = null;
let rows = [];
let selectedFile = null;
let uploadNonce = '';
let confirmed = null;
let busy = false;
let targetBook = {book_mode:'create',book_id:'',title:'',owner_member_id:'',prefix_policy:'reject'};

function flash(text, danger=false) {
  const node = $('flash');
  node.textContent = text || '';
  node.hidden = !text;
  node.classList.toggle('ui-alert--danger', Boolean(danger));
}
function text(parent, value, tag='span') {
  const el = document.createElement(tag);
  el.textContent = String(value ?? '');
  parent.append(el);
  return el;
}
function td(row, value) { return text(row,String(value ?? ''),'td'); }
function shorten(s, limit=280) { return String(s || '').slice(0,limit); }
function clearSecret() {
  $('member-secret-code').textContent = '';
  $('member-secret').hidden = true;
}
function busyDuring(work) {
  if (busy) return;
  busy = true;
  return Promise.resolve().then(work).finally(() => {busy=false;});
}
function currentPage() {
  const page=location.pathname.slice('/app/'.length);
  return knownPages.has(page)?page:'overview';
}
function showPage(page, push=false) {
  if (!knownPages.has(page)) page='overview';
  if(shownPage==='cabinet' && page!=='cabinet')geminiCabinet.clearForm();
  if(shownPage==='editor' && page!=='editor' && editor){
    if(!editor.canLeave()){
      if(!push) history.pushState({},'', '/app/editor');
      return false;
    }
    editor.dropUnsaved();
  }
  if (push && location.pathname !== '/app/'+page) history.pushState({},'', '/app/'+page);
  sections.forEach(s => s.hidden = s.dataset.page !== page);
  shownPage=page;
  document.querySelectorAll('[data-page-link]').forEach(link => {
    const selected=link.dataset.pageLink === page;
    link.classList.toggle('is-active',selected);
    if (selected) link.setAttribute('aria-current','page');
    else link.removeAttribute('aria-current');
  });
  // Direct links to /app/editor (and browser back/forward) must reveal the
  // active section within the narrow horizontally scrollable mobile nav.
  const nav=document.querySelector('.sidebar-nav');
  if(nav && window.matchMedia('(max-width: 540px)').matches){
    const active=nav.querySelector('a[aria-current="page"]');
    if(active){
      const frame=nav.getBoundingClientRect();
      const rect=active.getBoundingClientRect();
      if(rect.left<frame.left+4 || rect.right>frame.right-4){
        nav.scrollLeft+=rect.left-frame.left-4;
      }
    }
  }
  clearSecret();
  if (!identity) return true;
  if (page==='editor' && editor) void editor.activate().catch(e=>flash(e.message,true));
  if (page==='books') void refreshBooks().catch(e => flash(e.message,true));
  if (page==='team') void refreshMembers().catch(e => flash(e.message,true));
  if (page==='jobs') void refreshJobs().catch(e => flash(e.message,true));
  if (page==='cabinet') void geminiCabinet.refresh().catch(e => flash(e.message,true));
  if (page==='overview') {
    $('metric-books').textContent=String(books.length);
    $('metric-role').textContent=identity.role==='admin'?'Администратор':'Модератор';
  }
  return true;
}
document.querySelectorAll('[data-page-link]').forEach(link=>link.addEventListener('click',event=>{
  if (!identity || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button!==0) return;
  event.preventDefault(); if(showPage(link.dataset.pageLink,true))flash('');
}));
window.addEventListener('popstate',()=>showPage(currentPage()));

async function activateSession(session) {
  identity={member_id:session.member_id,role:session.role};
  const catalog=await request('/v1/books');
  books=catalog.books||[];
  members=identity.role==='admin'
    ? (await request('/v1/admin/members')).members : [];
  $('login-form').hidden=true;$('authenticated').hidden=false;
  $('identity-label').textContent=identity.member_id+' · '+
    (identity.role==='admin'?'Администратор':'Модератор');
  $('identity-label').hidden=false;$('login-form').reset();
  $('metric-books').textContent=String(books.length);
  $('metric-role').textContent=identity.role==='admin'?'Администратор':'Модератор';
  // The sidebar must be visible before showPage measures its mobile scroll.
  document.body.dataset.authenticated='true';
  showPage(currentPage());
  // Only reveal branding in the browser chrome after the session passes API checks.
  document.title='РидЭра — переводческая платформа';
  flash('');
}
function clearWorkspace(){
  document.title='Вход';
  editor?.reset();
  batchView.reset();
  auth.clear();identity=null;books=[];members=[];staged=null;rows=[];confirmed=null;
  clearSecret();geminiCabinet.clear();$('identity-label').hidden=true;
  $('authenticated').hidden=true;
  hideImportSteps();
}

async function chooseBookOwner(book, candidates) {
  const modal=$('book-owner-dialog');
  if(typeof modal.showModal!=='function'){
    throw new Error('Браузер не поддерживает безопасный диалог переназначения.');
  }
  if(modal.open) throw new Error('Закройте предыдущее назначение.');

  const select=$('book-owner-target');
  select.replaceChildren();
  for(const member of candidates){
    const option=document.createElement('option');
    option.value=member.member_id;
    option.textContent=member.member_id;
    select.append(option);
  }
  $('book-owner-title').textContent='Ответственный за «'+book.title+'»';
  $('book-owner-help').textContent=book.owner_member_id
    ? 'Текущий ответственный: '+book.owner_member_id+'. Выберите другого модератора.'
    : 'У книги ещё нет ответственного. Выберите модератора.';

  // "cancel" is the safe default for Escape/closing without confirmation.
  modal.returnValue='cancel';
  const selection=new Promise(resolve=>{
    modal.addEventListener('close',()=>{
      resolve(modal.returnValue==='confirm' ? select.value : null);
    },{once:true});
  });
  modal.showModal();
  select.focus();
  return selection;
}

const exportView=createBookExportView({
  dialog:$('book-export-dialog'),title:$('book-export-title'),help:$('book-export-help'),
  last:$('book-export-last'),status:$('book-export-status'),check:$('book-export-preview'),
  txt:$('book-export-txt'),html:$('book-export-html'),request,
  showError:e=>flash(e.message,true),notify:m=>flash(m),
});
const batchView=mountBookBatchView({lookup:$,request,flash,onNavigate:()=>showPage('books',true)});
const libraryView=createLibraryView({
  body:$('book-list'),filter:$('book-filter'),ownerFilter:$('book-owner-filter'),
  empty:$('books-empty'),request,openExport:book=>exportView.open(book),
  openBatch:book=>batchView.open(book),
  currentMember:()=>identity?.member_id || '',
  canReassign:()=>identity?.role==='admin',
  activeModerators:()=>members.filter(m=>m.role==='moderator' && m.status==='active'),
  chooseOwner:chooseBookOwner,
  reassignOwner:async(book,newOwner)=>{
    try{
      await request('/v1/books/'+encodeURIComponent(book.book_id)+'/owner',{
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          owner_member_id:newOwner,expected_owner_revision:book.owner_revision,
        }),
      });
    }catch(error){
      // A parallel admin may already have transferred this book. Refresh the
      // live CAS revision even when the attempted transfer was rejected.
      await refreshBooks().catch(()=>{});
      throw error;
    }
    await refreshBooks();
    flash('Ответственный за книгу изменён.');
  },
  openEditor:(bookId,chapterId='')=>{
    editor?.selectOnActivation(bookId,chapterId);
    if(showPage('editor',true))flash('');
  },
  showError:error=>flash(error.message,true),
});

async function refreshBooks() {
  const reply=await request('/v1/books');
  books=reply.books || [];
  libraryView.render(books);
  $('metric-books').textContent=String(books.length);
  fillBooksSelect();
}

async function refreshMembers() {
  $('team-admin-only').hidden=identity.role!=='admin';
  $('team-no-admin').hidden=identity.role==='admin';
  if (identity.role!=='admin') return;
  members=(await request('/v1/admin/members')).members;
  const root=$('members-list');root.replaceChildren();
  for (const member of members) {
    const tr=document.createElement('tr');
    td(tr,member.member_id);
    td(tr,member.role==='admin'?'Администратор':'Модератор');
    td(tr,member.status==='active'?'Активен':'Отключён');
    const actions=document.createElement('td');
    if (member.role==='moderator') {
      const rotate=text(actions,'Перевыпустить','button');
      rotate.type='button';rotate.className='ui-button ui-button--ghost';
      rotate.addEventListener('click',()=>void modifyMember(member.member_id,'rotate'));
      if (member.status==='active') {
        const disable=text(actions,'Отключить','button');
        disable.type='button';disable.className='ui-button ui-button--danger';
        disable.addEventListener('click',()=>void modifyMember(member.member_id,'disable'));
      }
    } else text(actions,'Управляется на сервере');
    tr.append(actions);root.append(tr);
  }
  fillOwnerSelect();
}
function revealSecret(reply) {
  $('member-secret-code').textContent=reply.access_token||'';
  $('member-secret-heading').textContent='Новый ключ — только сейчас';
  $('member-secret').hidden=false;
}
$('member-invite').addEventListener('submit',event=>{
  event.preventDefault();clearSecret();flash('');
  const form=event.currentTarget;
  void busyDuring(async()=>{
    try {
      const memberId=String(new FormData(form).get('member_id')||'').trim();
      if (!/^[A-Za-z][A-Za-z0-9._-]{0,63}$/.test(memberId)) throw new Error('Некорректный ID модератора.');
      const issued=await request('/v1/admin/members',{
        method:'POST',body:JSON.stringify({member_id:memberId}),
        headers:{'Content-Type':'application/json'},
      });
      revealSecret(issued);$('member-invite').reset();await refreshMembers();
      flash('Сохраните новый ключ и передайте модератору по защищённому каналу.');
    } catch(e){flash(e.message,true);}
  });
});
$('member-secret-hide').addEventListener('click',clearSecret);
async function modifyMember(memberId,action) {
  if (identity?.role!=='admin') return;
  const message=action==='rotate'
    ? 'Перевыпустить ключ «'+memberId+'»? Предыдущий перестанет работать.'
    : 'Отключить «'+memberId+'»? Ожидающие задания будут отменены. Все книги сначала должны быть переназначены.';
  if (!window.confirm(message)) return;
  clearSecret();flash('');
  await busyDuring(async()=>{
    try {
      const result=await request('/v1/admin/members/'+encodeURIComponent(memberId)+'/'+action,{method:'POST'});
      await refreshMembers();
      if (result.access_token) revealSecret(result);
      flash(action==='rotate'?'Новый ключ выдан однократно.':'Доступ модератора отключён.');
    } catch(e){flash(e.message,true);}
  });
}
async function refreshJobs() {
  const [jobsResult,quotaResult]=await Promise.allSettled([
    request('/v1/jobs?limit=50'),
    request('/v1/quota/gemini'),
  ]);
  if(jobsResult.status!=='fulfilled') throw jobsResult.reason;
  renderDailyQuota($('gemini-quota-status'),
    quotaResult.status==='fulfilled' ? quotaResult.value : null);
  const jobs=jobsResult.value.jobs || [];
  renderJobs($('jobs-list'),jobs,{
    text,shorten,
    renderStatus:window.ReadEraStatus
      ? (item,status)=>window.ReadEraStatus.renderStatus(item,status)
      : null,
  });
  $('metric-jobs').textContent=String(
    jobs.filter(j=>['queued','retry_wait','running'].includes(j.status)).length);
}
$('jobs-refresh').addEventListener('click',()=>void refreshJobs().catch(e=>flash(e.message,true)));

function hideImportSteps() {
  sourceBookImportView.reset();
  $('import-upload').hidden=false;$('import-resume').hidden=false;
  $('import-map').hidden=true;$('import-review').hidden=true;$('import-success').hidden=true;
  $('import-ack').checked=false;$('import-confirm').disabled=true;
  staged=null;rows=[];confirmed=null;batchView.offerImported(null);
  markStep(1);
}
function markStep(n) {
  document.querySelectorAll('#import-steps li').forEach(el=>{
    el.classList.toggle('is-current',Number(el.dataset.step)===n);
    el.classList.toggle('is-done',Number(el.dataset.step)<n);
  });
}
function fillOwnerSelect() {
  const select=$('import-owner');select.replaceChildren();
  const empty=document.createElement('option');empty.value='';empty.textContent='Выберите модератора';select.append(empty);
  if (identity?.role==='admin') {
    for (const m of members.filter(x=>x.role==='moderator'&&x.status==='active')) {
      const option=document.createElement('option');option.value=m.member_id;option.textContent=m.member_id;
      select.append(option);
    }
  } else if (identity) {
    const option=document.createElement('option');option.value=identity.member_id;option.textContent=identity.member_id;
    select.append(option);select.value=identity.member_id;
  }
}
function fillBooksSelect() {
  const select=$('import-existing');select.replaceChildren();
  const empty=document.createElement('option');empty.value='';empty.textContent='Выберите книгу';select.append(empty);
  for(const b of books){
    const opt=document.createElement('option');opt.value=b.book_id;opt.textContent=b.title+' · '+b.book_id;
    select.append(opt);
  }
}
function currentTarget() {
  const mode=document.querySelector('input[name="book_mode"]:checked')?.value || 'create';
  const id=mode==='existing' ? $('import-existing').value : $('import-book-id').value.trim();
  const pre=document.querySelector('input[name="prefix_policy"]:checked');
  return {
    book_mode:mode,book_id:id,title: mode==='create'?$('import-book-title').value:'',
    owner_member_id:mode==='create'
      ? (identity?.role==='admin' ? $('import-owner').value : identity?.member_id||'') : null,
    prefix_policy: staged?.unassigned_prefix_saved ? (pre?.value || 'reject') : 'reject',
  };
}
function stageShown(stage) {
  sourceBookImportView.hide();
  staged=stage;
  rows=createDraft(stage.preview);
  confirmed=stage.commit_result || null;
  $('import-upload').hidden=true;$('import-resume').hidden=true;
  $('import-map').hidden=Boolean(confirmed);$('import-review').hidden=true;
  $('import-success').hidden=!confirmed;
  $('import-book-id').value='book-'+stage.stage_id.slice(0,8);
  $('import-book-title').value=stage.preview.title || '';
  $('stage-details').textContent=stage.preview.filename+' · '+stage.preview.chapter_count+
    ' записей (сюжетных глав: '+(stage.preview.story_chapter_count ?? stage.preview.chapter_count)+
    ') · ID временной сессии: '+stage.stage_id+
    ' · истекает '+new Date(stage.expires_at*1000).toLocaleString('ru-RU');
  $('prefix-fieldset').hidden=!stage.unassigned_prefix_saved;
  $('prefix-preview').value=stage.unassigned_prefix_html || stage.preview.unassigned_prefix_sample || '';
  document.querySelectorAll('input[name="prefix_policy"]').forEach(x=>x.checked=false);
  document.querySelector('input[name="book_mode"][value="create"]').checked=true;
  $('import-warnings').hidden=!(stage.preview.warnings||[]).length;
  $('import-warnings').replaceChildren();
  for(const w of stage.preview.warnings || [])text($('import-warnings'),importWarningText(w),'p');
  const archive=sourceArchiveSummary(stage.preview);
  const provenance=$('import-archive-details');provenance.replaceChildren();provenance.hidden=!archive;
  $('import-archive-ack-wrap').hidden=!archive;
  $('import-archive-ack').checked=false;
  if(archive){
    text(provenance,'Архив Full Cycle App · границы глав из manifest.json','strong');
    text(provenance,'Исходный сайт: '+archive.domain+' · '+archive.sourceUrl,'p');
    text(provenance,'В ZIP: '+archive.chapterCount+' глав. Рабочий текст отличается от оригинала у '+archive.modifiedCount+' глав.','p');
    text(provenance,'Пропуски позиций: '+archive.gapsLabel,'p');
    text(provenance,archive.completenessWarning,'p');
  }
  fillOwnerSelect();fillBooksSelect();syncBookMode();renderMapping();
  $('import-resume').elements.stage_id.value=stage.stage_id;
  if(confirmed) showReceipt(confirmed);else markStep(2);
}
function syncBookMode() {
  const existing=document.querySelector('input[name="book_mode"]:checked')?.value==='existing';
  $('import-existing-wrap').hidden=!existing;
  $('import-title-wrap').hidden=existing;
  $('import-owner-wrap').hidden=existing;
  $('import-book-id').closest('label').hidden=existing;
  if (existing) {
    const selected=$('import-existing').value;
    $('import-book-id').value=selected;
  }
  rows=rows.map(row=>row.action==='replace'
    ? {...row,expected_revision:null,verified_book_id:null,verified_chapter_id:null} : row);
  renderMapping();
}
document.querySelectorAll('input[name="book_mode"]').forEach(el=>el.addEventListener('change',syncBookMode));
$('import-existing').addEventListener('change',()=>{
  $('import-book-id').value=$('import-existing').value;
  rows=rows.map(row=>row.action==='replace'
    ? {...row,expected_revision:null,verified_book_id:null,verified_chapter_id:null} : row);
  renderMapping();
});
function renderMapping() {
  const body=$('chapter-map');body.replaceChildren();
  if(!staged)return;
  for(const row of rows){
    const tr=document.createElement('tr');
    const info=document.createElement('td');
    text(info,'#'+row.sequence+' · '+row.title,'strong');
    const kind=row.entry_type==='extra'?'Дополнительная запись':
      row.entry_type==='service'?'Служебная запись':'Сюжетная глава';
    text(info,kind+' · '+sourceChapterLabel(row),'small');
    text(info,shorten(row.sample,90),'small');
    if(row.source_original_sha256 && row.source_working_sha256){
      text(info,'Позиция в архиве: '+row.catalog_ordinal+' · URL: '+shorten(row.source_url||'не указан',100),'small');
      text(info,'Оригинал SHA-256: '+row.source_original_sha256.slice(0,12)+
        '… · Рабочая версия: '+row.source_working_sha256.slice(0,12)+'…'+
        (row.source_original_sha256!==row.source_working_sha256?' (исправлена)':''),'small');
    }
    tr.append(info);
    const cellAction=document.createElement('td');
    const select=document.createElement('select');select.className='ui-input';
    select.setAttribute('aria-label','Действие главы '+row.sequence);
    for (const [value,label] of [['review','Требует решения'],['create','Создать'],['replace','Заменить'],['skip','Пропустить']]){
      if(value==='review'&&row.action!=='review')continue;
      const option=document.createElement('option');option.value=value;option.textContent=label;
      option.selected=value===row.action;
      option.disabled=value==='replace'&&document.querySelector('input[name="book_mode"]:checked')?.value==='create';
      select.append(option);
    }
    select.addEventListener('change',()=>{
      rows=rows.map(r=>r.sequence===row.sequence?changeAction(r,select.value):r);
      renderMapping();
    });
    cellAction.append(select);tr.append(cellAction);
    for(const key of ['chapter_id','chapter_index','chapter_path']){
      const tdEl=document.createElement('td');
      const input=rowInput('input',row[key],key+' для главы '+row.sequence,value=>{
        rows=rows.map(r=>r.sequence===row.sequence?destinationEdited(r,key,value):r);
        if(key==='chapter_index'){
          const latest=rows.find(r=>r.sequence===row.sequence);
          for(const dependent of ['chapter_id','chapter_path']){
            const target=tr.querySelector('[data-import-field="'+dependent+'"]');
            if(target&&latest)target.value=latest[dependent];
          }
        }
        if(row.action==='replace'){
          const latest=rows.find(r=>r.sequence===row.sequence);
          if(latest){const note=tr.querySelector('[data-rev-note]');if(note)note.textContent='Не проверена';}
        }
      });
      input.disabled=row.action==='skip';
      if(key==='chapter_index') input.inputMode='numeric';
      input.dataset.importField=key;
      tdEl.append(input);tr.append(tdEl);
    }
    const revision=document.createElement('td');
    const status=text(revision,row.action==='review'?'Нужно решение':row.action==='replace'
      ? (row.expected_revision===null?'Не проверена':'rev '+row.expected_revision)
      : row.action==='skip'?'Пропуск':'Новая','span');
    status.dataset.revNote='';
    if(row.action==='replace') {
      const check=text(revision,'Проверить','button');
      check.type='button';check.className='ui-button ui-button--ghost';
      check.addEventListener('click',()=>void verifyRow(row.sequence));
    }
    tr.append(revision);body.append(tr);
  }
}
async function verifyRow(sequence) {
  const row=rows.find(r=>r.sequence===sequence);const target=currentTarget();
  if(!row||row.action!=='replace'||target.book_mode!=='existing'||!target.book_id||!row.chapter_id.trim()){
    flash('Для замены нужно выбрать существующую книгу и ID главы.',true);return;
  }
  await busyDuring(async()=>{
    try {
      const oldBook=target.book_id,oldChapter=row.chapter_id.trim();
      const remote=await request('/v1/books/'+encodeURIComponent(oldBook)+'/chapters/'+encodeURIComponent(oldChapter));
      if(currentTarget().book_id!==oldBook)throw new Error('Книга изменилась во время проверки. Проверьте ревизию ещё раз.');
      rows=rows.map(r=>r.sequence===sequence?verifiedReplacement(r,oldBook,remote):r);
      renderMapping();flash('Ревизия главы '+sequence+' проверена.');
    } catch(e){flash(e.message,true);}
  });
}
$('import-upload').addEventListener('submit',event=>{
  event.preventDefault();
  void busyDuring(async()=>{
    const file=selectedFile||$('chapters_file').files[0];
    const limit=/\.zip$/i.test(file?.name||'')?24*1024*1024:12*1024*1024;
    if(!file||!/\.(txt|html?|xhtml|epub|docx|zip)$/i.test(file.name)||!file.size||file.size>limit){
      flash('Выберите TXT/HTML/EPUB/DOCX до 12 МиБ или ZIP Full Cycle App до 24 МиБ.',true);return;
    }
    try {
      const form=new FormData();form.append('file',file,file.name);
      const snapshot=await request('/v1/imports/stages',{
        method:'POST',body:form,headers:{'Idempotency-Key':uploadNonce||crypto.randomUUID()},
      });
      stageShown(await request('/v1/imports/stages/'+encodeURIComponent(snapshot.stage_id)));
      flash('Файл проверен. Пока ничего не сохранено в библиотеку.');
    }catch(e){flash(e.message,true);}
  });
});
function showImportFileSelection() {
  const file=selectedFile||$('chapters_file').files[0];
  $('import-file-state').textContent=file
    ? file.name+' · '+(file.size/(1024*1024)).toFixed(2)+' МиБ'
    : 'Файл не выбран.';
  $('import-dropzone').classList.toggle('has-file',Boolean(file));
}
$('chapters_file').addEventListener('change',event=>{
  selectedFile=event.target.files[0]||null;uploadNonce=crypto.randomUUID();
  showImportFileSelection();
});
const dropzone=$('import-dropzone');
for(const kind of ['dragenter','dragover']){
  dropzone.addEventListener(kind,event=>{
    if(!event.dataTransfer?.types?.includes('Files'))return;
    event.preventDefault();event.dataTransfer.dropEffect='copy';
    dropzone.classList.add('is-over');
  });
}
dropzone.addEventListener('dragleave',event=>{
  if(!dropzone.contains(event.relatedTarget))dropzone.classList.remove('is-over');
});
$('import-upload').addEventListener('dragover',event=>event.preventDefault());
$('import-upload').addEventListener('drop',event=>{
  event.preventDefault();dropzone.classList.remove('is-over');
  const files=event.dataTransfer?.files;
  if(files?.length>1){
    flash('Загрузите один файл за раз.',true);return;
  }
  if(files?.length===1){
    selectedFile=files[0];uploadNonce=crypto.randomUUID();
    const data=new DataTransfer();data.items.add(selectedFile);$('chapters_file').files=data.files;
    showImportFileSelection();
  }
});
$('import-resume').addEventListener('submit',event=>{
  event.preventDefault();
  const form=event.currentTarget;
  void busyDuring(async()=>{
    try {
      const stageId=String(new FormData(form).get('stage_id')||'').trim();
      stageShown(await request('/v1/imports/stages/'+encodeURIComponent(stageId)));
      flash('Сессия восстановлена. Неподтверждённые назначения глав нужно проверить заново.');
    } catch(e){flash(e.message,true);}
  });
});
$('import-abort').addEventListener('click',()=>{$('import-abort-confirm').hidden=false;});
$('import-abort-no').addEventListener('click',()=>{$('import-abort-confirm').hidden=true;});
$('import-abort-yes').addEventListener('click',()=>{
  if(!staged||confirmed)return;
  void busyDuring(async()=>{
    try{
      await request('/v1/imports/stages/'+encodeURIComponent(staged.stage_id),{method:'DELETE'});
      hideImportSteps();flash('Временные данные отменены; слот импорта освобождён.');
    }catch(e){flash(e.message,true);}
  });
});
$('import-review-button').addEventListener('click',()=>{
  if(!staged)return;
  targetBook=currentTarget();
  const errors=validateDraft(rows,targetBook,staged);
  if(errors.length){flash(errors.slice(0,12).join(' \n'),true);return;}
  const root=$('import-summary');root.replaceChildren();
  const counts={create:0,replace:0,skip:0};
  rows.forEach(r=>counts[r.action]++);
  [
    ['Книга',targetBook.book_id],
    ['Режим',targetBook.book_mode==='create'?'Создание книги':'Дополнение книги'],
    ['Создать глав',counts.create],
    ['Заменить глав',counts.replace],
    ['Пропустить глав',counts.skip],
    ['Предисловие',staged.unassigned_prefix_saved?targetBook.prefix_policy:'Не обнаружено'],
    ['Хеш файла',staged.file_sha256],
    ...(sourceArchiveSummary(staged.preview) ? [
      ['ZIP Full Cycle App',sourceArchiveSummary(staged.preview).domain],
      ['Пропуски позиций',sourceArchiveSummary(staged.preview).gapsLabel],
      ['Полнота сайта','Не подтверждена по архиву'],
    ] : []),
  ].forEach(([label,value])=>{
    const div=document.createElement('p');
    text(div,label+': ','strong');text(div,value);root.append(div);
  });
  $('import-map').hidden=true;$('import-review').hidden=false;markStep(3);flash('');
  $('import-ack').checked=false;
  $('import-archive-ack').checked=false;
  refreshImportConfirmEnabled();
});
function refreshImportConfirmEnabled(){
  const archive=sourceArchiveSummary(staged?.preview);
  $('import-confirm').disabled=!$('import-ack').checked ||
    (archive!==null&&!$('import-archive-ack').checked);
}
$('import-ack').addEventListener('change',refreshImportConfirmEnabled);
$('import-archive-ack').addEventListener('change',refreshImportConfirmEnabled);
$('import-back').addEventListener('click',()=>{
  $('import-review').hidden=true;$('import-map').hidden=false;
  $('import-ack').checked=false;$('import-archive-ack').checked=false;markStep(2);
});
$('import-confirm').addEventListener('click',()=>{
  if(!staged||!$('import-ack').checked||
     (sourceArchiveSummary(staged.preview)!==null&&!$('import-archive-ack').checked)||confirmed)return;
  const errors=validateDraft(rows,targetBook,staged);
  if(errors.length){flash(errors.slice(0,12).join(' \n'),true);return;}
  void busyDuring(async()=>{
    try {
      const receipt=await request('/v1/imports/stages/'+encodeURIComponent(staged.stage_id)+'/confirm',{
        method:'POST',body:JSON.stringify(confirmation(rows,targetBook,staged)),
        headers:{'Content-Type':'application/json'},
      });
      showReceipt(receipt);await refreshBooks();flash('Сервер подтвердил атомарный импорт.');
    }catch(e){flash(e.message,true);}
  });
});
function showReceipt(receipt){
  confirmed=receipt;
  const el=$('import-receipt');el.replaceChildren();
  [
    ['Книга',receipt.book_id],
    ['Создано/заменено',receipt.imported_count],
    ['Пропущено',receipt.skipped_count],
    ['Предисловие',receipt.unassigned_prefix_preserved?'Сохранено':'Не сохранялось'],
    ['ID операции',receipt.stage_id],
  ].forEach(([label,value])=>{const p=document.createElement('p');text(p,label+': ','strong');text(p,value);el.append(p);});
  $('import-map').hidden=true;$('import-review').hidden=true;$('import-success').hidden=false;markStep(3);
  batchView.offerImported(receipt);
}
$('import-another').addEventListener('click',()=>{hideImportSteps();$('chapters_file').value='';selectedFile=null;flash('');});
const sourceBookImportView=createSourceBookImportView({
  request,flash,
  onStage:async stageId=>stageShown(
    await request('/v1/imports/stages/'+encodeURIComponent(stageId))
  ),
});
editor=initChapterEditor({request,flash,getBooks:()=>books});
hideImportSteps();
showPage(currentPage());
installAuthView({auth,activateSession,clearWorkspace,flash,busyDuring,
  canLeave:()=>shownPage!=='editor'||!editor||editor.canLeave()});
