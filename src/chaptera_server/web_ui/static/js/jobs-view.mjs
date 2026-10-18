/** Read-only presentation of queued work and local Gemini daily RPD.
 * The controller retains all authenticated fetches; these renderers never
 * access browser storage, Google credentials or provider APIs.
 */
export function renderDailyQuota(target,state) {
  if(!state){
    target.textContent='Данные локальной квоты временно недоступны.';
    return;
  }
  if(!state.enabled){
    target.textContent=state.personal_projects
      ? 'Лимиты Gemini учитываются отдельно для ваших проектов; остаток квоты Google неизвестен.'
      : 'Локальный суточный лимит не настроен. Квота Google неизвестна.';
    return;
  }
  const reset=new Date(Number(state.reset_at)*1000);
  const resetLabel=Number.isFinite(reset.getTime())
    ? reset.toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short'})
    : 'неизвестно';
  const exhausted=state.externally_blocked
    ? ' Провайдер сообщил об исчерпании дневной квоты (429).'
    : '';
  target.textContent='Модель '+state.model+': зарезервировано '+state.used+
    ' из '+state.limit+' запросов; локальный остаток '+state.remaining+
    '. Сброс '+resetLabel+' по времени устройства.'+exhausted;
}

export function renderJobs(root,jobs,{text,shorten,renderStatus}) {
  root.replaceChildren();
  for(const job of jobs) {
    const item=document.createElement('article');item.className='chapter-import-queue__item';
    const main=document.createElement('div');main.className='chapter-import-queue__main';
    text(main,'Задание '+shorten(job.id,80),'strong');
    text(main,shorten(job.chapter_id,80),'small');
    const meta=document.createElement('div');meta.className='chapter-import-queue__meta';
    const label=text(meta,job.status,'span');
    label.dataset.importStatusLabel='';
    item.append(main,meta);root.append(item);
    if(renderStatus) {
      renderStatus(item,{
        status:job.status==='completed'?'succeeded':job.status,
        status_label:job.status,progress:{current:0,total:0},
        result:{},preview:{},error_message:'',
      });
    }
  }
  if(!jobs.length)text(root,'Заданий пока нет.','p');
}
