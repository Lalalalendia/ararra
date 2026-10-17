/* Actor-scoped server history; job IDs stay in server-backed state, no browser secrets.
 * Deleting a completed source job frees its crawler checkpoints only; it
 * cannot delete an imported book, stage, or accepted translation.
 */
export function createSourceJobHistoryView({request,flash,onOpen,onDelete}) {
  const $=id=>document.getElementById(id);
  const refreshButton=$('source-job-history-refresh');
  const picker=$('source-job-history-list');
  const openButton=$('source-job-history-open');
  const deleteButton=$('source-job-history-delete');
  const status=$('source-job-history-status');
  let jobs=new Map();
  let busy=false;
  let generation=0;
  const finished=new Set(['complete','partial','cancelled']);
  const stateNames={queued:'в очереди',running:'в работе',complete:'собрано',
    partial:'есть ошибки',cancelled:'отменено'};

  function selected() {
    return jobs.get(picker.value)||null;
  }
  function controls() {
    const job=selected();
    refreshButton.disabled=busy;
    picker.disabled=busy||jobs.size===0;
    openButton.disabled=busy||!job;
    deleteButton.disabled=busy||!job||!finished.has(job.status);
  }
  function render(list) {
    const previous=picker.value;
    jobs=new Map(list.map(job=>[job.job_id,job]));
    picker.replaceChildren();
    const placeholder=document.createElement('option');
    placeholder.value='';placeholder.textContent='Выберите книгу / диапазон';
    picker.append(placeholder);
    for(const job of list) {
      const option=document.createElement('option');
      option.value=job.job_id;
      const time=Number.isFinite(job.updated_at)
        ?new Date(job.updated_at*1000).toLocaleString('ru-RU'):'без даты';
      const source=String(job.source||'источник');
      let book='книга';
      try {
        const path=new URL(job.book_url).pathname.split('/').filter(Boolean);
        book=decodeURIComponent(path.at(-1)||'книга').replace(/[-_]+/g,' ').slice(0,60);
      }catch{/* Only server-verified public book URLs are expected. */}
      option.title=String(job.book_url||'');
      option.textContent=source+' · '+book+' · '+job.selected_start+'–'+job.selected_end+
        ' · '+(stateNames[job.status]||job.status)+' · '+job.completed+'/'+job.total+
        ' · '+time;
      picker.append(option);
    }
    picker.value=jobs.has(previous)?previous:'';
    status.textContent='Доступно своих сборок: '+list.length+
      '. Показываются последние 30 по изменению.';
    controls();
  }

  function clear() {
    generation+=1;
    jobs.clear();
    picker.replaceChildren();
    const empty=document.createElement('option');
    empty.value='';empty.textContent='Получите список заданий';
    picker.append(empty);
    status.textContent='Список доступен после входа в Эриду.';
    controls();
  }

  async function refresh() {
    if(busy)return;
    const ticket=++generation;
    busy=true;controls();
    try {
      const result=await request('/v1/source-books/jobs?limit=30');
      if(ticket!==generation)return;
      if(!Array.isArray(result?.jobs))
        throw new Error('Сервер не вернул список заданий.');
      render(result.jobs);
    }catch(err){
      if(ticket!==generation)return;
      clear();
      status.textContent='Не удалось загрузить мои сборки.';
      flash(err.message,true);
    }finally{
      busy=false;controls();
    }
  }
  refreshButton.addEventListener('click',()=>void refresh());
  picker.addEventListener('change',controls);
  openButton.addEventListener('click',()=>{
    if(busy)return;
    const job=selected();if(!job)return;
    void (async()=>{
      const ticket=generation;
      busy=true;controls();
      try {
        await onOpen(job.job_id);
        if(ticket===generation)
          flash('Загрузка открыта. Продолжайте с сохранённого прогресса.');
      }catch(err){flash(err.message,true);}
      finally{busy=false;controls();}
    })();
  });
  deleteButton.addEventListener('click',()=>{
    if(busy)return;
    const job=selected();
    if(!job||!finished.has(job.status))return;
    if(!window.confirm(
      'Безвозвратно удалить сохранённые исходники выбранного задания? '+
      'Уже импортированные главы в библиотеке Эриды останутся.'))return;
    void (async()=>{
      busy=true;controls();
      try {
        const response=await request(
          '/v1/source-books/jobs/'+encodeURIComponent(job.job_id),
          {method:'DELETE'},
        );
        if(response.deleted!==true||response.job_id!==job.job_id)
          throw new Error('Сервер не подтвердил очистку выбранного задания.');
        onDelete(job.job_id);
        status.textContent='Удалено контрольных точек: '+response.deleted_checkpoints+
          '; байт исходников: '+response.freed_source_bytes+'.';
        flash('Сохранённые исходники задания удалены; библиотека не изменена.');
      }catch(err){flash(err.message,true);}
      finally{
        busy=false;controls();
        await refresh();
      }
    })();
  });
  return {refresh,clear};
}
