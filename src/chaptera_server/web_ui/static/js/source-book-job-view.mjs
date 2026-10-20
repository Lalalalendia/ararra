import {createSourceJobTxtView} from './source-job-txt-view.mjs';
import {createSourceJobHistoryView} from './source-job-history-view.mjs';
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
  let sessionGeneration=0;
  let busy=false;
  const history=createSourceJobHistoryView({
    request,flash,onOpen:jobId=>load(jobId),
    onDelete:jobId=>{if(current?.job_id===jobId)reset();},
  });
  const txt=createSourceJobTxtView({
    request,flash,getCurrent:()=>current,isBusy:()=>busy,
    setBusy:value=>setBusy(value),
  });
  function invalidateTxt(){txt.invalidate();}
  function setBusy(value){
    busy=value;
    for(const button of [create,refresh,stage,retry,cancel])button.disabled=value;
    txt.setDisabled(value);
  }
  function render(job){
    const changed=current?.job_id!==job.job_id;
    current=job;id.value=job.job_id;
    invalidateTxt();
    txt.reset();
    if(changed){
      if(job.selection_mode==='ordered'){
        from.value='1';
        to.value=String(Math.min(20,job.total));
      }else{
        from.value=String(job.selected_start);
        to.value=String(Math.min(job.selected_start+19,job.selected_end));
      }
    }
    from.closest('label').querySelector('span').textContent=
      job.selection_mode==='ordered'?'Позиция в плане с':'Сохранить в импорт с главы';
    to.closest('label').querySelector('span').textContent=
      job.selection_mode==='ordered'?'По позицию':'По главу';
    actions.hidden=false;
    const labels={queued:'В очереди',running:'Скачивается',
      complete:'Выбранный набор собран',partial:'Нужна проверка',
      cancelled:'Отменено'};
    const delay=job.next_attempt_at;
    const next=job.status==='queued' && Number.isFinite(delay) && delay>Date.now()/1000
      ?' · пауза сайта до '+new Date(delay*1000).toLocaleString('ru-RU'):'';
    state.textContent=(labels[job.status]||job.status)+
      ' · готово '+job.completed+'/'+job.total+
      ' · ошибок '+job.failed+
      ' · осталось '+job.pending+next+
      ' · ID: '+job.job_id+
      (job.failures?.length?' · '+job.failures.slice(0,8)
        .map(f=>'Глава '+f.ordinal+': '+f.error).join('; '):'');
    retry.hidden=job.status!=='partial';
    cancel.hidden=!['queued','running','partial'].includes(job.status);
    stage.hidden=job.completed===0;
  }
  function reset(){
    sessionGeneration+=1;
    current=null;id.value='';actions.hidden=true;
    invalidateTxt();
    txt.reset();
    state.textContent='Фоновое задание ещё не выбрано.';
  }
  async function load(jobId){
    const ticket=sessionGeneration;
    const job=await request('/v1/source-books/jobs/'+encodeURIComponent(jobId));
    if(ticket!==sessionGeneration)return null;
    render(job);
    return job;
  }
  async function createFromSelection(catalog,selection){
    if(busy)return;
    if(!catalog||!selection?.selection_id||
       selection.catalog_id!==catalog.catalog_id||
       selection.catalog_sha256!==catalog.catalog_sha256)
      throw new Error('Сохранённый план не соответствует оглавлению');
    setBusy(true);
    try{
      const job=await request('/v1/source-books/jobs',{
        method:'POST',
        headers:{'Content-Type':'application/json',
          'Idempotency-Key':'source-plan:'+selection.selection_id+':'+
            selection.selection_sha256.slice(0,24)},
        body:JSON.stringify({
          catalog_id:catalog.catalog_id,
          expected_catalog_sha256:catalog.catalog_sha256,
          selection_id:selection.selection_id,
          expected_selection_sha256:selection.selection_sha256,
        }),
      });
      render(job);void history.refresh();
      flash('Запущен сбор '+job.total+' выбранных глав в сохранённом порядке.');
      return job;
    }finally{setBusy(false);}
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
        void history.refresh();
        flash('Задание сохранено на сервере. Его можно открыть через «Мои сборки».');
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
    const ordered=current.selection_mode==='ordered';
    if(!Number.isSafeInteger(lo)||!Number.isSafeInteger(hi)||
       (ordered ? (lo<1||hi>current.total) :
         (lo<current.selected_start||hi>current.selected_end))||
       hi<lo||hi-lo>=20){
      flash('Выберите не более 20 уже сохранённых глав или позиций.',true);return;
    }
    void (async()=>{
      setBusy(true);
      try{
        const result=await request('/v1/source-books/jobs/'+encodeURIComponent(current.job_id)+'/stage',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(ordered ?
            {start_position:lo,end_position:hi} :
            {start_ordinal:lo,end_ordinal:hi}),
        });
        if(result.status!=='staged'||!result.stage_id)
          throw new Error('Сервер не подтвердил подготовку импорта.');
        await onStage(result.stage_id);
        flash('Готовые исходники переданы в проверяемый импорт. Подтвердите назначения глав.');
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  return {reset,render,createFromSelection};
}
