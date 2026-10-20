/** Pure, bounded TOC selection and display-order model.
 * Only source ordinals enter the network request; URLs stay server-owned.
 * A nonselected chapter is deliberately excluded, never a missing result.
 */
export const PAGE_SIZE = 80;
const positiveOrdinal = n => Number.isSafeInteger(n) && n >= 1 && n <= 10000000;
const auxiliary = chapter => ['extra','service'].includes(chapter.entry_type);
function clone(plan) {
  return {...plan, order:[...plan.order], selected:new Set(plan.selected),
    unresolved:new Set(plan.unresolved)};
}
export function createTocPlan(chapters, restoredOrder=null) {
  if (!Array.isArray(chapters) || !chapters.length || chapters.length > 2000) {
    throw new Error('Некорректное количество глав');
  }
  const origin=chapters.map(c=>c.ordinal);
  if (origin.some(n=>!positiveOrdinal(n)) || new Set(origin).size!==origin.length) {
    throw new Error('Неоднозначная нумерация в оглавлении');
  }
  const source=new Map(chapters.map(c=>[c.ordinal,c]));
  if (restoredOrder !== null && (!Array.isArray(restoredOrder) ||
    !restoredOrder.length || restoredOrder.some(n=>!source.has(n)) ||
    new Set(restoredOrder).size !== restoredOrder.length)) {
    throw new Error('Сохранённый план не соответствует оглавлению');
  }
  const included=restoredOrder ?? origin.filter(n=>!auxiliary(source.get(n)));
  const selected=new Set(included);
  return {
    source,original:origin,
    order:restoredOrder===null?[...origin]:
      [...restoredOrder,...origin.filter(n=>!selected.has(n))],
    selected,unresolved:new Set(restoredOrder===null ?
      origin.filter(n=>auxiliary(source.get(n))) : []),
  };
}
export function selectedOrdinals(plan) {
  return plan.order.filter(n=>plan.selected.has(n));
}
export function visibleOrdinals(plan,search='') {
  const query=String(search||'').trim().toLocaleLowerCase();
  if (!query) return [...plan.order];
  return plan.order.filter(n=>{
    const chapter=plan.source.get(n);
    return String(n).includes(query) ||
      String(chapter.title||'').toLocaleLowerCase().includes(query) ||
      String(chapter.source_id||'').toLocaleLowerCase().includes(query);
  });
}
export function pageOf(plan,search='',page=0,size=PAGE_SIZE) {
  if (!Number.isSafeInteger(page) || !Number.isSafeInteger(size) || size<1 || size>200) {
    throw new Error('Invalid catalog page');
  }
  const all=visibleOrdinals(plan,search);
  const count=Math.max(1,Math.ceil(all.length/size));
  const current=Math.min(Math.max(0,page),count-1);
  return {page:current,pages:count,total:all.length,
    ordinals:all.slice(current*size,(current+1)*size)};
}
export function toggleSelected(plan,ordinal,enabled) {
  if (!plan.source.has(ordinal)) throw new Error('Unknown chapter');
  const next=clone(plan);
  if (enabled) next.selected.add(ordinal);
  else next.selected.delete(ordinal);
  next.unresolved.delete(ordinal);
  return next;
}
export function selectOrdinals(plan,ordinals,enabled) {
  let next=clone(plan);
  for (const n of ordinals) {
    if (!next.source.has(n)) throw new Error('Unknown chapter');
    if (enabled) next.selected.add(n);
    else next.selected.delete(n);
    next.unresolved.delete(n);
  }
  return next;
}
export function moveBefore(plan,moving,target) {
  if (!plan.source.has(moving) || !plan.source.has(target) || moving===target) return plan;
  const next=clone(plan);
  next.order.splice(next.order.indexOf(moving),1);
  next.order.splice(next.order.indexOf(target),0,moving);
  return next;
}
export function moveVisible(plan,ordinal,direction,search='') {
  if (direction!==-1 && direction!==1) throw new Error('Invalid move direction');
  const visible=visibleOrdinals(plan,search);
  const idx=visible.indexOf(ordinal);
  const other=idx+direction;
  if (idx<0 || other<0 || other>=visible.length) return plan;
  // Swap visible positions; hidden filtered records remain in place.
  const next=clone(plan);
  const a=next.order.indexOf(ordinal),b=next.order.indexOf(visible[other]);
  [next.order[a],next.order[b]]=[next.order[b],next.order[a]];
  return next;
}
export function planSummary(plan) {
  const selected=selectedOrdinals(plan);
  const originalSelected=plan.original.filter(n=>plan.selected.has(n));
  return {
    total:plan.original.length,selected:selected.length,
    excluded:plan.original.length-selected.length,
    moved:selected.filter((n,i)=>n!==originalSelected[i]).length,
    unresolved:plan.unresolved.size,
    first:selected.slice(0,10),
  };
}
