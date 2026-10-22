/** Chapter navigation is a projection of the same server snapshot as the card.
 * Never mark an attempted save as accepted or rebuild the select/textarea here.
 */
export function chapterOptionLabel(row) {
  const state=row.accepted_revision>0
    ? (row.accepted_current?' \u00b7 \u043f\u0440\u0438\u043d\u044f\u0442\u043e':' \u00b7 \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e')
    : ' \u00b7 \u0438\u0441\u0445\u043e\u0434\u043d\u0438\u043a';
  return '\u0413\u043b\u0430\u0432\u0430 '+row.chapter_index+' \u00b7 '+row.chapter_id+state;
}

/** Called only for a successfully loaded/accepted chapter in the active book.
 * Keep the TOC metadata fresh, but do not cache complete chapter HTML there.
 */
export function syncChapterOption(bookId, toc, select, data) {
  if(data.book_id!==bookId)return false;
  const row=toc.find(item=>item.chapter_id===data.chapter_id);
  const item=Array.from(select.options).find(node=>node.value===data.chapter_id);
  if(!row||!item)return false;
  Object.assign(row,{
    chapter_index:data.chapter_index,
    source_revision:data.source_revision,
    accepted_revision:data.accepted_revision,
    accepted_source_revision:data.accepted_source_revision,
    accepted_current:data.accepted_current,
  });
  // Identifiers are untrusted text, not HTML or CSS selector fragments.
  item.textContent=chapterOptionLabel(row);
  return true;
}
