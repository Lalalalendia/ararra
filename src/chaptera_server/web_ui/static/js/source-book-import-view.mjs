/* Site-specific public book selection. Uses ReadEra's authenticated request
 * boundary; the final write is still the existing ImportSessions confirmation.
 */
import {createSourceJobView} from './source-book-job-view.mjs';

export function createSourceBookImportView({request,flash,onStage}) {
  const $=id=>document.getElementById(id);
  const root=$('source-import');
  const form=$('source-book-form');
  const catalogSection=$('source-catalog');
  const summary=$('source-summary');
  const result=$('source-result');
  const rows=$('source-rows');
  const discover=$('source-discover');
  const collect=$('source-collect');
  const start=$('source-start');
  const end=$('source-end');
  let catalog=null;
  let busy=false;
  const jobs=createSourceJobView({request,flash,onStage,getCatalog:()=>catalog});
  const setBusy=value=>{
    busy=value;
    discover.disabled=value;
    collect.disabled=value;
  };
  function clearCatalog(){
    catalog=null;
    catalogSection.hidden=true;
    summary.textContent='';
    result.textContent='';
    rows.replaceChildren();
  }
  function renderCatalog(data){
    catalog=data;
    rows.replaceChildren();
    for(const ch of data.chapters.slice(0,80)){
      const tr=document.createElement('tr');
      for(const value of [ch.ordinal,ch.title,ch.source_id]){
        const td=document.createElement('td');
        td.textContent=String(value??'');
        tr.append(td);
      }
      rows.append(tr);
    }
    start.value=String(data.first_ordinal);
    end.value=String(Math.min(data.first_ordinal+9,data.last_ordinal));
    start.max=String(data.last_ordinal);
    end.max=String(data.last_ordinal);
    catalogSection.hidden=false;
    const gaps=data.catalog_gaps||[];
    const coverage=data.coverage_state==='declared_count_matched'
      ?'Счётчик сайта: '+data.declared_chapter_count+
        ', все заявленные главы обнаружены в публичном HTML.'
      :'Сайт не подтвердил общее число глав: полнота книги не установлена.';
    summary.textContent='Найдено '+data.chapter_count+' глав. '+
      'Просмотрено страниц оглавления: '+(data.toc_page_count||1)+'. '+
      'Диапазон: '+data.first_ordinal+'–'+data.last_ordinal+'. '+
      coverage+' '+
      'Номера для Royal Road соответствуют позиции в оглавлении, а не заголовку автора.'+
      (gaps.length?' Недостающие номера: '+gaps.slice(0,15).join(', '):'');
  }
  form.addEventListener('submit',event=>{
    event.preventDefault();
    if(busy)return;
    const source=$('source-site').value;
    const book_url=$('source-book-url').value.trim();
    clearCatalog();
    void (async()=>{
      setBusy(true);
      try {
        const data=await request('/v1/source-books/catalogs',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({source,book_url}),
        });
        renderCatalog(data);
        flash('Оглавление загружено. Проверьте главы перед скачиванием.');
      } catch(err){flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  collect.addEventListener('click',()=>{
    if(!catalog||busy)return;
    const from=Number(start.value),to=Number(end.value);
    if(!Number.isSafeInteger(from)||!Number.isSafeInteger(to)||
       from<1||to<from||to-from>=20){
      result.textContent='Выберите непрерывный диапазон не более 20 глав.';
      return;
    }
    const available=new Set(catalog.chapters.map(x=>x.ordinal));
    const ordinals=[];
    for(let n=from;n<=to;n++){
      if(!available.has(n)){
        result.textContent='Глава '+n+' отсутствует в оглавлении. Уточните диапазон.';
        return;
      }
      ordinals.push(n);
    }
    void (async()=>{
      setBusy(true);result.textContent='Чтение выбранных глав и проверка содержимого…';
      try {
        const out=await request('/v1/source-books/catalogs/'+encodeURIComponent(catalog.catalog_id)+'/collect',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            ordinals,expected_catalog_sha256:catalog.catalog_sha256,
          }),
        });
        if(out.status!=='staged'||!out.stage_id){
          const errors=(out.failures||[]).slice(0,10)
            .map(x=>'Глава '+x.ordinal+': '+x.error);
          result.textContent='Не все главы получены ('+
            (out.collected_count||0)+'/'+ordinals.length+
            '). Импорт не создан. '+errors.join(' · ');
          return;
        }
        await onStage(out.stage_id);
        flash('Получены '+out.collected_count+' глав. Подтвердите назначения в обычном импорте.');
      }catch(err){result.textContent='Сбор не завершён: '+err.message;flash(err.message,true);}
      finally{setBusy(false);}
    })();
  });
  return {
    hide(){root.hidden=true;},
    reset(){root.hidden=false;form.reset();clearCatalog();jobs.reset();},
  };
}
