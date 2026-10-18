import {verifySha256} from './book-export-view.mjs';
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
  const checkTxt=$('source-job-check-txt');
  const downloadTxt=$('source-job-download-txt');
  const txtStatus=$('source-job-txt-status');
  let current=null;
  let sessionGeneration=0;
  let busy=false;
  let txtReceipt=null;
  const history=createSourceJobHistoryView({
    request,flash,onOpen:jobId=>load(jobId),
    onDelete:jobId=>{if(current?.job_id===jobId)reset();},
  });
  function invalidateTxt(){
    txtReceipt=null;
    downloadTxt.disabled=true;
  }
  function setBusy(value){
    busy=value;
    for(const button of [create,refresh,stage,retry,cancel,checkTxt,downloadTxt]){
      button.disabled=value || (button===downloadTxt && !txtReceipt);
    }
  }
  function render(job){
    const changed=current?.job_id!==job.job_id;
    current=job;id.value=job.job_id;
    invalidateTxt();
    txtStatus.textContent='Проверка полного исходника ещё не выполнена.';
    if(changed){
      from.value=String(job.selected_start);
      to.value=String(Math.min(job.selected_start+19,job.selected_end));
    }
    actions.hidden=false;
    const labels={queued:'В очереди',running:'Скачивается',
      complete:'Выбранный диапазон собран',partial:'Нужна проверка',
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
    txtStatus.textContent='Проверка полного исходника ещё не выполнена.';
    state.textContent='Фоновое задание ещё не выбрано.';
  }
  async function load(jobId){
    const ticket=sessionGeneration;
    const job=await request('/v1/source-books/jobs/'+encodeURIComponent(jobId));
    if(ticket!==sessionGeneration)return null;
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
  checkTxt.addEventListener('click',()=>{
    if(!current||busy)return;
    const jobId=current.job_id;
    invalidateTxt();
    void (async()=>{
      setBusy(true);
      txtStatus.textContent='Проверка SHA-256 каждой сохранённой главы…';
      try {
        const report=await request('/v1/source-books/jobs/'+encodeURIComponent(jobId)+'/assembly');
        if(current?.job_id!==jobId)return;
        if(report.ready && /^[a-f0-9]{64}$/.test(report.content_sha256||'')){
          txtReceipt={jobId,manifest:report.manifest_sha256,
            sha:report.content_sha256,bytes:report.content_bytes,
            source:report.source,from:report.start_ordinal,to:report.end_ordinal};
          txtStatus.textContent='Готово: '+report.verified_count+'/'+report.selected_count+
            ' глав, '+report.content_bytes+' байт. Полон только выбранный диапазон каталога.';
        }else{
          const problems=(report.issues||[]).slice(0,8)
            .map(x=>(x.ordinal===null?'Сбор':('Глава '+x.ordinal))+
              ': '+x.code).join('; ');
          txtStatus.textContent='TXT заблокирован: проверено '+report.verified_count+
            '/'+report.selected_count+', пропусков '+report.missing_count+
            ', недоступных '+report.failed_count+', повреждённых '+
            report.damaged_count+'. '+problems;
        }
      }catch(err){txtStatus.textContent='Проверка TXT не завершена.';flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  downloadTxt.addEventListener('click',()=>{
    if(!current||busy||!txtReceipt||current.job_id!==txtReceipt.jobId)return;
    const receipt=txtReceipt;
    void (async()=>{
      setBusy(true);
      txtStatus.textContent='Получение файла и проверка SHA-256 в браузере…';
      try{
        const url='/v1/source-books/jobs/'+encodeURIComponent(receipt.jobId)+
          '/export.txt?expected_manifest_sha256='+receipt.manifest+
          '&expected_content_sha256='+receipt.sha;
        const response=await request(url,{responseType:'raw'});
        if(response.headers.get('X-ReadEra-Manifest-SHA256')!==receipt.manifest ||
          response.headers.get('X-ReadEra-Source-Scope')!=='selected_catalog_range_only')
          throw new Error('Не совпадает подтверждение выбранного диапазона.');
        const data=await response.arrayBuffer();
        if(data.byteLength!==receipt.bytes)
          throw new Error('Полученный файл имеет неправильный размер.');
        await verifySha256(data,receipt.sha);
        if(current?.job_id!==receipt.jobId)return;
        const blob=new Blob([data],{type:'text/plain;charset=utf-8'});
        const blobUrl=URL.createObjectURL(blob);
        const a=document.createElement('a');
        const site=String(receipt.source||'source').replace(/[^a-z0-9_-]/gi,'').slice(0,25)||'source';
        a.download='readera-original-'+site+'-'+receipt.from+'-'+receipt.to+'.txt';
        a.href=blobUrl;document.body.append(a);
        try{a.click();}finally{
          a.remove();setTimeout(()=>URL.revokeObjectURL(blobUrl),30000);
        }
        txtStatus.textContent='TXT проверен по SHA-256 и передан браузеру. Это исходный текст.';
        flash('Исходный TXT собран из всех проверенных глав выбранного диапазона.');
      }catch(err){
        invalidateTxt();
        txtStatus.textContent='Выгрузка отклонена. Проверьте полноту повторно.';
        flash(err.message,true);
      }finally{setBusy(false);}
    })();
  });
  return {reset,render};
}
