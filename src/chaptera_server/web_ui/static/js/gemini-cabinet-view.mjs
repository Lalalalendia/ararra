/** ReadEra personal Gemini cabinet. Never persist keys in browser storage. */
export function createGeminiCabinet({request,flash}) {
  const $=id=>document.getElementById(id);
  const form=$('gemini-key-form'),list=$('gemini-keys-list'),status=$('gemini-cabinet-status');
  function clearForm(){
    form.reset();
    form.querySelector('[name="api_key"]').value='';
  }
  function clear(){
    clearForm();
    list.replaceChildren();
    status.textContent='Загрузка настроек…';
  }
  async function refresh(){
    const state=await request('/v1/me/gemini-keys');
    clearForm();
    list.replaceChildren();
    status.textContent=state.configured
      ? 'Ключи зашифрованы на сервере. Для каждого проекта допускается один ключ.'
      : 'Серверное хранилище ключей ещё не настроено. Сохранение недоступно.';
    form.querySelector('button[type="submit"]').disabled=!state.configured;
    if(!state.projects.length){
      const empty=document.createElement('p');
      empty.textContent='У вас пока нет подключённых проектов.';
      list.append(empty);
      return;
    }
    for(const project of state.projects){
      const row=document.createElement('div');
      row.className='gemini-project-row';
      const label=document.createElement('span');
      label.textContent=project.project_id+' · ключ сохранён';
      const remove=document.createElement('button');
      remove.type='button';
      remove.className='ui-button ui-button--ghost';
      remove.textContent='Удалить';
      remove.setAttribute('aria-label','Удалить Gemini-ключ проекта '+project.project_id);
      remove.disabled=!state.configured;
      remove.addEventListener('click',async()=>{
        remove.disabled=true;
        try{
          await request('/v1/me/gemini-keys/'+encodeURIComponent(project.project_id),{
            method:'DELETE',responseType:'raw',
          });
          await refresh();
          flash('Ключ проекта удалён.');
        }catch(error){
          flash(error.message,true);
          remove.disabled=false;
        }
      });
      const budget=document.createElement('form');
      budget.className='gemini-budget-form';
      const field=document.createElement('label');
      field.className='ui-field';
      const caption=document.createElement('span');
      caption.textContent='Суточный локальный предел запросов (RPD)';
      const cap=document.createElement('input');
      cap.className='ui-input';
      cap.type='number';cap.min='1';cap.max='1000000';cap.step='1';
      cap.required=true;
      cap.value=project.daily_request_cap===null?'':String(project.daily_request_cap);
      cap.setAttribute('aria-label','Суточный предел для проекта '+project.project_id);
      field.append(caption,cap);
      const set=document.createElement('button');
      set.type='submit';set.className='ui-button ui-button--ghost';
      set.textContent='Сохранить предел';
      set.disabled=!state.configured;
      const usage=document.createElement('p');
      usage.className='muted';
      const local=project.local_quota;
      usage.textContent=local
        ? 'Локально учтено '+local.used+' из '+local.limit+
          ', остаток '+local.remaining+
          (local.externally_blocked?' (Google сообщил об исчерпании дневной квоты)':'')+
          '. Полная квота и затраты Google неизвестны.'
        : 'Предел не установлен: автоматические запросы Gemini для этого проекта запрещены.';
      budget.addEventListener('submit',async event=>{
        event.preventDefault();
        const limit=Number(cap.value);
        if(!Number.isInteger(limit)||limit<1||limit>1000000){
          flash('Укажите целый предел от 1 до 1 000 000.',true);
          return;
        }
        set.disabled=true;
        try{
          await request('/v1/me/gemini-keys/'+encodeURIComponent(project.project_id)+'/daily-cap',{
            method:'PUT',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({daily_request_cap:limit}),
          });
          await refresh();
          flash('Локальный суточный предел проекта сохранён.');
        }catch(error){flash(error.message,true);set.disabled=false;}
      });
      budget.append(field,set,usage);
      const projectControls=document.createElement('div');
      projectControls.className='gemini-project-controls';
      projectControls.append(label,budget);
      row.append(projectControls,remove);
      list.append(row);
    }
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const projectId=String(form.elements.namedItem('project_id').value).trim();
    const input=form.elements.namedItem('api_key');
    const apiKey=String(input.value).trim();
    // Clear visible key immediately, before awaiting network or on failure.
    input.value='';
    const save=form.querySelector('button[type="submit"]');
    save.disabled=true;
    try{
      await request('/v1/me/gemini-keys',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({project_id:projectId,api_key:apiKey}),
      });
      await refresh();
      flash('Ключ проекта сохранён.');
    }catch(error){
      flash(error.message,true);
    }finally{
      // Do not keep a copied key in a hidden password input.
      input.value='';
      if(status.textContent.includes('Ключи зашифрованы'))save.disabled=false;
    }
  });
  return {refresh,clear,clearForm};
}
