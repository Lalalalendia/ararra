/* Bounded TOC page presentation; never trust site HTML as markup. */
import {PAGE_SIZE,pageOf,moveVisible,moveBefore,planSummary,toggleSelected}
  from './source-toc-plan.mjs';

export function renderTocRows({
  plan,search,page,busy,dirty,rows,selectedStatus,pageInfo,
  prevButton,nextButton,onUpdate,
}){
  const view=pageOf(plan,search,page,PAGE_SIZE);
  rows.replaceChildren();
  let dragged=null;
    for(const n of view.ordinals){
      const ch=plan.source.get(n);
      const tr=document.createElement('tr');
      tr.draggable=true;
      const checked=document.createElement('td');
      const checkbox=document.createElement('input');
      checkbox.type='checkbox';checkbox.checked=plan.selected.has(n);
      checkbox.setAttribute('aria-label','Собирать запись '+n+': '+ch.title);
      checkbox.disabled=busy;
      checkbox.addEventListener('change',()=>onUpdate(toggleSelected(plan,n,checkbox.checked)));
      checked.append(checkbox);tr.append(checked);
      const order=document.createElement('td');
      const up=document.createElement('button'),down=document.createElement('button');
      for(const [button,arrow,step] of [[up,'↑',-1],[down,'↓',1]]){
        button.type='button';button.className='ui-button ui-button--ghost';
        button.textContent=arrow;button.disabled=busy;
        button.setAttribute('aria-label',(step<0?'Поднять ':'Опустить ')+'запись '+n);
        button.addEventListener('click',()=>onUpdate(moveVisible(plan,n,step,search)));
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
        onUpdate(moveBefore(plan,dragged,n));
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
    return view.page;
}
