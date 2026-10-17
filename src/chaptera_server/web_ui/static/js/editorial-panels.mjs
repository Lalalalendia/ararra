/** Separate review metadata and accepted history; never modify literary text.
 * All content from the server or users is inserted with textContent.
 */
import {initChapterQaPanel} from './qa-panel.mjs';

const $=id=>document.getElementById(id);
const node=(tag,value)=>{const el=document.createElement(tag);el.textContent=String(value??'');return el;};

export function initEditorialPanels({request,flash,current,reloadChapter,toText}) {
  const notesRoot=$('editor-notes-list');
  const historyRoot=$('editor-history-list');
  const preview=$('editor-history-preview');
  const noteText=$('editor-note-body');
  let noteSeq=0,historySeq=0;
  const qa=initChapterQaPanel({request,flash,current});

  function path(ctx){
    return '/v1/books/'+encodeURIComponent(ctx.bookId)
      +'/chapters/'+encodeURIComponent(ctx.chapterId);
  }
  function remains(ctx){
    const now=current();
    return now && now.bookId===ctx.bookId&&now.chapterId===ctx.chapterId;
  }
  function blank(){
    noteSeq++;historySeq++;
    qa.blank();
    notesRoot.replaceChildren();
    historyRoot.replaceChildren();
    preview.hidden=true;preview.textContent='';
    noteText.value='';
  }
  async function loadNotes(){
    const ctx=current();
    if(!ctx)return;
    const seq=++noteSeq;
    const response=await request(path(ctx)+'/notes');
    if(seq!==noteSeq||!remains(ctx))return;
    notesRoot.replaceChildren();
    const notes=response.notes||[];
    if(!notes.length){notesRoot.append(node('p','Замечаний пока нет.'));return;}
    for(const note of notes){
      const wrap=document.createElement('article');wrap.className='editor-note';
      const header=node('strong',note.author_id+' · '+(note.status==='open'?'Открыто':'Решено'));
      const body=node('p',note.body_text);
      const meta=node('small','Исх. rev '+note.source_revision+
        ' · Черновик rev '+note.draft_revision+' · Принято rev '+note.accepted_revision);
      const btn=node('button',note.status==='open'?'Закрыть замечание':'Открыть повторно');
      btn.type='button';btn.className='ui-button ui-button--ghost';
      btn.addEventListener('click',async()=>{
        try{
          const status=note.status==='open'?'resolved':'open';
          await request(path(ctx)+'/notes/'+encodeURIComponent(note.id),{
            method:'PATCH',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({status,expected_revision:note.note_revision}),
          });
          if(remains(ctx)){await loadNotes();flash('Статус замечания сохранён.');}
        }catch(e){flash(e.message,true);}
      });
      wrap.append(header,body,meta,btn);notesRoot.append(wrap);
    }
  }
  async function loadHistory(){
    const ctx=current();
    if(!ctx)return;
    const seq=++historySeq;
    const response=await request(path(ctx)+'/accepted-revisions');
    if(seq!==historySeq||!remains(ctx))return;
    historyRoot.replaceChildren();
    const history=response.revisions||[];
    if(!history.length){historyRoot.append(node('p','Принятых редакций ещё нет.'));return;}
    for(const revision of history){
      const row=document.createElement('div');row.className='editor-revision';
      const title=node('strong','Ревизия '+revision.revision+' · '+revision.actor_id);
      const meta=node('span','На исходнике rev '+revision.source_revision+
        ' · '+new Date(revision.created_at*1000).toLocaleString('ru-RU'));
      const view=node('button','Посмотреть');
      view.type='button';view.className='ui-button ui-button--ghost';
      view.addEventListener('click',async()=>{
        try{
          const version=await request(path(ctx)+'/accepted-revisions/'+revision.revision);
          if(!remains(ctx))return;
          preview.hidden=false;preview.textContent=toText(version.translated_html);
          preview.dataset.revision=String(revision.revision);
        }catch(e){flash(e.message,true);}
      });
      const restore=node('button','Восстановить');
      restore.type='button';restore.className='ui-button ui-button--danger';
      const currentRev=ctx.acceptedRevision;
      restore.disabled=revision.revision===currentRev ||
        revision.source_revision!==ctx.sourceRevision;
      restore.title=revision.source_revision!==ctx.sourceRevision
        ? 'История относится к другому исходнику: автоматический откат запрещён.'
        : 'Создать следующую принятую редакцию из этой версии';
      restore.addEventListener('click',async()=>{
        if(!remains(ctx))return;
        if(ctx.dirty()&&!window.confirm('Есть несохранённые правки. Продолжить откат?'))return;
        if(!window.confirm('Создать новую принятую ревизию на основе версии '+
          revision.revision+'? История будет сохранена.'))return;
        try{
          await request(path(ctx)+'/restore-accepted',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({revision:revision.revision,
              expected_current_revision:ctx.acceptedRevision,
              expected_source_revision:ctx.sourceRevision}),
          });
          if(remains(ctx)){await reloadChapter();flash('Откат записан как новая принятая ревизия.');}
        }catch(e){flash(e.message,true);}
      });
      row.append(title,meta,view,restore);historyRoot.append(row);
    }
  }
  $('editor-note-form').addEventListener('submit',async event=>{
    event.preventDefault();
    const ctx=current();
    if(!ctx)return;
    if(ctx.dirty()||ctx.outdated()){
      flash('Сначала сохраните и сверьте черновик с текущими ревизиями, затем добавьте замечание.',true);return;
    }
    const body=noteText.value.trim();
    if(!body)return;
    try{
      await request(path(ctx)+'/notes',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({body_text:body,
          expected_source_revision:ctx.sourceRevision,
          expected_draft_revision:ctx.draftRevision,
          expected_accepted_revision:ctx.acceptedRevision}),
      });
      if(remains(ctx)){noteText.value='';await loadNotes();flash('Замечание сохранено отдельно от главы.');}
    }catch(e){flash(e.message,true);}
  });
  return {blank,load:async()=>{await Promise.all([loadNotes(),loadHistory(),qa.load()]);},loadNotes,loadHistory};
}
