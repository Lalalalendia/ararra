/* Immutable source archive reader. Every API request is team-authenticated.
 * Third-party chapter text and labels are rendered through textContent only.
 */
export function createSourceArchiveView({request,flash}) {
  const $=id=>document.getElementById(id);
  const root=$('source-archive');
  const books=$('source-archive-books'),refreshButton=$('source-archive-refresh');
  const openButton=$('source-archive-open'),status=$('source-archive-status');
  const reader=$('source-archive-reader'),chapters=$('source-archive-chapters');
  const nextButton=$('source-archive-next'),readButton=$('source-archive-read');
  const caption=$('source-archive-caption'),body=$('source-archive-body');
  let items=new Map(),active='',offset=0,busy=false,generation=0;
  const option=(id,name)=>{
    const node=document.createElement('option');
    node.value=String(id);node.textContent=String(name);return node;
  };
  function clearReader() {
    active='';offset=0;reader.hidden=true;
    chapters.replaceChildren(option('','Выберите сохранённую главу'));
    chapters.disabled=true;readButton.disabled=true;
    nextButton.hidden=true;caption.textContent='';
    body.textContent='';body.hidden=true;
  }
  function controls() {
    refreshButton.disabled=busy;
    books.disabled=busy||items.size===0;
    openButton.disabled=busy||!items.has(books.value);
    chapters.disabled=busy||!active||chapters.options.length<2;
    readButton.disabled=busy||!active||!chapters.value;
    nextButton.disabled=busy;
  }
  function reset() {
    generation++;items.clear();busy=false;
    books.replaceChildren(option('','Загрузите архив'));
    clearReader();controls();root.open=false;
    status.textContent='Архив доступен только после входа в Эриду.';
  }
  async function refresh() {
    if(busy)return;
    const ticket=++generation;busy=true;controls();
    try {
      const result=await request('/v1/source-books/archive?limit=30&offset=0');
      if(ticket!==generation)return;
      if(result?.scope!=='team_shared_original_archive'||!Array.isArray(result.books))
        throw Error('Сервер не подтвердил список архива.');
      items=new Map(result.books.map(x=>[x.archive_book_id,x]));
      books.replaceChildren(option('','Выберите книгу'));
      for(const book of items.values()) {
        let title='книга';
        try {
          title=decodeURIComponent(new URL(book.book_url).pathname
            .split('/').filter(Boolean).at(-1)||'книга')
            .replace(/[-_]/g,' ').slice(0,55);
        } catch {/* URL has already been validated by the server. */}
        books.append(option(book.archive_book_id,
          book.source+' · '+title+' · '+book.chapter_count+' глав'));
      }
      clearReader();
      status.textContent='Книг с сохранёнными оригиналами: '+items.size+
        '. Архив читается всей командой.';
    } catch(err) {
      if(ticket!==generation)return;
      items.clear();books.replaceChildren(option('','Ошибка архива'));
      clearReader();status.textContent='Не удалось открыть архив.';
      flash(err.message,true);
    } finally {
      if(ticket===generation){busy=false;controls();}
    }
  }
  async function load(bookId,start,ticket) {
    const result=await request('/v1/source-books/archive/'+
      encodeURIComponent(bookId)+'?limit=50&offset='+start);
    if(ticket!==generation)return;
    if(result?.scope!=='team_shared_original_archive'||
       result.archive_book_id!==bookId||!Array.isArray(result.chapters))
      throw Error('Некорректный список сохранённых глав.');
    if(start===0)chapters.replaceChildren(option('','Выберите сохранённую главу'));
    for(const ch of result.chapters) {
      chapters.append(option(ch.revision_id,
        'Глава '+ch.ordinal+' · '+ch.title+' · '+ch.body_sha256.slice(0,8)));
    }
    offset=start+result.chapters.length;
    nextButton.hidden=!result.has_more;
    status.textContent='Сохранённых ревизий на экране: '+offset+
      '. Права на изменение архива есть только у администратора.';
  }
  refreshButton.addEventListener('click',()=>void refresh());
  books.addEventListener('change',()=>{
    generation++;clearReader();controls();
  });
  openButton.addEventListener('click',()=>{
    if(busy||!items.has(books.value))return;
    const bookId=books.value,ticket=++generation;
    busy=true;clearReader();controls();
    void (async()=>{
      try{
        await load(bookId,0,ticket);
        if(ticket!==generation)return;
        active=bookId;reader.hidden=false;
      }catch(err){
        if(ticket===generation){status.textContent='Не удалось загрузить главы.';flash(err.message,true);}
      }finally{if(ticket===generation){busy=false;controls();}}
    })();
  });
  nextButton.addEventListener('click',()=>{
    if(busy||!active)return;
    const id=active,start=offset,ticket=++generation;
    busy=true;controls();
    void (async()=>{
      try{await load(id,start,ticket);}
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
    const revision=chapters.value,ticket=++generation;
    busy=true;body.textContent='';body.hidden=true;controls();
    void (async()=>{
      try {
        const data=await request('/v1/source-books/archive/revisions/'+
          encodeURIComponent(revision));
        if(ticket!==generation||data?.revision_id!==revision)return;
        if(data.scope!=='single_verified_archived_original'||
           typeof data.body!=='string')throw Error('Недостоверный оригинал.');
        caption.textContent='Глава '+data.ordinal+' · '+data.title+
          ' · ревизия '+data.sha256.slice(0,8)+' · только чтение';
        body.textContent=data.body;
        body.hidden=false;
      }catch(err){
        if(ticket!==generation)return;
        caption.textContent='Исходник недоступен или повреждён.';
        flash(err.message,true);
      }finally{if(ticket===generation){busy=false;controls();}}
    })();
  });
  return {reset,refresh};
}
