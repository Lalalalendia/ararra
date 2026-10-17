/** Read-only accepted-chapter QA diagnostics with explicit human review.
 * Text/evidence is placed into DOM only via textContent, never innerHTML.
 * No direct fetch or browser-persisted keys: reuse authenticated request.
 */
const $=id=>document.getElementById(id);
const element=(tag,text)=>{const el=document.createElement(tag);el.textContent=String(text??'');return el;};

export function initChapterQaPanel({request,flash,current}) {
  const root=$('editor-qa-list'),summary=$('editor-qa-summary');
  const scanButton=$('editor-qa-scan');
  let epoch=0, snapshot=null;

  function path(ctx) {
    return '/v1/books/'+encodeURIComponent(ctx.bookId)
      +'/chapters/'+encodeURIComponent(ctx.chapterId)+'/qa';
  }
  function same(ctx) {
    const now=current();
    return !!now && now.bookId===ctx.bookId && now.chapterId===ctx.chapterId;
  }
  function blank() {
    ++epoch;snapshot=null;root.replaceChildren();
    summary.textContent='Выберите принятую главу для проверки.';
    scanButton.disabled=true;
  }
  function formatEvidence(evidence) {
    if(!evidence||typeof evidence!=='object')return '';
    return Object.entries(evidence).filter(([key,value])=>
      value!=null && key!=='target_slot_ids'
    ).slice(0,8).map(([key,value])=>
      key+': '+String(typeof value==='string'?value:JSON.stringify(value)).slice(0,350)
    ).join(' · ');
  }
  function render(ctx) {
    root.replaceChildren();
    const report=snapshot?.report;
    scanButton.disabled=!snapshot?.accepted_current;
    if(!snapshot?.accepted_current) {
      summary.textContent='Принятого актуального перевода нет; проверка недоступна.';
      return;
    }
    if(!report) {
      summary.textContent='Для этой ревизии ещё нет QA-отчёта. Проверка не вызывает Gemini.';
      return;
    }
    summary.textContent='Диагностика · исходник rev '+report.source_revision+
      ' · принято rev '+report.accepted_revision+
      ' · глоссарий rev '+report.glossary_revision+
      ' · замечаний '+report.finding_count+
      (report.truncated?' (показаны первые '+report.stored_count+')':'')+
      '. Автоматическая блокировка выключена.';
    for(const finding of report.findings) {
      const card=document.createElement('article');card.className='editor-note';
      const label=element('strong',finding.code+' · '+finding.severity+
        ' · '+({open:'Открыто',reviewed:'Проверено',waived:'Допущено'}[finding.review_status]
        ||finding.review_status));
      const message=element('p',finding.message);
      const metadata=element('small','Рекомендовано блокировать: '+
        (finding.would_block?'да, только диагностика':'нет')+
        ' · rev '+finding.review_revision+
        ' · '+(finding.reviewer_id||'без решения'));
      card.append(label,message,metadata);
      const evidence=formatEvidence(finding.evidence);
      if(evidence) {
        const details=document.createElement('details');
        details.append(element('summary','Проверить свидетельства'));
        details.append(element('pre',evidence));
        card.append(details);
      }
      if(finding.review_note)card.append(element('p','Решение: '+finding.review_note));
      const controls=document.createElement('div');controls.className='chapter-authoring-actions';
      const note=element('textarea','');note.className='ui-input';
      note.placeholder='Причина допуска / комментарий (при допуске обязательна)';
      note.maxLength=1000;note.rows=2;
      const reviewed=element('button','Проверено');
      reviewed.type='button';reviewed.className='ui-button ui-button--ghost';
      const waived=element('button','Допустить');
      waived.type='button';waived.className='ui-button ui-button--ghost';
      for(const [button,status] of [[reviewed,'reviewed'],[waived,'waived']]) {
        button.addEventListener('click',async()=>{
          if(!same(ctx))return;
          if(status==='waived'&&!note.value.trim()){
            flash('Для допуска QA-замечания требуется указать причину.',true);return;
          }
          reviewed.disabled=true;waived.disabled=true;
          try {
            await request(path(ctx)+'/findings/'+encodeURIComponent(finding.finding_id),{
              method:'PATCH',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({
                status, note:note.value.trim(),expected_review_revision:finding.review_revision,
                expected_source_revision:report.source_revision,
                expected_accepted_revision:report.accepted_revision,
                expected_glossary_revision:report.glossary_revision,
              }),
            });
            if(same(ctx)) {await load();flash('QA-решение сохранено с историей ревизий.');}
          } catch(err){if(same(ctx))flash(err.message,true);}
          finally {reviewed.disabled=false;waived.disabled=false;}
        });
      }
      controls.append(reviewed,waived);card.append(note,controls);root.append(card);
    }
    if(!report.findings.length)root.append(element('p','Замечаний для этой редакции не найдено.'));
  }
  async function load() {
    const ctx=current();
    if(!ctx)return;
    const seq=++epoch;
    const fresh=await request(path(ctx));
    if(seq!==epoch||!same(ctx))return;
    snapshot=fresh;render(ctx);
  }
  scanButton.addEventListener('click',async()=>{
    const ctx=current();
    if(!ctx||!snapshot?.accepted_current)return;
    scanButton.disabled=true;
    try {
      const data=await request(path(ctx),{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          expected_source_revision:snapshot.source_revision,
          expected_accepted_revision:snapshot.accepted_revision,
          expected_glossary_revision:snapshot.glossary_revision,
        }),
      });
      if(same(ctx)){await load();flash('QA-отчёт сохранён для точной принятой ревизии.');}
    }catch(e){if(same(ctx))flash(e.message,true);}
    finally{if(same(ctx))scanButton.disabled=!snapshot?.accepted_current;}
  });
  return {blank,load};
}
