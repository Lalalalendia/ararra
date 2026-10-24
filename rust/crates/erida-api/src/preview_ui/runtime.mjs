/** Native preview capabilities, not fake successful server responses. */
const supported=new Set(['overview','books','editor','team','import']);
const message='Rust preview: только проверенный TXT без HTML-сущностей, <br/>, EPUB/DOCX и загрузки сайтов. Gemini, QA и очереди переводов здесь недоступны.';
const unavailable=target=>{
  const link=target?.closest?.('[data-page-link]');
  if(link&&!supported.has(link.dataset.pageLink))return link;
  const button=target?.closest?.('button[aria-label]');
  if(button?.getAttribute('aria-label')?.startsWith('\u041f\u0430\u043a\u0435\u0442\u043d\u044b\u0439 \u043f\u0435\u0440\u0435\u0432\u043e\u0434 \u043a\u043d\u0438\u0433\u0438 '))return button;
  return null;
};
if(document.body.dataset.runtime==='rust-preview'){
  const banner=document.createElement('p');
  banner.id='rust-preview-warning';banner.className='ui-alert';
  banner.setAttribute('role','note');banner.textContent=message;
  document.getElementById('authenticated').prepend(banner);
  const mark=()=>document.querySelectorAll('[data-page-link],button[aria-label]').forEach(node=>{
    if(!unavailable(node))return;
    node.setAttribute('aria-disabled','true');node.title=message;
    if(node.tagName==='BUTTON')node.disabled=true;
    else node.tabIndex=-1;
  });
  mark();new MutationObserver(mark).observe(document.getElementById('authenticated'),{childList:true,subtree:true});
  document.addEventListener('click',event=>{
    if(!unavailable(event.target))return;
    event.preventDefault();event.stopImmediatePropagation();
    banner.scrollIntoView({block:'nearest'});
  },true);
}

if(document.body.dataset.runtime==='rust-preview'){
  const source=document.getElementById('source-import');
  if(source){
    source.hidden=true;source.style.display='none';source.setAttribute('aria-hidden','true');
    source.querySelectorAll('input,button,select,textarea').forEach(el=>el.disabled=true);
  }
  const file=document.getElementById('chapters_file');
  if(file)file.accept='.txt';
  const upload=document.getElementById('import-upload');
  if(upload){
    const note=document.createElement('p');
    note.id='rust-txt-import-boundary';
    note.className='ui-alert';
    note.textContent='Экспериментальный Rust TXT: UTF-8, пронумерованные главы, обычные длинные абзацы. Неизвестные HTML-форматы и служебные строки не сохраняются.';
    upload.prepend(note);
  }
}
