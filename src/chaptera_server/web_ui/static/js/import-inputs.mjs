/** Small import-specific DOM primitive: no fetch, no stored credentials. */
export function rowInput(type,value,label,change) {
  const el=document.createElement(type);
  if(type==='input'){el.type='text';el.value=value;el.setAttribute('aria-label',label);el.addEventListener('input',e=>change(e.target.value));}
  return el;
}
