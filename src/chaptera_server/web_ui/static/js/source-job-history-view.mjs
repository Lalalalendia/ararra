/* Actor-scoped server history; job IDs stay in server-backed state, no browser secrets.
 * Deleting a completed personal job retains its verified originals in the
 * shared immutable archive, with no effect on BookLibrary translations.
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
  function render(list, usage) {
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
      const waiting=job.status==='queued' && Number.isFinite(job.next_attempt_at) &&
        job.next_attempt_at>Date.now()/1000
        ?' · пауза до '+new Date(job.next_attempt_at*1000).toLocaleTimeString('ru-RU')
        :'';
      option.textContent=source+' · '+book+' · '+job.selected_start+'–'+job.selected_end+
        ' · '+(stateNames[job.status]||job.status)+waiting+' · '+job.completed+'/'+job.total+
        ' · '+time;
      picker.append(option);
    }
    picker.value=jobs.has(previous)?previous:'';
    const quota=(usage &&
      Number.isFinite(usage.actor_used_bytes) &&
      Number.isFinite(usage.actor_limit_bytes) &&
      Number.isFinite(usage.effective_remaining_bytes))
      ?(' Исходники: '+(usage.actor_used_bytes/1048576).toFixed(1)+' из '+
        (usage.actor_limit_bytes/1048576).toFixed(0)+' МиБ; доступно ещё '+
        (usage.effective_remaining_bytes/1048576).toFixed(1)+' МиБ.'+
        (usage.can_collect===false?' Квота исчерпана: очистите старые сборки.':''))
      :'';
    status.textContent='Доступно своих сборок: '+list.length+
      '. Показываются последние 30 по изменению.'+quota;
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
      render(result.jobs, result.usage);
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
      'Удалить личное задание парсера? Проверенные исходники сохранятся '+
      'в общем архиве команды, а импортированные главы — в библиотеке. '+
      'Если архив заполнен, удаление будет отклонено.'))return;
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
        status.textContent='Удалено точек личного задания: '+response.deleted_checkpoints+
          '; освобождено из квоты модератора: '+response.freed_source_bytes+
          ' байт; сохранено в общем архиве: '+(response.archived_verified_chapters??0)+'.';
        flash('Задание удалено; проверенные тексты сохранены в общем архиве.');
      }catch(err){flash(err.message,true);}
      finally{
        busy=false;controls();
        await refresh();
      }
    })();
  });
  return {refresh,clear};
}
