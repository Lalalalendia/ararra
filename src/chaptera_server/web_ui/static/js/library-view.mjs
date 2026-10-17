/** ReadEra book directory: real SQLite-backed counts and on-demand chapter TOC.
 * No templated authors, fabricated publication states or untrusted HTML.
 */
const node = (tag, value, css='') => {
  const el=document.createElement(tag);
  if(value!==null && value!==undefined)el.textContent=String(value);
  if(css)el.className=css;
  return el;
};

export function createLibraryView({body,filter,empty,request,openEditor,showError}) {
  let catalog=[];

  function bookRow(book) {
    const tr=node('tr',null,'library-book-row');
    const title=node('td');
    title.append(node('strong',book.title),node('small',book.book_id));
    tr.append(title);
    tr.append(node('td',book.chapter_count));
    tr.append(node('td',book.accepted_current_count+' / '+book.chapter_count));
    tr.append(node('td',book.owner_member_id || 'Не назначен'));

    const actions=node('td',null,'library-row-actions');
    const toggle=node('button','Главы','ui-button ui-button--ghost');
    toggle.type='button';toggle.setAttribute('aria-expanded','false');
    toggle.setAttribute('aria-label','Оглавление книги '+book.title);
    toggle.addEventListener('click',()=>void toggleChapters(book,tr,toggle));
    const edit=node('button','Редактор','ui-button ui-button--primary');
    edit.type='button';
    edit.setAttribute('aria-label','Редактировать книгу '+book.title);
    edit.addEventListener('click',()=>openEditor(book.book_id));
    actions.append(toggle,edit);
    tr.append(actions);
    return tr;
  }

  async function toggleChapters(book,tr,button) {
    const next=tr.nextElementSibling;
    if(next?.classList.contains('library-chapters-row')) {
      next.remove();button.setAttribute('aria-expanded','false');return;
    }
    button.disabled=true;button.textContent='Загрузка...';
    try {
      const reply=await request('/v1/books/'+encodeURIComponent(book.book_id)+'/chapters');
      if(!tr.isConnected)return; // Search/refresh may have replaced the row.
      const detail=node('tr',null,'library-chapters-row');
      const cell=node('td');cell.colSpan=5;
      const heading=node('strong','Оглавление: '+book.title);
      cell.append(heading);
      const chapters=reply.chapters || [];
      if(!chapters.length) {
        cell.append(node('p','Глав пока нет.','muted'));
      } else {
        const list=node('ol',null,'library-toc');
        for(const chapter of chapters) {
          const item=node('li');
          const description=node('div',null,'library-chapter-description');
          description.append(
            node('strong','Глава '+chapter.chapter_index+' · '+chapter.chapter_id),
            node('small',chapter.accepted_revision>0
              ? (chapter.accepted_current
                ? 'Принятая редакция rev '+chapter.accepted_revision
                : 'Принятый перевод устарел относительно исходника')
              : 'Принятого перевода нет'),
          );
          const edit=node('button','Открыть','ui-button ui-button--ghost');
          edit.type='button';
          edit.setAttribute('aria-label','Открыть главу '+chapter.chapter_index+' книги '+book.title);
          edit.addEventListener('click',()=>openEditor(book.book_id,chapter.chapter_id));
          item.append(description,edit);list.append(item);
        }
        cell.append(list);
      }
      detail.append(cell);tr.after(detail);
      button.setAttribute('aria-expanded','true');
    } catch(error) {
      showError(error);
    } finally {
      button.disabled=false;button.textContent='Главы';
    }
  }

  function render(books=catalog) {
    catalog=books;
    const q=filter.value.trim().toLocaleLowerCase('ru-RU');
    const subset=catalog.filter(b=>
      [b.title,b.book_id,b.owner_member_id].join(' ')
        .toLocaleLowerCase('ru-RU').includes(q));
    body.replaceChildren(...subset.map(bookRow));
    empty.hidden=subset.length>0;
  }
  filter.addEventListener('input',()=>render());
  return {render};
}
