/** Native preview capabilities, not fake successful server responses. */
const supported=new Set(['overview','books','editor','team']);
const message='Rust preview: \u0438\u043c\u043f\u043e\u0440\u0442, QA, \u043a\u0432\u043e\u0442\u044b, Gemini \u0438 \u043b\u0438\u0447\u043d\u044b\u0439 \u043a\u0430\u0431\u0438\u043d\u0435\u0442 \u0435\u0449\u0451 \u043d\u0435 \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0435\u043d\u044b. \u042d\u043a\u0441\u043f\u043e\u0440\u0442 \u043f\u043e\u043a\u0430 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0435\u0442 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u043d\u0443\u044e HTML-\u0440\u0430\u0437\u043c\u0435\u0442\u043a\u0443.';
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
