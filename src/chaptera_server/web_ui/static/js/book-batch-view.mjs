/** RE-06C — chapter selection, exact preview and idempotent batch confirmation.
 * The browser owns only transient UI state. API/SQLite own jobs, snapshots,
 * personal Gemini credentials and batch recovery. Never store keys or batch
 * receipts in localStorage/sessionStorage or append untrusted source HTML.
 */
const element=(tag,value,css='')=>{
  const node=document.createElement(tag);
  if(value!==undefined && value!==null)node.textContent=String(value);
  if(css)node.className=css;
  return node;
};
const chapterOrder=(a,b)=>a.chapter_index-b.chapter_index ||
  String(a.chapter_id).localeCompare(String(b.chapter_id));
const bookPath=bookId=>'/v1/books/'+encodeURIComponent(bookId);

export function buildBatchPayload(book,chapters,selected,{transport='html_v0',maxAttempts=3}={}){
  if(!book || !Number.isInteger(book.glossary_revision) || book.glossary_revision<0)
    throw new Error('Нельзя подтвердить версию словаря книги.');
  const chosen=chapters.filter(c=>selected.has(c.chapter_id)).sort(chapterOrder);
  if(!chosen.length || chosen.length>100)
    throw new Error('Выберите от 1 до 100 глав.');
  if(!['html_v0','atoms_v1'].includes(transport))
    throw new Error('Неподдерживаемый протокол перевода.');
  if(!Number.isInteger(maxAttempts) || maxAttempts<1 || maxAttempts>10)
    throw new Error('Число попыток должно быть от 1 до 10.');
  const ids=new Set();
  for(const chapter of chosen){
    if(!chapter.chapter_id || ids.has(chapter.chapter_id) ||
        !Number.isInteger(chapter.source_revision) || chapter.source_revision<1)
      throw new Error('Оглавление содержит недопустимые ID или ревизии.');
    ids.add(chapter.chapter_id);
  }
  return {
    expected_glossary_revision:book.glossary_revision,
    chapters:chosen.map(c=>({
      chapter_id:c.chapter_id,expected_source_revision:c.source_revision,
    })),
    source_language:'auto',target_language:'Russian',
    style_instructions:'',max_attempts:maxAttempts,transport,
  };
}

export function batchProgressSummary(batch){
  if(!batch || !Number.isInteger(batch.chapter_count))
    return 'Прогресс пока недоступен.';
  const counts=batch.counts||{};
  const total=batch.chapter_count;
  return 'Глав '+total+' · готово '+Number(counts.completed||0)+
    ' · в очереди '+Number(counts.queued||0)+
    ' · выполняются '+Number(counts.running||0)+
    ' · ожидают повтора '+Number(counts.retry_wait||0)+
    ' · ошибки '+Number(counts.failed||0)+
    ' · отменены '+Number(counts.cancelled||0)+'.';
}

const statusLabels={
  queued:'В очереди',running:'В работе',retry_wait:'Ожидание повтора',
  completed:'Переведена (не принята)',failed:'Ошибка',cancelled:'Отменена',
};
export function batchStatusLabel(status){
  return statusLabels[status]||'Неизвестный статус';
}

export function createBookBatchView({
  dialog,title,help,selectionStatus,chaptersBody,selectAll,clearSelection,
  transport,maxAttempts,previewButton,review,reviewSummary,ack,submitButton,
  status,historyList,historyRefresh,progressPanel,progressTitle,progressSummary,
  progressBody,progressRefresh,request,showError,notify,
}){
  let active=null;
  let catalog=[];
  let bookDetails=null;
  let selected=new Set();
  let receipt=null;
  let currentBatchId='';
  let generation=0;
  let working=false;
  let reading=false;

  const activeFor=(ticket,book)=>ticket===generation && dialog.open && active===book;
  const path=()=>bookPath(active.book_id)+'/translation-batches';
  function resetPreview(){
    receipt=null;
    ack.checked=false;
    review.hidden=true;
    submitButton.disabled=true;
  }
  function refreshSelectionStatus(){
    selectionStatus.textContent='Выбрано '+selected.size+' из '+catalog.length+
      ' глав. В одну партию допускается не больше 100.';
    previewButton.disabled=working || !selected.size || selected.size>100 || !bookDetails;
  }
  function changed(){
    resetPreview();
    status.textContent='Выбор или параметры изменились. Нужна новая проверка.';
    refreshSelectionStatus();
  }
  function checkedSelection(id,checked){
    if(checked && !selected.has(id) && selected.size===100){
      status.textContent='Предел — 100 глав на одну партию.';
      return false;
    }
    if(checked)selected.add(id);else selected.delete(id);
    changed();
    return true;
  }
  function renderChapters(){
    chaptersBody.replaceChildren();
    if(!catalog.length){
      const row=element('tr');
      const col=element('td','Нет исходных глав для выбора.');col.colSpan=3;
      row.append(col);chaptersBody.append(row);return;
    }
    const fragment=document.createDocumentFragment();
    for(const chapter of catalog){
      const row=element('tr');
      const column=element('td');
      const label=element('label',null,'batch-chapter-check');
      const box=element('input');
      box.type='checkbox';
      box.dataset.chapterId=chapter.chapter_id;
      box.checked=selected.has(chapter.chapter_id);
      box.setAttribute('aria-label','Выбрать главу '+chapter.chapter_index);
      box.addEventListener('change',()=>{
        if(!checkedSelection(chapter.chapter_id,box.checked))
          box.checked=false;
      });
      label.append(box,element('span','Глава '+chapter.chapter_index));
      column.append(label);
      row.append(column);
      row.append(element('td',chapter.chapter_id));
      row.append(element('td',String(chapter.source_revision)));
      fragment.append(row);
    }
    chaptersBody.append(fragment);
  }
  function renderHistory(batches){
    historyList.replaceChildren();
    if(!batches.length){
      historyList.append(element('p','Сохранённых партий пока нет.','muted'));
      return;
    }
    for(const batch of batches){
      const line=element('div',null,'batch-history-row');
      const detail=element('span','Глав '+batch.chapter_count+' · '+
        batchProgressSummary(batch));
      const openButton=element('button','Открыть '+batch.batch_id.slice(0,8),
        'ui-button ui-button--ghost');
      openButton.type='button';
      openButton.addEventListener('click',()=>void loadProgress(batch.batch_id));
      line.append(detail,openButton);
      historyList.append(line);
    }
  }
  function renderProgress(batch){
    currentBatchId=batch.batch_id;
    progressPanel.hidden=false;
    progressTitle.textContent='Партия '+batch.batch_id;
    progressSummary.textContent=batchProgressSummary(batch);
    progressRefresh.disabled=false;
    progressBody.replaceChildren();
    for(const chapter of batch.chapters||[]){
      const tr=element('tr');
      tr.append(element('td','Глава '+chapter.chapter_index),
        element('td',chapter.chapter_id),
        element('td',batchStatusLabel(chapter.status)));
      progressBody.append(tr);
    }
  }
  async function loadHistory(){
    if(!active || reading)return;
    const book=active,ticket=generation;
    reading=true;
    try{
      const result=await request(path()+'?limit=20');
      if(activeFor(ticket,book))renderHistory(result.batches||[]);
    }catch(error){
      if(activeFor(ticket,book))showError(error);
    }finally{reading=false;}
  }
  async function loadProgress(batchId){
    if(!active || !batchId)return;
    const book=active,ticket=generation;
    progressSummary.textContent='Загрузка сохранённого прогресса…';
    try{
      const result=await request(path()+'/'+encodeURIComponent(batchId));
      if(activeFor(ticket,book))renderProgress(result);
    }catch(error){if(activeFor(ticket,book))showError(error);}
  }

  function reset(){
    generation+=1;
    selected=new Set();catalog=[];bookDetails=null;active=null;receipt=null;
    currentBatchId='';
    working=false;reading=false;
    resetPreview();chaptersBody.replaceChildren();historyList.replaceChildren();
    progressPanel.hidden=true;
    transport.value='html_v0';maxAttempts.value='3';
    selectionStatus.textContent='Загрузка оглавления…';
    status.textContent='Выберите главы и проверьте состав перед отправкой.';
    previewButton.disabled=true;
    if(dialog.open)dialog.close();
  }
  dialog.addEventListener('close',reset);

  async function open(book){
    if(!book || dialog.open)return;
    if(typeof dialog.showModal!=='function'){
      showError(new Error('Браузер не поддерживает окно подтверждения.'));return;
    }
    reset();
    active=book;
    const ticket=generation;
    title.textContent='Перевод глав — '+book.title;
    help.textContent='Исходные главы не изменяются. После отправки задания выполняет '+
      'существующая очередь с личным Gemini-ключом модератора.';
    dialog.showModal();
    try{
      const base=bookPath(book.book_id);
      const [detail,toc]=await Promise.all([
        request(base),request(base+'/chapters'),
      ]);
      if(!activeFor(ticket,book))return;
      bookDetails=detail;
      catalog=(toc.chapters||[]).slice().sort(chapterOrder);
      renderChapters();refreshSelectionStatus();
      status.textContent='Выберите главы и нажмите «Проверить партию». '+
        'Отправка произойдёт только после отдельного подтверждения.';
      void loadHistory();
    }catch(error){if(activeFor(ticket,book)){status.textContent='Оглавление недоступно.';showError(error);}}
  }

  selectAll.addEventListener('click',()=>{
    selected=new Set(catalog.slice(0,100).map(c=>c.chapter_id));
    changed();renderChapters();
    if(catalog.length>100)status.textContent='Выбраны первые 100 глав. Остальные отправляются отдельной партией.';
  });
  clearSelection.addEventListener('click',()=>{
    selected.clear();changed();renderChapters();
  });
  transport.addEventListener('change',changed);
  maxAttempts.addEventListener('input',changed);
  ack.addEventListener('change',()=>{
    submitButton.disabled=working || !receipt || !ack.checked;
  });
  previewButton.addEventListener('click',()=>void preview());
  submitButton.addEventListener('click',()=>void submit());
  historyRefresh.addEventListener('click',()=>void loadHistory());
  progressRefresh.addEventListener('click',()=>void loadProgress(currentBatchId));

  async function preview(){
    if(!active||!bookDetails||working)return;
    const book=active,ticket=generation;
    resetPreview();
    let payload;
    try{
      payload=buildBatchPayload(bookDetails,catalog,selected,{
        transport:transport.value,maxAttempts:Number(maxAttempts.value),
      });
    }catch(error){status.textContent=error.message;return;}
    working=true;previewButton.disabled=true;
    status.textContent='Проверяем выбранные главы и их версии на сервере…';
    try{
      const result=await request(path()+'/preview',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload),
      });
      if(!activeFor(ticket,book))return;
      if(!/^[a-f0-9]{64}$/.test(result.manifest_sha256) || result.chapter_count!==payload.chapters.length)
        throw new Error('Сервер вернул некорректное подтверждение партии.');
      receipt={payload,sha:result.manifest_sha256,key:crypto.randomUUID()};
      review.hidden=false;
      reviewSummary.textContent='Проверены '+result.chapter_count+
        ' глав. Контрольный SHA-256: '+result.manifest_sha256+
        '. Перевод не запускается до нажатия «Подтвердить отправку».';
      status.textContent='Состав проверен. Подтвердите явное создание заданий.';
      ack.checked=false;
    }catch(error){
      if(activeFor(ticket,book)){status.textContent='Проверка партии не прошла.';showError(error);}
    }finally{
      working=false;if(activeFor(ticket,book))refreshSelectionStatus();
    }
  }
  async function submit(){
    if(!active||!receipt||!ack.checked||working)return;
    const book=active,ticket=generation,pin=receipt;
    working=true;submitButton.disabled=true;previewButton.disabled=true;
    status.textContent='Подтверждаем создание всей партии одним действием…';
    try{
      const result=await request(path(),{
        method:'POST',headers:{
          'Content-Type':'application/json','Idempotency-Key':pin.key,
        },
        body:JSON.stringify({...pin.payload,expected_manifest_sha256:pin.sha}),
      });
      if(!activeFor(ticket,book))return;
      receipt=null;
      ack.checked=false;review.hidden=true;
      renderProgress(result);
      status.textContent='Сервер сохранил партию. Задания не означают принятые переводы.';
      notify('Партия '+result.chapter_count+' глав сохранена в очереди.');
      void loadHistory();
    }catch(error){
      if(activeFor(ticket,book)){
        // In case the server committed but its response was lost, keep the
        // original key/payload for a safe retry. Server history is the other
        // recovery path. Never blindly issue a different second batch.
        status.textContent='Ответ отправки не подтверждён. Проверьте список партий '+
          'или повторите с тем же подтверждением; дубли исключает сервер.';
        showError(error);void loadHistory();
      }
    }finally{
      working=false;
      if(activeFor(ticket,book)){
        refreshSelectionStatus();
        submitButton.disabled=!receipt || !ack.checked;
      }
    }
  }
  return {open,reset};
}
