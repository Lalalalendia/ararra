/* Durable source collection UI, isolated from the 20-chapter quick import.
 * No credentials, job content or tokens are stored in the browser.
 */
export function createSourceJobView({request,flash,onStage,getCatalog}) {
  const $=id=>document.getElementById(id);
  const form=$('source-job-resume-form');
  const start=$('source-start'),end=$('source-end');
  const id=$('source-job-id');
  const state=$('source-job-status');
  const actions=$('source-job-actions');
  const from=$('source-job-from'),to=$('source-job-to');
  const create=$('source-job-create');
  const refresh=$('source-job-refresh');
  const stage=$('source-job-stage');
  const retry=$('source-job-retry');
  const cancel=$('source-job-cancel');
  let current=null;
  let busy=false;
  function setBusy(value){
    busy=value;
    for(const button of [create,refresh,stage,retry,cancel]){
      button.disabled=value;
    }
  }
  function render(job){
    const changed=current?.job_id!==job.job_id;
    current=job;id.value=job.job_id;
    if(changed){
      from.value=String(job.selected_start);
      to.value=String(Math.min(job.selected_start+19,job.selected_end));
    }
    actions.hidden=false;
    const labels={queued:'В очереди',running:'Скачивается',
      complete:'Выбранный диапазон собран',partial:'Нужна проверка',
      cancelled:'Отменено'};
    state.textContent=(labels[job.status]||job.status)+
      ' · готово '+job.completed+'/'+job.total+
      ' · ошибок '+job.failed+
      ' · осталось '+job.pending+
      ' · ID: '+job.job_id+
      (job.failures?.length?' · '+job.failures.slice(0,8)
        .map(f=>'Глава '+f.ordinal+': '+f.error).join('; '):'');
    retry.hidden=job.status!=='partial';
    cancel.hidden=!['queued','running','partial'].includes(job.status);
    stage.hidden=job.completed===0;
  }
  function reset(){
    current=null;id.value='';actions.hidden=true;
    state.textContent='Фоновое задание ещё не выбрано.';
  }
  async function load(jobId){
    const job=await request('/v1/source-books/jobs/'+encodeURIComponent(jobId));
    render(job);
    return job;
  }
  create.addEventListener('click',()=>{
    if(busy)return;
    const catalog=getCatalog();
    if(!catalog){flash('Сначала получите оглавление книги.',true);return;}
    const lo=Number(start.value),hi=Number(end.value);
    if(!Number.isSafeInteger(lo)||!Number.isSafeInteger(hi)||
       lo<1||hi<lo||hi-lo>=2000){
      flash('Выберите диапазон от 1 до 2000 глав.',true);return;
    }
    const available=new Set(catalog.chapters.map(ch=>ch.ordinal));
    for(let number=lo;number<=hi;number++){
      if(!available.has(number)){
        flash('Глава '+number+' отсутствует в проверенном оглавлении.',true);
        return;
      }
    }
    void (async()=>{
      setBusy(true);
      try {
        const job=await request('/v1/source-books/jobs',{
          method:'POST',
          headers:{'Content-Type':'application/json',
            'Idempotency-Key':'source:'+catalog.catalog_id+':'+lo+'-'+hi},
          body:JSON.stringify({
            catalog_id:catalog.catalog_id,
            expected_catalog_sha256:catalog.catalog_sha256,
            start_ordinal:lo,end_ordinal:hi,
          }),
        });
        render(job);
        flash('Задание сохранено. Скопируйте ID; source-worker продолжит сбор на сервере.');
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  form.addEventListener('submit',event=>{
    event.preventDefault();
    if(busy||!id.value.trim())return;
    void (async()=>{
      setBusy(true);
      try{await load(id.value.trim());flash('Состояние сборки обновлено.');}
      catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  retry.addEventListener('click',()=>{
    if(!current||busy||current.status!=='partial')return;
    void (async()=>{
      setBusy(true);
      try{
        render(await request('/v1/source-books/jobs/'+encodeURIComponent(current.job_id)+'/retry',
          {method:'POST'}));
        flash('Неудачные главы снова поставлены в очередь; завершённые сохранены.');
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  cancel.addEventListener('click',()=>{
    if(!current||busy||!['queued','running','partial'].includes(current.status))return;
    if(!window.confirm('Отменить фоновый сбор? Уже полученные исходники не появятся в библиотеке.'))return;
    void (async()=>{
      setBusy(true);
      try{render(await request('/v1/source-books/jobs/'+encodeURIComponent(current.job_id)+'/cancel',
        {method:'POST'}));flash('Сбор отменён.');}
      catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  stage.addEventListener('click',()=>{
    if(!current||busy||current.completed===0)return;
    const lo=Number(from.value),hi=Number(to.value);
    if(!Number.isSafeInteger(lo)||!Number.isSafeInteger(hi)||
       lo<current.selected_start||hi>current.selected_end||hi<lo||hi-lo>=20){
      flash('Выберите не более 20 уже сохранённых глав.',true);return;
    }
    void (async()=>{
      setBusy(true);
      try{
        const result=await request('/v1/source-books/jobs/'+encodeURIComponent(current.job_id)+'/stage',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({start_ordinal:lo,end_ordinal:hi}),
        });
        if(result.status!=='staged'||!result.stage_id)
          throw new Error('Сервер не подтвердил подготовку импорта.');
        await onStage(result.stage_id);
        flash('Готовые исходники переданы в проверяемый импорт. Подтвердите назначения глав.');
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  return {reset,render};
}
