/** ReadEra shared CN/RU editor. Jinja form follows books_platform's
 * chapter_form.html but uses the existing accepted-revision CAS API.
 *
 * Source HTML is read from a trusted team API but is NEVER rendered as
 * innerHTML. Draft text is kept only in browser memory until accepted.
 */
import {initEditorialPanels} from './editorial-panels.mjs';

const $ = id => document.getElementById(id);
const richPattern = /<(?:a|strong|em|b|i|span|img|table|ul|ol|li|blockquote)\b/i;

function sourceAsText(html) {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  doc.querySelectorAll('script,style,noscript,template').forEach(el=>el.remove());
  doc.querySelectorAll('br').forEach(el=>el.replaceWith(doc.createTextNode('\n')));
  const blocks = [...doc.body.children].map(el=>el.textContent.trim()).filter(Boolean);
  return (blocks.length?blocks.join('\n\n'):doc.body.textContent || '')
    .replace(/\n{3,}/g,'\n\n').trim();
}
function escapeHTML(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function textToHTML(value) {
  const clean=String(value||'').replace(/\r\n?/g,'\n').trim();
  return clean.split(/\n\s*\n/).map(para=>
    '<p>'+escapeHTML(para).replace(/\n/g,'<br>')+'</p>').join('\n');
}
function option(value,label) {
  const el=document.createElement('option');
  el.value=value;el.textContent=label;return el;
}

export function initChapterEditor({request,flash,getBooks}) {
  let chosenBook='', chosenChapter='', chapter=null, toc=[];
  let savedText='',acceptedText='',hasRich=false,busy=false,sequence=0;
  let draftRevision=0,draftSourceRevision=0,draftAcceptedRevision=0;
  let panels;

  const edit=$('editor-text');
  const bookChoice=$('editor-book');
  const chapterChoice=$('editor-chapter');

  function dirty(){return Boolean(chapter)&&edit.value!==savedText;}
  function draftStale(){return Boolean(chapter&&draftRevision>0)&&(
    draftSourceRevision!==chapter.source_revision ||
    draftAcceptedRevision!==chapter.accepted_revision);}
  function canLeave(){
    if(busy){flash('Дождитесь завершения сохранения главы.',true);return false;}
    return !dirty() || window.confirm('У текущей главы есть несохранённые изменения. Отменить их?');
  }
  function dropUnsaved() {
    if(dirty()) { edit.value=savedText; adjust(); }
  }
  function reset() {
    dropUnsaved(); chosenBook=''; toc=[]; ++sequence; clearChapter();
  }
  function adjust() {
    const unsaved=dirty(),stale=draftStale();
    $('editor-unsaved').hidden=!unsaved;
    $('editor-draft-stale').hidden=!stale;
    $('editor-rebase-wrap').hidden=!stale;
    $('editor-save-draft').disabled=busy || (!unsaved&&!stale) ||
      (stale&&!$('editor-rebase').checked);
    $('editor-accept').disabled=busy || !edit.value.trim() ||
      edit.value===acceptedText || stale ||
      (hasRich&&!$('editor-format-ack').checked);
  }
  function clearChapter(){
    chapter=null;chosenChapter='';savedText='';acceptedText='';
    draftRevision=0;draftSourceRevision=0;draftAcceptedRevision=0;hasRich=false;
    edit.value='';$('editor-source').textContent='';
    $('editor-workspace').hidden=true;$('editor-empty').hidden=false;
    $('editor-accept').disabled=true;$('editor-save-draft').disabled=true;
    $('editor-unsaved').hidden=true;$('editor-format-warning').hidden=true;
    $('editor-rebase-wrap').hidden=true;$('editor-draft-stale').hidden=true;
    $('editor-rebase').checked=false;
    $('editor-draft-status').textContent='Черновик ещё не сохранён.';
    $('editor-draft-rev').textContent='—';
    panels?.blank();
  }
  function revState(){
    if(!chapter)return;
    $('editor-source-rev').textContent='rev '+chapter.source_revision;
    $('editor-accepted-rev').textContent='rev '+chapter.accepted_revision;
    $('editor-stale').hidden=!(chapter.accepted_revision>0&&!chapter.accepted_current);
    const node=$('editor-info');node.replaceChildren();
    for(const [title,value] of [
      ['ID главы',chapter.chapter_id],
      ['Номер',chapter.chapter_index],
      ['Путь',chapter.chapter_path],
      ['Принятие',chapter.accepted_revision>0
        ? (chapter.accepted_current?'Актуально':'Устарело после изменения оригинала')
        : 'Ещё не принято'],
    ]){
      const item=document.createElement('p');
      const b=document.createElement('strong');
      b.textContent=title+': ';item.append(b,document.createTextNode(String(value)));
      node.append(item);
    }
  }
  function displayChapter(data){
    chapter=data;chosenChapter=data.chapter_id;
    $('editor-source').textContent=sourceAsText(data.source_html);
    acceptedText=sourceAsText(data.accepted_html);
    savedText=acceptedText;
    edit.value=acceptedText;
    draftRevision=0;draftSourceRevision=data.source_revision;
    draftAcceptedRevision=data.accepted_revision;
    $('editor-draft-rev').textContent='—';
    $('editor-draft-status').textContent='Черновик ещё не сохранён.';
    hasRich=Boolean(data.accepted_html&&richPattern.test(data.accepted_html));
    $('editor-format-warning').hidden=!hasRich;
    $('editor-format-ack').checked=false;
    $('editor-workspace').hidden=false;$('editor-empty').hidden=true;
    revState();adjust();
  }
  async function readChapter(id){
    if(!chosenBook||!id){clearChapter();return;}
    const ticket=++sequence;
    const data=await request('/v1/books/'+encodeURIComponent(chosenBook)
      +'/chapters/'+encodeURIComponent(id));
    if(ticket!==sequence)return;
    displayChapter(data);
    chapterChoice.value=id;
    syncNav();
    const base='/v1/books/'+encodeURIComponent(chosenBook)
      +'/chapters/'+encodeURIComponent(id);
    const draft=await request(base+'/draft');
    if(ticket!==sequence)return;
    draftRevision=draft.revision;
    draftSourceRevision=draft.source_revision;
    draftAcceptedRevision=draft.accepted_revision;
    if(draft.revision>0){
      // Never overwrite text typed while the draft request was in flight.
      if(edit.value===savedText)edit.value=draft.body_text;
      savedText=draft.body_text;
      $('editor-draft-rev').textContent='rev '+draft.revision;
      $('editor-draft-status').textContent='Сохранён '+new Date(draft.updated_at*1000)
        .toLocaleString('ru-RU')+' · '+draft.actor_id;
    }
    adjust();
    await panels.load();
  }
  function syncNav(){
    const idx=toc.findIndex(r=>r.chapter_id===chosenChapter);
    $('editor-prev').disabled=idx<=0;
    $('editor-next').disabled=idx<0||idx>=toc.length-1;
  }
  async function readBook(bookId){
    const ticket=++sequence;
    chosenBook=bookId;clearChapter();
    chapterChoice.replaceChildren(option('','Выберите главу'));
    if(!bookId){toc=[];return;}
    const response=await request('/v1/books/'+encodeURIComponent(bookId)+'/chapters');
    if(ticket!==sequence)return;
    toc=response.chapters || [];
    for(const row of toc){
      const state=row.accepted_revision>0
        ? (row.accepted_current?' · принято':' · устарело') : ' · исходник';
      chapterChoice.append(option(row.chapter_id,
        'Глава '+row.chapter_index+' · '+row.chapter_id+state));
    }
    if(toc.length)await readChapter(toc[0].chapter_id);
    else $('editor-empty').textContent='В книге пока нет глав. Сначала импортируйте оригиналы.';
  }
  async function activate(){
    const books=getBooks();
    const oldChoice=chosenBook;
    bookChoice.replaceChildren(option('','Выберите книгу'));
    for(const b of books)bookChoice.append(option(b.book_id,b.title+' · '+b.book_id));
    const target=books.some(b=>b.book_id===oldChoice)?oldChoice:(books[0]?.book_id||'');
    bookChoice.value=target;
    if(target!==chosenBook||!chapter)await readBook(target);
  }
  async function switchBook(target){
    if(target===chosenBook)return;
    if(!canLeave()){bookChoice.value=chosenBook;return;}
    try{await readBook(target);}
    catch(e){flash(e.message,true);}
  }
  async function switchChapter(target){
    if(target===chosenChapter)return;
    if(!canLeave()){chapterChoice.value=chosenChapter;return;}
    try{await readChapter(target);}
    catch(e){flash(e.message,true);}
  }

  bookChoice.addEventListener('change',()=>void switchBook(bookChoice.value));
  chapterChoice.addEventListener('change',()=>void switchChapter(chapterChoice.value));
  for(const [control,step] of [['editor-prev',-1],['editor-next',1]]){
    $(control).addEventListener('click',()=>{
      const pos=toc.findIndex(r=>r.chapter_id===chosenChapter);
      const next=toc[pos+step];
      if(next){
        chapterChoice.value=next.chapter_id;
        void switchChapter(next.chapter_id);
      }
    });
  }
  edit.addEventListener('input',adjust);
  $('editor-rebase').addEventListener('change',adjust);
  $('editor-save-draft').addEventListener('click',()=>{
    if(!chapter||busy||!edit.value.trim()||(!dirty()&&!draftStale()))return;
    if(draftStale()&&!$('editor-rebase').checked){
      flash('Подтвердите осознанную перепривязку черновика к текущим ревизиям.',true);
      return;
    }
    const id=chosenChapter,bookId=chosenBook,body=edit.value;
    const base='/v1/books/'+encodeURIComponent(bookId)+'/chapters/'+encodeURIComponent(id);
    const previous=draftRevision;
    busy=true;adjust();
    void request(base+'/draft',{
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        body_text:body,expected_revision:previous,
        expected_source_revision:chapter.source_revision,
        expected_accepted_revision:chapter.accepted_revision,
        rebase:$('editor-rebase').checked,
      }),
    }).then(data=>{
      if(bookId!==chosenBook||id!==chosenChapter)return;
      draftRevision=data.revision;draftSourceRevision=data.source_revision;
      draftAcceptedRevision=data.accepted_revision;savedText=body;
      $('editor-draft-rev').textContent='rev '+data.revision;
      $('editor-draft-status').textContent='Сохранено на сервере · '+data.actor_id;
      $('editor-rebase').checked=false;
      flash('Черновик сохранён. Принятая редакция не изменена.');
    }).catch(e=>flash(e.message+' Текущий текст не заменён.',true))
      .finally(()=>{busy=false;adjust();});
  });
  $('editor-format-ack').addEventListener('change',adjust);
  $('editor-reload').addEventListener('click',()=>{
    if(!chapter||!canLeave())return;
    void readChapter(chosenChapter).then(()=>flash('Глава обновлена с сервера.'))
      .catch(e=>flash(e.message,true));
  });
  $('chapter-editor').addEventListener('submit',event=>{
    event.preventDefault();
    if(!chapter||busy||edit.value===acceptedText||!edit.value.trim()||draftStale())return;
    if(hasRich&&!$('editor-format-ack').checked){
      flash('Подтвердите преобразование сложного форматирования в текстовые абзацы.',true);
      return;
    }
    const bookId=chosenBook,chapterId=chosenChapter;
    const saved=chapter,content=textToHTML(edit.value);
    busy=true;adjust();
    void request('/v1/books/'+encodeURIComponent(bookId)
      +'/chapters/'+encodeURIComponent(chapterId)+'/accepted-translation',{
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        translated_html:content,
        expected_revision:saved.accepted_revision,
        expected_source_revision:saved.source_revision,
      }),
    }).then(data=>{
      // An in-flight save must not replace a different chapter's editor.
      if(bookId===chosenBook&&chapterId===chosenChapter){
        displayChapter(data);
        void readChapter(chapterId).catch(e=>flash(e.message,true));
        flash('Принята редакция '+data.accepted_revision+
          '. История предыдущих редакций сохранена.');
      }
    }).catch(error=>{
      flash(error.message+' Ревизия не изменена; рабочий текст остался в редакторе.',true);
    }).finally(()=>{busy=false;adjust();});
  });
  window.addEventListener('beforeunload',event=>{
    if(dirty()){event.preventDefault();event.returnValue='';}
  });
  panels=initEditorialPanels({
    request,flash,toText:sourceAsText,
    current:()=>chapter?{
      bookId:chosenBook,chapterId:chosenChapter,
      sourceRevision:chapter.source_revision,
      acceptedRevision:chapter.accepted_revision,
      draftRevision,dirty,outdated:draftStale,
    }:null,
    reloadChapter:()=>readChapter(chosenChapter),
  });
  clearChapter();syncNav();
  return {activate,canLeave,dropUnsaved,reset};
}
