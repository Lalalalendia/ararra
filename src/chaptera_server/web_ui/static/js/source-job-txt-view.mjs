/* Source-job TXT preflight and verified-byte download.
 * Shared authenticated request callback is the only transport.
 */
import {verifySha256} from './book-export-view.mjs';

export function createSourceJobTxtView({request,flash,getCurrent,isBusy,setBusy}){
  const $=id=>document.getElementById(id);
  const checkTxt=$('source-job-check-txt');
  const downloadTxt=$('source-job-download-txt');
  const txtStatus=$('source-job-txt-status');
  let txtReceipt=null;
  function invalidate(){
    txtReceipt=null;
    downloadTxt.disabled=true;
  }
  function reset(){
    invalidate();
    txtStatus.textContent='Проверка полного исходника ещё не выполнена.';
  }
  function setDisabled(value){
    checkTxt.disabled=value;
    downloadTxt.disabled=value||!txtReceipt;
  }
  checkTxt.addEventListener('click',()=>{
    if(!getCurrent()||isBusy())return;
    const jobId=getCurrent().job_id;
    invalidate();
    void (async()=>{
      setBusy(true);
      txtStatus.textContent='Проверка SHA-256 каждой сохранённой главы…';
      try {
        const report=await request('/v1/source-books/jobs/'+encodeURIComponent(jobId)+'/assembly');
        if(getCurrent()?.job_id!==jobId)return;
        if(report.ready && /^[a-f0-9]{64}$/.test(report.content_sha256||'')){
          if(!['selected_catalog_range_only','selected_catalog_selection_only'].includes(report.scope))
            throw new Error('Неизвестная область исходников');
          txtReceipt={jobId,manifest:report.manifest_sha256,
            sha:report.content_sha256,bytes:report.content_bytes,scope:report.scope,
            source:report.source,from:report.start_ordinal,to:report.end_ordinal};
          txtStatus.textContent='Готово: '+report.verified_count+'/'+report.selected_count+
            ' глав, '+report.content_bytes+' байт. Полон только подтверждённый набор.';
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
    if(!getCurrent()||isBusy()||!txtReceipt||getCurrent().job_id!==txtReceipt.jobId)return;
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
          response.headers.get('X-ReadEra-Source-Scope')!==receipt.scope)
          throw new Error('Не совпадает подтверждение выбранного диапазона.');
        const data=await response.arrayBuffer();
        if(data.byteLength!==receipt.bytes)
          throw new Error('Полученный файл имеет неправильный размер.');
        await verifySha256(data,receipt.sha);
        if(getCurrent()?.job_id!==receipt.jobId)return;
        const blob=new Blob([data],{type:'text/plain;charset=utf-8'});
        const blobUrl=URL.createObjectURL(blob);
        const a=document.createElement('a');
        const site=String(receipt.source||'source').replace(/[^a-z0-9_-]/gi,'').slice(0,25)||'source';
        a.download='readera-original-'+site+'-'+
          (receipt.scope==='selected_catalog_selection_only'?'selected':
            receipt.from+'-'+receipt.to)+'.txt';
        a.href=blobUrl;document.body.append(a);
        try{a.click();}finally{
          a.remove();setTimeout(()=>URL.revokeObjectURL(blobUrl),30000);
        }
        txtStatus.textContent='TXT проверен по SHA-256 и передан браузеру. Это исходный текст.';
        flash('Исходный TXT собран из всех проверенных выбранных глав.');
      }catch(err){
        invalidate();
        txtStatus.textContent='Выгрузка отклонена. Проверьте полноту повторно.';
        flash(err.message,true);
      }finally{setBusy(false);}
    })();
  });
  return {invalidate,reset,setDisabled};
}
