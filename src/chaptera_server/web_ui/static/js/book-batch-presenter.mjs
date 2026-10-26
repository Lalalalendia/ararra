/** RE-06C — chapter selection, exact preview and idempotent batch confirmation.
 * The browser owns only transient UI state. API/SQLite own jobs, snapshots,
 * personal Gemini credentials and batch recovery. Never store keys or batch
 * receipts in browser storage or append untrusted source HTML.
 */
const element=(tag,value,css='')=>{
  const node=document.createElement(tag);
  if(value!==undefined && value!==null)node.textContent=String(value);
  if(css)node.className=css;
  return node;
};
export const chapterOrder=(a,b)=>a.chapter_index-b.chapter_index ||
  String(a.chapter_id).localeCompare(String(b.chapter_id));
export const bookPath=bookId=>'/v1/books/'+encodeURIComponent(bookId);

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

/** An unchecked checkbox cannot silently opt the batch into Drive writes. */
export function withDriveConsent(payload,checked){
  if(typeof checked!=='boolean')throw new Error('Drive consent must be explicit');
  return {...payload,drive_export_requested:checked};
}
/** Google Drive TXT only; untrusted receipt IDs cannot redirect a browser. */
export function driveFileUrl(fileId){
  if(typeof fileId!=='string'||!/^[A-Za-z0-9_-]{12,256}$/.test(fileId))
    return null;
  return 'https://drive.google.com/file/d/'+encodeURIComponent(fileId)+'/view';
}
export function driveStatusLabel(status){
  return ({
    not_requested:'Не запрошено',
    awaiting_translation:'Ожидает перевода',
    awaiting_exporter:'Ожидает выгрузки',
    uploading:'Выгружается',
    exported:'На Google Drive',
    requires_review:'Нужна проверка',
    not_delivered:'Не доставлено',
  })[status]||'Статус неизвестен';
}

export function batchProgressSummary(batch){
  if(!batch || !Number.isInteger(batch.chapter_count))
    return 'Прогресс пока недоступен.';
  const counts=batch.counts||{};
  const total=batch.chapter_count;
  const exported=Number(batch.drive_counts?.exported||0);
  const drive=batch.drive_export_requested
    ? ' · На Google Drive '+exported+'/'+total+'.'
    : ' · Автоотправка на Google Drive не запрошена.';
  return 'Глав '+total+' · готово '+Number(counts.completed||0)+
    ' · в очереди '+Number(counts.queued||0)+
    ' · выполняются '+Number(counts.running||0)+
    ' · ожидают повтора '+Number(counts.retry_wait||0)+
    ' · ошибки '+Number(counts.failed||0)+
    ' · отменены '+Number(counts.cancelled||0)+'.'+drive;
}

const statusLabels={
  queued:'В очереди',running:'В работе',retry_wait:'Ожидание повтора',
  completed:'Переведена (не принята)',failed:'Ошибка',cancelled:'Отменена',
};
export function batchStatusLabel(status){
  return statusLabels[status]||'Неизвестный статус';
}


export function renderBatchChapters(chaptersBody,catalog,selected,checkedSelection){
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

export function renderBatchHistory(historyList,batches,loadProgress){
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

export function renderBatchProgress(batch,{progressPanel,progressTitle,progressSummary,progressBody,progressRefresh}){
    progressPanel.hidden=false;
    progressTitle.textContent='Партия '+batch.batch_id;
    progressSummary.textContent=batchProgressSummary(batch);
    progressRefresh.disabled=false;
    progressBody.replaceChildren();
    for(const chapter of batch.chapters||[]){
      const tr=element('tr');
      const driveCell=element('td',driveStatusLabel(chapter.drive_status||
        (batch.drive_export_requested?'awaiting_translation':'not_requested')));
      const url=chapter.drive_status==='exported'
        ?driveFileUrl(chapter.drive_translation_file_id):null;
      if(url){
        const link=element('a','Открыть перевод на Drive');
        link.href=url;link.target='_blank';link.rel='noopener noreferrer';
        driveCell.append(' ',link);
      }
      tr.append(element('td','Глава '+chapter.chapter_index),
        element('td',chapter.chapter_id),
        element('td',batchStatusLabel(chapter.status)),driveCell);
      progressBody.append(tr);
    }
  }

/** The import receipt is only a selection hint, never authority to enqueue.
 * Re-fetch the current TOC and verify every imported chapter before preview.
 */
export function importedChapterSelection(receipt,catalog) {
  if(!receipt || !Array.isArray(receipt.chapters) ||
      typeof receipt.stage_id!=='string' ||
      !/^[0-9a-f]{8}-[0-9a-f-]{27,40}$/.test(receipt.stage_id) ||
      !Number.isSafeInteger(receipt.imported_count))
    throw new Error('Квитанция импорта недоступна. Откройте книгу из библиотеки.');
  const choices=receipt.chapters.filter(c=>c.action==='create'||c.action==='replace');
  if(!choices.length || choices.length>100 || receipt.imported_count!==choices.length)
    throw new Error('Один запуск поддерживает от 1 до 100 импортированных глав.');
  const catalogIds=new Set(catalog.map(c=>c.chapter_id));
  const ids=new Set();
  for(const c of choices) {
    if(typeof c.chapter_id!=='string'||!c.chapter_id||
        ids.has(c.chapter_id)||!catalogIds.has(c.chapter_id))
      throw new Error('Импортированные главы изменились. Откройте книгу из библиотеки.');
    ids.add(c.chapter_id);
  }
  return ids;
}
