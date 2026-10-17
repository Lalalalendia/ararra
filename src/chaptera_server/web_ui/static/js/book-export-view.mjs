/** RE-06D browser export wizard. All API IO uses the in-memory-key controller. */

const issueName={
  missing_source:'Отсутствует исходная глава',
  not_accepted:'Перевод ещё не принят',
  stale_accepted:'Принятый перевод устарел',
  source_beyond_declared_end:'Есть глава после указанного конца',
  unsafe_accepted_html:'Принятый текст содержит неподдерживаемую разметку',
  export_size_or_count_limit:'Превышен ограниченный размер экспорта',
};

export function describeExportPreview(preview){
  if(preview?.ready){
    return 'Готово к экспорту: '+preview.ready_chapter_count+
      ' глав; конец книги — глава '+preview.expected_last_chapter+
      '. Скачивание проверит ревизии повторно.';
  }
  if(!preview)return 'Для проверки введите номер последней главы.';
  const issues=(preview.issues||[]).slice(0,5).map(item=>{
    const name=issueName[item.code]||'Неизвестная ошибка';
    return name+(Number.isInteger(item.chapter_index)?' '+item.chapter_index:'');
  });
  const hidden=preview.issue_count>issues.length
    ? '; ещё '+(preview.issue_count-issues.length)+' проблем':'';
  return 'Экспорт запрещён ('+preview.issue_count+'): '+issues.join('; ')+hidden;
}

export function safeExportFilename(bookId,extension){
  const clean=String(bookId||'book').replace(/[^A-Za-z0-9_-]/g,'_').slice(0,50)||'book';
  return 'readera-'+clean+'.'+extension;
}

export async function verifySha256(buffer,sha){
  if(!/^[a-f0-9]{64}$/.test(sha||''))throw new Error('Нет контрольной суммы выгрузки.');
  if(!globalThis.crypto?.subtle)throw new Error('Проверка файла требует защищённого подключения.');
  const digest=await crypto.subtle.digest('SHA-256',buffer);
  const actual=Array.from(new Uint8Array(digest))
    .map(x=>x.toString(16).padStart(2,'0')).join('');
  if(actual!==sha)throw new Error('Контрольная сумма файла не совпала. Скачивание остановлено.');
}

export function createBookExportView({
  dialog,title,help,last,status,check,txt,html,request,showError,notify,
}){
  let active=null;
  let receipt=null;
  let generation=0;
  let working=false;
  const base=book=>'/v1/books/'+encodeURIComponent(book.book_id)+'/export';

  function invalidate(){
    generation+=1;
    receipt=null;
    txt.disabled=true;html.disabled=true;
  }
  function current(ticket,book){
    return ticket===generation && dialog.open && active===book;
  }
  function open(book){
    if(dialog.open || !book)return;
    if(typeof dialog.showModal!=='function'){
      showError(new Error('Браузер не поддерживает безопасное окно экспорта.'));return;
    }
    active=book;
    last.value='';
    invalidate();
    title.textContent='Экспорт «'+book.title+'»';
    help.textContent='В библиотеке '+book.chapter_count+' глав. Укажите подтверждённый последний номер. '+
      'Полный экспорт поддерживает только непрерывную нумерацию 1..N.';
    status.textContent='Для проверки введите номер последней главы.';
    dialog.showModal();last.focus();
  }
  last.addEventListener('input',()=>{
    invalidate();
    status.textContent='Номер главы изменён. Требуется повторная проверка.';
  });
  dialog.addEventListener('close',()=>{
    invalidate();active=null;
    check.disabled=false;
  });
  check.addEventListener('click',()=>void preview());
  txt.addEventListener('click',()=>void download('txt'));
  html.addEventListener('click',()=>void download('html'));

  async function preview(){
    if(!active||working)return;
    const book=active;
    const number=Number(last.value);
    if(!last.value || !Number.isInteger(number) || number<1 || number>2000){
      status.textContent='Укажите целый номер последней главы от 1 до 2000.';
      return;
    }
    invalidate();
    const ticket=generation;
    working=true;check.disabled=true;
    status.textContent='Проверка всех принятых глав...';
    try{
      const result=await request(base(book)+'/preview?expected_last_chapter='+number);
      if(!current(ticket,book))return;
      status.textContent=describeExportPreview(result);
      if(result.ready && /^[a-f0-9]{64}$/.test(result.manifest_sha256)){
        receipt={number,sha:result.manifest_sha256};
        txt.disabled=false;html.disabled=false;
      }
    }catch(e){
      if(current(ticket,book)){
        status.textContent='Не удалось проверить книгу.';
        showError(e);
      }
    }finally{
      working=false;check.disabled=false;
    }
  }

  async function download(extension){
    if(!active||!receipt||working)return;
    const book=active, {number,sha}=receipt, ticket=generation;
    working=true;txt.disabled=true;html.disabled=true;
    status.textContent='Скачивание и проверка SHA-256...';
    try{
      const path=base(book)+'?expected_last_chapter='+number+
        '&format='+extension+'&expected_manifest_sha256='+sha;
      const response=await request(path,{responseType:'raw'});
      const data=await response.arrayBuffer();
      await verifySha256(data,response.headers.get('X-ReadEra-Content-SHA256'));
      if(!current(ticket,book))return;
      if(response.headers.get('X-ReadEra-Manifest-SHA256')!==sha)
        throw new Error('Ревизии книги изменились. Нужна новая проверка.');
      const blob=new Blob([data],{type:response.headers.get('Content-Type')||'application/octet-stream'});
      const url=URL.createObjectURL(blob);
      const anchor=document.createElement('a');
      anchor.href=url;
      anchor.download=safeExportFilename(book.book_id,extension);
      document.body.append(anchor);
      try{anchor.click();}finally{
        anchor.remove();
        setTimeout(()=>URL.revokeObjectURL(url),30000);
      }
      status.textContent='Файл проверен по SHA-256 и передан браузеру для сохранения.';
      notify('Экспорт '+extension.toUpperCase()+' подготовлен.');
    }catch(e){
      if(current(ticket,book)){
        invalidate();
        status.textContent='Выгрузка не выполнена. Проверьте готовность книги заново.';
        showError(e);
      }
    }finally{
      working=false;
      check.disabled=false;
      if(current(ticket,book) && receipt){
        txt.disabled=false;html.disabled=false;
      }
    }
  }
  return {open};
}
