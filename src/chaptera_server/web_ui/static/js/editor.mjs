/** ReadEra shared CN/RU editor. Jinja form follows books_platform's
 * chapter_form.html but uses the existing accepted-revision CAS API.
 *
 * Source HTML is read from a trusted team API but is NEVER rendered as
 * innerHTML. Draft text is kept only in browser memory until accepted.
 */
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
  let savedText='',hasRich=false,busy=false,sequence=0;

  const edit=$('editor-text');
  const bookChoice=$('editor-book');
  const chapterChoice=$('editor-chapter');

  function dirty(){return Boolean(chapter)&&edit.value!==savedText;}
  function canLeave(){
    return !dirty() || window.confirm('У текущей главы есть несохранённые изменения. Отменить их?');
  }
  function dropUnsaved() {
    if(dirty()) { edit.value=savedText; adjust(); }
  }
  function reset() {
    dropUnsaved(); chosenBook=''; toc=[]; ++sequence; clearChapter();
  }
  function adjust() {
    const unsaved=dirty();
    $('editor-unsaved').hidden=!unsaved;
    $('editor-accept').disabled=busy || !unsaved || !edit.value.trim()
      || (hasRich&&!$('editor-format-ack').checked);
  }
  function clearChapter(){
    chapter=null;chosenChapter='';savedText='';hasRich=false;
    edit.value='';$('editor-source').textContent='';
    $('editor-workspace').hidden=true;$('editor-empty').hidden=false;
    $('editor-accept').disabled=true;$('editor-unsaved').hidden=true;
    $('editor-format-warning').hidden=true;
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
    savedText=sourceAsText(data.accepted_html);
    edit.value=savedText;
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
  $('editor-format-ack').addEventListener('change',adjust);
  $('editor-reload').addEventListener('click',()=>{
    if(!chapter||!canLeave())return;
    void readChapter(chosenChapter).then(()=>flash('Глава обновлена с сервера.'))
      .catch(e=>flash(e.message,true));
  });
  $('chapter-editor').addEventListener('submit',event=>{
    event.preventDefault();
    if(!chapter||busy||!dirty()||!edit.value.trim())return;
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
  clearChapter();syncNav();
  return {activate,canLeave,dropUnsaved,reset};
}
