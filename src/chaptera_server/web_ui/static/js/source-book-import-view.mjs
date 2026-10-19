/* Two-stage source discovery: selectable, reorderable TOC before capture.
 * All metadata originates in actor-owned server catalog snapshots. Never
 * render source strings as markup; no prose, keys, or URLs enter localStorage.
 */
import {createSourceJobView} from './source-book-job-view.mjs';
import {createSharedSourceView} from './source-shared-view.mjs';
import {createSourceArchiveView} from './source-archive-view.mjs';
import {
  PAGE_SIZE,createTocPlan,selectedOrdinals,visibleOrdinals,pageOf,
  toggleSelected,selectOrdinals,moveVisible,moveBefore,planSummary,
} from './source-toc-plan.mjs';

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
  const query=$('source-selection-filter');
  const selectedStatus=$('source-selected-summary');
  const saveButton=$('source-selection-save');
  const beginButton=$('source-selection-start');
  const restoreButton=$('source-selection-restore');
  const prevButton=$('source-selection-prev');
  const nextButton=$('source-selection-next');
  const pageInfo=$('source-selection-page');
  let catalog=null,plan=null,saved=null;
  let dirty=true,page=0,busy=false,dragged=null;
  const jobs=createSourceJobView({request,flash,onStage,getCatalog:()=>catalog});
  const shared=createSharedSourceView({request,flash});
  const archive=createSourceArchiveView({request,flash});
  function setBusy(value){
    busy=value;
    for(const button of [discover,collect,saveButton,beginButton,restoreButton,
      prevButton,nextButton,$('source-select-all'),$('source-select-none'),
      $('source-select-visible')]) button.disabled=value;
    for(const input of rows.querySelectorAll('button,input'))input.disabled=value;
  }
  function clearCatalog(){
    catalog=null;plan=null;saved=null;dirty=true;page=0;
    catalogSection.hidden=true;
    summary.textContent='';
    result.textContent='';
    rows.replaceChildren();
  }
  function updatePlan(next){
    plan=next;dirty=true;
    renderEntries();
  }
  function renderEntries(){
    if(!plan)return;
    const view=pageOf(plan,query.value,page,PAGE_SIZE);
    page=view.page;
    rows.replaceChildren();
    const visible=visibleOrdinals(plan,query.value);
    for(const n of view.ordinals){
      const ch=plan.source.get(n);
      const tr=document.createElement('tr');
      tr.draggable=true;
      const checked=document.createElement('td');
      const checkbox=document.createElement('input');
      checkbox.type='checkbox';checkbox.checked=plan.selected.has(n);
      checkbox.setAttribute('aria-label','Собирать запись '+n+': '+ch.title);
      checkbox.disabled=busy;
      checkbox.addEventListener('change',()=>updatePlan(toggleSelected(plan,n,checkbox.checked)));
      checked.append(checkbox);tr.append(checked);
      const order=document.createElement('td');
      const up=document.createElement('button'),down=document.createElement('button');
      for(const [button,arrow,step] of [[up,'↑',-1],[down,'↓',1]]){
        button.type='button';button.className='ui-button ui-button--ghost';
        button.textContent=arrow;button.disabled=busy;
        button.setAttribute('aria-label',(step<0?'Поднять ':'Опустить ')+'запись '+n);
        button.addEventListener('click',()=>updatePlan(moveVisible(plan,n,step,query.value)));
      }
      order.append(up,down);tr.append(order);
      for(const value of [n,ch.title,ch.source_id,ch.entry_type || 'chapter']){
        const td=document.createElement('td');td.textContent=String(value??'');
        tr.append(td);
      }
      if(plan.unresolved.has(n))tr.title='Дополнительная или служебная запись: выберите включить или исключить';
      tr.addEventListener('dragstart',event=>{
        if(busy){event.preventDefault();return;}
        dragged=n;event.dataTransfer.effectAllowed='move';
        event.dataTransfer.setData('text/plain',String(n));
      });
      tr.addEventListener('dragover',event=>{
        if(dragged!==null && dragged!==n && !busy){
          event.preventDefault();event.dataTransfer.dropEffect='move';
        }
      });
      tr.addEventListener('drop',event=>{
        if(dragged===null||busy)return;
        event.preventDefault();
        updatePlan(moveBefore(plan,dragged,n));
        dragged=null;
      });
      tr.addEventListener('dragend',()=>{dragged=null;});
      rows.append(tr);
    }
    const stats=planSummary(plan);
    selectedStatus.textContent='Всего '+stats.total+' · выбрано '+stats.selected+
      ' · исключено '+stats.excluded+' · переставлено '+stats.moved+
      (stats.unresolved?' · требуют решения '+stats.unresolved:'')+
      (dirty?' · изменения не сохранены':' · план сохранён')+
      (stats.first.length?' · начало: '+stats.first.join(', '):'');
    pageInfo.textContent='Страница '+(view.page+1)+' из '+view.pages+
      ' · найдено '+view.total;
    prevButton.disabled=busy||view.page===0;
    nextButton.disabled=busy||view.page+1>=view.pages;
  }
  function renderCatalog(data,restored=null){
    if(!Array.isArray(data.chapters))throw new Error('Оглавление отсутствует');
    catalog=data;
    plan=createTocPlan(data.chapters,restored?.ordered_ordinals||null);
    saved=restored;dirty=!restored;
    page=0;query.value='';
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
    renderEntries();
  }
  function validateSelection(){
    if(!catalog||!plan)throw new Error('Сначала получите оглавление');
    const stats=planSummary(plan);
    if(!stats.selected)throw new Error('Выберите хотя бы одну главу для сбора');
    if(stats.unresolved)throw new Error('Укажите решение для дополнительных и служебных записей');
    if(Date.now()/1000>=catalog.expires_at)
      throw new Error('Оглавление устарело. Получите его повторно');
    return stats;
  }
  async function savePlan(){
    validateSelection();
    if(saved && !dirty)return saved;
    const order=selectedOrdinals(plan);
    const previous=saved?.selection_sha256||null;
    const response=await request('/v1/source-books/catalogs/'+
      encodeURIComponent(catalog.catalog_id)+'/selection',{
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        ordered_ordinals:order,expected_catalog_sha256:catalog.catalog_sha256,
        expected_selection_sha256:previous,
      }),
    });
    saved=response;
    dirty=selectedOrdinals(plan).join(',')!==order.join(',');
    renderEntries();
    return response;
  }
  form.addEventListener('submit',event=>{
    event.preventDefault();
    if(busy)return;
    const source=$('source-site').value;
    const book_url=$('source-book-url').value.trim();
    clearCatalog();
    void (async()=>{
      setBusy(true);
      try{
        const data=await request('/v1/source-books/catalogs',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({source,book_url}),
        });
        renderCatalog(data);
        flash('Оглавление загружено. Выберите главы, измените порядок и сохраните план.');
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);renderEntries();}
    })();
  });
  query.addEventListener('input',()=>{page=0;renderEntries();});
  prevButton.addEventListener('click',()=>{page--;renderEntries();});
  nextButton.addEventListener('click',()=>{page++;renderEntries();});
  $('source-select-all').addEventListener('click',()=>{
    if(!busy && plan)updatePlan(selectOrdinals(plan,plan.original,true));
  });
  $('source-select-none').addEventListener('click',()=>{
    if(!busy && plan)updatePlan(selectOrdinals(plan,plan.original,false));
  });
  $('source-select-visible').addEventListener('click',()=>{
    if(!busy && plan)updatePlan(selectOrdinals(plan,visibleOrdinals(plan,query.value),true));
  });
  saveButton.addEventListener('click',()=>{
    if(busy)return;
    void (async()=>{
      setBusy(true);
      try{await savePlan();flash('Выбранные главы и порядок сохранены на сервере.');}
      catch(err){flash(err.message,true);}
      finally{setBusy(false);renderEntries();}
    })();
  });
  beginButton.addEventListener('click',()=>{
    if(busy)return;
    try{
      const stats=validateSelection();
      const message='Начать фоновый сбор '+stats.selected+' глав из '+stats.total+
        '? Исходная нумерация сохранится, порядок итогового импорта: '+
        selectedOrdinals(plan).slice(0,15).join(', ')+
        (stats.selected>15?' …':'')+'. Действие не запускает перевод.';
      if(!window.confirm(message))return;
    }catch(err){flash(err.message,true);return;}
    void (async()=>{
      setBusy(true);
      try{
        const selected=await savePlan();
        await jobs.createFromSelection(catalog,selected);
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);renderEntries();}
    })();
  });
  restoreButton.addEventListener('click',()=>{
    if(busy)return;
    void (async()=>{
      setBusy(true);
      try{
        const recent=await request('/v1/source-books/selections/recent');
        if(!recent.selection){flash('Нет действующего сохранённого плана.',true);return;}
        const draft=recent.selection;
        const data=await request('/v1/source-books/catalogs/'+
          encodeURIComponent(draft.catalog_id));
        if(data.catalog_sha256!==draft.catalog_sha256)
          throw new Error('Оглавление изменилось. План требует повторной проверки.');
        $('source-site').value=data.source;
        $('source-book-url').value=data.book_url;
        renderCatalog(data,draft);
        flash('Черновик выбора глав восстановлен с сервера.');
      }catch(err){flash(err.message,true);}
      finally{setBusy(false);renderEntries();}
    })();
  });
  // Compatibility path: the previous contiguous-range importer remains available.
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
      setBusy(true);result.textContent='Чтение диапазона и проверка содержимого…';
      try{
        const out=await request('/v1/source-books/catalogs/'+
          encodeURIComponent(catalog.catalog_id)+'/collect',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ordinals,expected_catalog_sha256:catalog.catalog_sha256}),
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
      finally{setBusy(false);renderEntries();}
    })();
  });
  return {
    hide(){root.hidden=true;},
    reset(){root.hidden=false;form.reset();clearCatalog();jobs.reset();shared.reset();archive.reset();},
  };
}
