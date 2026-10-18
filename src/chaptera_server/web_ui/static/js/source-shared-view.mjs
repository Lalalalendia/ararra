/* Team-readable, checksum-verified original chapters only.
 * Source collection jobs and their deletion/retry/staging remain actor-owned.
 * Rendering uses textContent; remote prose is NEVER inserted as HTML.
 */
export function createSharedSourceView({request,flash}) {
  const $=id=>document.getElementById(id);
  const picker=$('source-shared-list');
  const refreshButton=$('source-shared-refresh');
  const openButton=$('source-shared-open');
  const status=$('source-shared-status');
  const reader=$('source-shared-reader');
  const chapters=$('source-shared-chapters');
  const nextButton=$('source-shared-next');
  const readButton=$('source-shared-read');
  const caption=$('source-shared-caption');
  const body=$('source-shared-body');
  let sources=new Map();
  let active=null;
  let loaded=0;
  let busy=false;
  let generation=0;

  function option(value,label) {
    const node=document.createElement('option');
    node.value=String(value);
    node.textContent=String(label);
    return node;
  }
  function clearReader() {
    active=null;loaded=0;reader.hidden=true;
    chapters.replaceChildren(option('','Выберите главу'));
    chapters.disabled=true;
    nextButton.hidden=true;readButton.disabled=true;
    caption.textContent='';body.textContent='';body.hidden=true;
  }
  function controls() {
    picker.disabled=busy||sources.size===0;
    refreshButton.disabled=busy;
    openButton.disabled=busy||!sources.has(picker.value);
    chapters.disabled=busy||!active||chapters.options.length<=1;
    readButton.disabled=busy||!active||!chapters.value;
    nextButton.disabled=busy;
  }
  function reset() {
    generation++;
    sources.clear();active=null;busy=false;
    picker.replaceChildren(option('','Загрузите список'));
    status.textContent='Список доступен после входа в Эриду.';
    clearReader();controls();
    $('source-shared').open=false;
  }
  async function refresh() {
    if(busy)return;
    const ticket=++generation;
    busy=true;controls();
    try {
      const result=await request('/v1/source-books/jobs/shared?limit=30');
      if(ticket!==generation)return;
      if(result?.scope!=='team_verified_originals'||!Array.isArray(result.sources))
        throw new Error('Неверный формат общего каталога.');
      sources=new Map(result.sources.map(item=>[item.job_id,item]));
      const before=picker.value;
      picker.replaceChildren(option('','Выберите сборку'));
      for(const item of sources.values()){
        let book='книга';
        try{
          book=decodeURIComponent(new URL(item.book_url).pathname.split('/').filter(Boolean).at(-1)||'книга')
            .replace(/[-_]+/g,' ').slice(0,55);
        }catch{/* Only server-validated source URLs should arrive. */}
        picker.append(option(item.job_id,
          item.source+' · '+book+' · '+item.owner_member_id+
          ' · '+item.verified_count+'/'+item.selected_count));
      }
      picker.value=sources.has(before)?before:'';
      clearReader();
      status.textContent='Сохранённых сборок с проверенными исходниками: '+sources.size+
        '. Это общие тексты, но управление заданиями остаётся у их авторов.';
    }catch(err){
      if(ticket!==generation)return;
      sources.clear();picker.replaceChildren(option('','Ошибка загрузки'));
      clearReader();status.textContent='Общий каталог не загружен.';
      flash(err.message,true);
    }finally {
      if(ticket===generation){busy=false;controls();}
    }
  }
  async function loadChapters(jobId,offset,ticket) {
    const data=await request('/v1/source-books/jobs/shared/'+encodeURIComponent(jobId)+
      '?offset='+offset+'&limit=50');
    if(ticket!==generation||data?.job_id!==jobId)return;
    if(data.scope!=='verified_original_checkpoints'||!Array.isArray(data.chapters))
      throw new Error('Неверные метаданные исходных глав.');
    if(offset===0){
      chapters.replaceChildren(option('','Выберите главу'));
      caption.textContent='';
    }
    for(const item of data.chapters) {
      chapters.append(option(item.ordinal,'Глава '+item.ordinal+' · '+item.title));
    }
    loaded=offset+data.chapters.length;
    nextButton.hidden=!data.has_more;
    status.textContent='Доступно проверенных глав: '+data.verified_count+
      '. Показано '+loaded+'. Владелец сборки: '+data.owner_member_id+'.';
  }
  refreshButton.addEventListener('click',()=>void refresh());
  picker.addEventListener('change',()=>{
    generation++;clearReader();controls();
  });
  openButton.addEventListener('click',()=>{
    if(busy||!sources.has(picker.value))return;
    const jobId=picker.value;
    const ticket=++generation;
    busy=true;controls();clearReader();
    void (async()=>{
      try {
        await loadChapters(jobId,0,ticket);
        if(ticket!==generation)return;
        active=jobId;reader.hidden=false;
      }catch(err){
        if(ticket!==generation)return;
        status.textContent='Не удалось открыть общие главы.';
        flash(err.message,true);
      }finally {
        if(ticket===generation){busy=false;controls();}
      }
    })();
  });
  nextButton.addEventListener('click',()=>{
    if(busy||!active)return;
    const jobId=active,offset=loaded,ticket=++generation;
    busy=true;controls();
    void (async()=>{
      try{await loadChapters(jobId,offset,ticket);}
      catch(err){if(ticket===generation)flash(err.message,true);}
      finally{if(ticket===generation){busy=false;controls();}}
    })();
  });
  chapters.addEventListener('change',()=>{
    generation++;body.textContent='';body.hidden=true;caption.textContent='';
    controls();
  });
  readButton.addEventListener('click',()=>{
    if(busy||!active||!chapters.value)return;
    const jobId=active,ordinal=Number(chapters.value),ticket=++generation;
    busy=true;body.hidden=true;body.textContent='';controls();
    void (async()=>{
      try{
        const chapter=await request('/v1/source-books/jobs/shared/'+
          encodeURIComponent(jobId)+'/chapters/'+ordinal);
        if(ticket!==generation||chapter?.job_id!==jobId||chapter?.ordinal!==ordinal)return;
        if(chapter.scope!=='single_verified_original'||typeof chapter.body!=='string')
          throw new Error('Сервер не подтвердил проверенный оригинал.');
        caption.textContent='Глава '+ordinal+' · '+chapter.title+
          ' · оригинал, только чтение';
        body.textContent=chapter.body;
        body.hidden=false;
      }catch(err){
        if(ticket!==generation)return;
        caption.textContent='Глава недоступна или не прошла проверку.';
        flash(err.message,true);
      }finally{if(ticket===generation){busy=false;controls();}}
    })();
  });
  return {reset,refresh};
}
