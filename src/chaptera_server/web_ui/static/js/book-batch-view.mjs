/** RE-06C — transient browser controller; durable state lives in SQLite.
 * All network requests are provided by the authenticated session client.
 */
import {
  buildBatchPayload,chapterOrder,bookPath,importedChapterSelection,
  renderBatchChapters,
} from './book-batch-presenter.mjs';
import {createBatchReads} from './book-batch-read.mjs';
export {buildBatchPayload,batchProgressSummary,batchStatusLabel}
  from './book-batch-presenter.mjs';

export function createBookBatchView({
  dialog,title,help,selectionStatus,chaptersBody,selectAll,clearSelection,
  transport,maxAttempts,previewButton,review,reviewSummary,ack,submitButton,
  status,historyList,historyRefresh,progressPanel,progressTitle,progressSummary,
  progressBody,progressRefresh,request,showError,notify,
}){
  let active=null;
  let importedReceipt=null;
  let catalog=[];
  let bookDetails=null;
  let selected=new Set();
  let receipt=null;
  let generation=0;
  let selectionRevision=0;
  let working=false;

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
    selectionRevision+=1;
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
    renderBatchChapters(chaptersBody,catalog,selected,checkedSelection);
  }
  const reads=createBatchReads({
    getActive:()=>active,getTicket:()=>generation,activeFor,path,request,
    historyList,progressPanel,progressTitle,progressSummary,progressBody,
    progressRefresh,showError,
  });
  const {loadHistory,loadProgress,renderProgress}=reads;

  function reset(){
    generation+=1;selectionRevision+=1;
    selected=new Set();catalog=[];bookDetails=null;active=null;receipt=null;
    importedReceipt=null;
    reads.reset();
    working=false;
    resetPreview();chaptersBody.replaceChildren();historyList.replaceChildren();
    setWorking(false);
    progressPanel.hidden=true;
    transport.value='html_v0';maxAttempts.value='3';
    selectionStatus.textContent='Загрузка оглавления…';
    status.textContent='Выберите главы и проверьте состав перед отправкой.';
    previewButton.disabled=true;
    if(dialog.open)dialog.close();
  }
  dialog.addEventListener('close',reset);

  async function open(book,fromImport=null){
    if(!book || dialog.open)return;
    if(typeof dialog.showModal!=='function'){
      showError(new Error('Браузер не поддерживает окно подтверждения.'));return;
    }
    reset();
    active=book;
    importedReceipt=fromImport;
    const ticket=generation;
    title.textContent='Перевод глав — '+book.title;
    help.textContent=fromImport
      ? 'Выбраны только главы из этой загрузки. После проверки потребуется явное согласие на платный перевод. Автовыгрузка на Google Drive настраивается отдельно и сейчас не гарантируется.'
      : 'Исходные главы не изменяются. После отправки задания выполняет '+
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
      if(importedReceipt) {
        try{selected=importedChapterSelection(importedReceipt,catalog);}
        catch(error){
          status.textContent=error.message;
          showError(error);
          void loadHistory();
          return;
        }
      }
      renderChapters();refreshSelectionStatus();
      status.textContent=importedReceipt
        ? 'Импортированные главы выбраны. Проверяем состав и бюджет без платных запросов…'
        : 'Выберите главы и нажмите «Проверить партию». '+
          'Отправка произойдёт только после отдельного подтверждения.';
      void loadHistory();
      if(importedReceipt)await preview();
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
  progressRefresh.addEventListener('click',()=>void loadProgress(reads.batchId()));

  function setWorking(state){
    working=state;
    for(const control of [selectAll,clearSelection,transport,maxAttempts])
      control.disabled=state;
    for(const checkbox of chaptersBody.querySelectorAll('input[type="checkbox"]'))
      checkbox.disabled=state;
    if(state){
      previewButton.disabled=true;
      submitButton.disabled=true;
    } else {
      refreshSelectionStatus();
      submitButton.disabled=!receipt || !ack.checked;
    }
  }

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
    const selectionVersion=selectionRevision;
    setWorking(true);
    status.textContent='Проверяем выбранные главы и их версии на сервере…';
    try{
      const result=await request(path()+'/preview',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload),
      });
      if(!activeFor(ticket,book) || selectionVersion!==selectionRevision)return;
      if(!/^[a-f0-9]{64}$/.test(result.manifest_sha256) || result.chapter_count!==payload.chapters.length)
        throw new Error('Сервер вернул некорректное подтверждение партии.');
      if(result.personal_budget_ready===false){
        // No queue operation and no paid call: fail closed before consent.
        status.textContent='У вашего Gemini-проекта нет локального суточного лимита. '+
          'Установите предел RPD в личном кабинете и повторите проверку.';
        return;
      }
      receipt={
        payload,sha:result.manifest_sha256,
        key:importedReceipt ? 'import-mvp:'+importedReceipt.stage_id : crypto.randomUUID(),
      };
      review.hidden=false;
      reviewSummary.textContent='Проверены '+result.chapter_count+
        ' глав. Контрольный SHA-256: '+result.manifest_sha256+
        '. Перевод не запускается до нажатия «Подтвердить отправку».';
      status.textContent='Состав проверен. Подтвердите явное создание заданий.';
      ack.checked=false;
    }catch(error){
      if(activeFor(ticket,book)){status.textContent='Проверка партии не прошла.';showError(error);}
    }finally{
      if(activeFor(ticket,book))setWorking(false);
    }
  }
  async function submit(){
    if(!active||!receipt||!ack.checked||working)return;
    const book=active,ticket=generation,pin=receipt;
    setWorking(true);
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
      selected.clear();renderChapters();
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
      if(activeFor(ticket,book))setWorking(false);
    }
  }
  return {open,openImported:(book,receipt)=>open(book,receipt),reset};
}
