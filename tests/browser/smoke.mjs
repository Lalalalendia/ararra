/** Browser acceptance for Jinja2 / books_platform interface, no Gemini worker.
 * Screenshots omit login and one-time secret fields.
 */
import assert from 'node:assert/strict';
import {mkdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const origin=process.env.READ_E2E_BASE_URL || 'http://127.0.0.1:18376';
const adminKey=process.env.READ_E2E_ADMIN_KEY || 'test-local-administrator-only';
const modKey=process.env.READ_E2E_MOD_KEY || 'test-local-moderator-only';
const output=fileURLToPath(new URL('./artifacts/',import.meta.url));
mkdirSync(output,{recursive:true});
const pageErrors=[];
const sample=(first='Первое предложение.')=>'Предисловие к книге.\n\nГлава 12. Первая\n'+first+
  '\n\nГлава 13. Вторая\nВторое предложение.\n';

async function api(path,key=modKey,method='GET',body) {
  const response=await fetch(new URL(path,origin),{
    method,headers:{'X-API-Key':key,...(body?{'Content-Type':'application/json'}:{})},
    ...(body?{body:JSON.stringify(body)}:{}),
  });
  return {status:response.status,data:await response.json()};
}
async function login(page,key){
  await page.locator('#login-form [name="api_key"]').fill(key);
  await page.locator('#login-form button[type="submit"]').click();
  await page.locator('#authenticated').waitFor({state:'visible'});
  assert.equal(await page.locator('#login-form').isVisible(),false);
}
async function choose(page,name){
  await page.locator('.sidebar-nav a[data-page-link="'+name+'"]').click();
  await page.locator('[data-page="'+name+'"]').waitFor({state:'visible'});
}
async function upload(page,name,source){
  await page.locator('#chapters_file').setInputFiles({
    name,mimeType:'text/plain',buffer:Buffer.from(source,'utf8'),
  });
  await page.locator('#import-upload button[type="submit"]').click();
  await page.locator('#import-map').waitFor({state:'visible',timeout:15000});
}
function spy(page,name){page.on('pageerror',e=>pageErrors.push(name+': '+e.message));}
async function screenshot(page,name){
  await page.screenshot({path:output+'/'+name,fullPage:true,animations:'disabled'});
}
async function fit(page,label){
  const widths=await page.evaluate(()=>[document.documentElement.scrollWidth,document.documentElement.clientWidth]);
  assert.ok(widths[0]<=widths[1]+2,label+': page width overflow '+widths.join('/'));
}
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--no-sandbox']});
try{
  const context=await browser.newContext({viewport:{width:1440,height:900},locale:'ru-RU'});
  const page=await context.newPage();spy(page,'desktop');
  const response=await page.goto(origin+'/app/overview');
  assert.equal(response.status(),200);
  assert.match(await page.title(),/РидЭра/);
  assert.equal(await page.locator('#login-form').isVisible(),true);
  assert.equal((await api('/v1/books','invalid')).status,401);
  await login(page,adminKey);
  assert.match(await page.locator('#identity-label').innerText(),/Администратор/);
  assert.equal(await page.locator('#metric-books').innerText(),'0');
  await screenshot(page,'01-overview-desktop.png');await fit(page,'overview desktop');

  await choose(page,'team');
  await page.locator('#team-admin-only').waitFor({state:'visible'});
  await page.locator('#member-invite [name="member_id"]').fill('writer');
  await page.locator('#member-invite button[type="submit"]').click();
  await page.locator('#member-secret').waitFor({state:'visible'});
  const issued=await page.locator('#member-secret-code').innerText();
  assert.ok(issued.length>=32);
  await page.locator('#member-secret-hide').click();
  assert.equal(await page.locator('#member-secret').isVisible(),false);
  assert.match(await page.locator('#members-list').innerText(),/writer/);
  await screenshot(page,'02-team-desktop.png');
  await page.locator('#logout').click();
  assert.equal(await page.locator('#authenticated').isVisible(),false);
  await login(page,issued);
  assert.match(await page.locator('#identity-label').innerText(),/writer/);
  await choose(page,'team');
  assert.equal(await page.locator('#team-no-admin').isVisible(),true);
  assert.equal((await api('/v1/admin/members',issued)).status,403);

  await choose(page,'import');
  assert.match(await page.locator('#import-file-state').innerText(),/не выбран/);
  const droppedFile=await page.evaluateHandle(()=>{
    const transfer=new DataTransfer();
    transfer.items.add(new File(['Глава 1. Из файла\\nТекст.\\n'], 'dropped.txt',
      {type:'text/plain'}));
    return transfer;
  });
  await page.locator('#import-dropzone').dispatchEvent('dragenter',{dataTransfer:droppedFile});
  assert.equal(await page.locator('#import-dropzone').evaluate(el=>el.classList.contains('is-over')),true);
  await page.locator('#import-dropzone').dispatchEvent('drop',{dataTransfer:droppedFile});
  assert.equal(await page.locator('#import-dropzone').evaluate(el=>el.classList.contains('is-over')),false);
  assert.match(await page.locator('#import-file-state').innerText(),/dropped.txt/);
  assert.equal(await page.locator('#chapters_file').evaluate(el=>el.files[0].name),'dropped.txt');
  await screenshot(page,'02-import-dropzone-desktop.png');
  await fit(page,'import upload desktop');
  await droppedFile.dispose();
  // Native selection must update the same visible status and continue the
  // existing staged-upload workflow; drag/drop does not auto-import.
  await upload(page,'novel.txt',sample());
  assert.equal(await page.locator('#chapter-map tr').count(),2);
  assert.match(await page.locator('#stage-details').innerText(),/novel.txt/);
  assert.equal(await page.locator('#prefix-fieldset').isVisible(),true);
  await page.locator('#import-book-id').fill('browser-novel');
  await page.locator('#import-book-title').fill('Браузерная проверка');
  const first=page.locator('#chapter-map tr').first().locator('input');
  assert.equal(await first.nth(0).inputValue(),'12');
  assert.equal(await first.nth(1).inputValue(),'12');
  await page.locator('#import-review-button').click();
  assert.match(await page.locator('#flash').innerText(),/предисловию/);
  await page.locator('#prefix-fieldset input[value="preserve"]').check();
  await screenshot(page,'03-import-mapping.png');await fit(page,'import desktop');
  await page.locator('#import-review-button').click();
  await page.locator('#import-review').waitFor({state:'visible'});
  assert.match(await page.locator('#import-summary').innerText(),/Создать глав: 2/);
  await screenshot(page,'04-import-confirm.png');
  await page.locator('#import-ack').check();
  await page.locator('#import-confirm').click();
  await page.locator('#import-success').waitFor({state:'visible',timeout:15000});
  assert.match(await page.locator('#import-receipt').innerText(),/browser-novel/);
  const book=await api('/v1/books/browser-novel',issued);
  assert.equal(book.status,200);assert.equal(book.data.owner_member_id,'writer');
  assert.equal((await api('/v1/books/browser-novel/chapters/12',issued)).data.source_revision,1);
  await screenshot(page,'05-import-success.png');

  await choose(page,'books');
  const libraryRow=page.locator('#book-list .library-book-row')
    .filter({hasText:'Браузерная проверка'});
  await libraryRow.waitFor();
  assert.match(await libraryRow.innerText(),/writer/);
  assert.equal((await libraryRow.locator('td').nth(1).innerText()).trim(),'2');
  assert.equal((await libraryRow.locator('td').nth(2).innerText()).trim(),'0 / 2');
  assert.equal(await page.locator('#book-list').getByText('Опубликовано').count(),0);
  await screenshot(page,'06-library-desktop.png');await fit(page,'books desktop');

  // Expand the real TOC lazily and navigate to a selected chapter. Never
  // pretend the Canva mockup's sample author, published state or PDF quota.
  await libraryRow.getByRole('button',{name:'Оглавление книги Браузерная проверка'}).click();
  const chapters=page.locator('#book-list .library-chapters-row .library-toc li');
  await chapters.first().waitFor();
  assert.equal(await chapters.count(),2);
  await screenshot(page,'06-library-toc-desktop.png');
  await fit(page,'library TOC desktop');
  await chapters.filter({hasText:'Глава 13'}).getByRole('button',{name:/Открыть главу 13/}).click();
  await page.locator('[data-page="editor"]').waitFor({state:'visible'});
  await page.locator('#editor-chapter').waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('#editor-chapter')?.value==='13');
  await choose(page,'books');
  const rowAgain=page.locator('#book-list .library-book-row')
    .filter({hasText:'Браузерная проверка'});
  await rowAgain.getByRole('button',{name:'Редактировать книгу Браузерная проверка'}).click();
  await page.locator('[data-page="editor"]').waitFor({state:'visible'});
  await page.locator('#editor-chapter').selectOption('12');

  // Bookplatform-derived editor: safe original view, RU accepted revisions,
  // never replacing source or losing another editor's accepted revision.
  await page.locator('#editor-workspace').waitFor({state:'visible',timeout:15000});
  await page.locator('#editor-source').getByText(/Первое предложение/).waitFor();
  assert.equal(await page.locator('#editor-book').inputValue(),'browser-novel');
  assert.equal(await page.locator('#editor-chapter').inputValue(),'12');
  assert.match(await page.locator('#editor-source').innerText(),/Первое предложение/);
  assert.equal(await page.locator('#editor-accept').isEnabled(),false);
  await screenshot(page,'07-editor-empty-ru.png');
  await page.locator('#editor-text').fill('Глава 12. Первая\n\nРусский литературный перевод.');
  await page.locator('#editor-save-draft').click();
  await page.locator('#editor-draft-rev').getByText('rev 1').waitFor();
  const firstDraft=await api('/v1/books/browser-novel/chapters/12/draft',issued);
  assert.equal(firstDraft.status,200);
  assert.equal(firstDraft.data.body_text,'Глава 12. Первая\n\nРусский литературный перевод.');

  // Notes are separate from chapter text and must not execute attacker HTML.
  const poisoned='Проверить термин <img src=x onerror="window.__unsafe=1">';
  await page.locator('#editor-note-body').fill(poisoned);
  await page.locator('#editor-note-form button[type="submit"]').click();
  await page.locator('.editor-note').getByText(poisoned,{exact:true}).waitFor();
  assert.equal(await page.locator('#editor-notes-list img').count(),0);
  assert.equal(await page.evaluate(()=>window.__unsafe??0),0);
  assert.equal((await api('/v1/books/browser-novel/chapters/12',issued)).data.accepted_html,null);
  await page.locator('.editor-note button').first().click();
  await page.locator('.editor-note strong').getByText(/Решено/).waitFor();

  // Another editor updates shared draft; stale browser cannot overwrite it.
  const parallelDraft=await api('/v1/books/browser-novel/chapters/12/draft',issued,'PUT',{
    body_text:'Другой модератор сохранил правки.',expected_revision:1,
    expected_source_revision:1,expected_accepted_revision:0,
  });
  assert.equal(parallelDraft.status,200);
  await page.locator('#editor-text').fill('Локальное изменение после ревизии 1');
  await page.locator('#editor-save-draft').click();
  await page.locator('#flash').getByText(/Конфликт версий/).waitFor({timeout:15000});
  assert.match(await page.locator('#editor-text').inputValue(),/Локальное изменение/);
  page.once('dialog',d=>void d.accept());
  await page.locator('#editor-reload').click();
  await page.locator('#editor-draft-rev').getByText('rev 2').waitFor();
  assert.match(await page.locator('#editor-text').inputValue(),/Другой модератор/);

  await page.locator('#editor-text').fill('Глава 12. Первая\n\nРусский литературный перевод.');
  await page.locator('#editor-save-draft').click();
  await page.locator('#editor-draft-rev').getByText('rev 3').waitFor();
  // Canva editor reference: three simultaneously visible desktop columns.
  // Enforce bounded reading panes rather than an unbounded chapter-height form.
  const editorGeometry=await page.evaluate(()=>{
    const selectors=['.editor-source-col','.editor-ru-col','.editor-notes-col'];
    return {
      columns:selectors.map(s=>{
        const r=document.querySelector(s).getBoundingClientRect();
        return {left:r.left,right:r.right,top:r.top};
      }),
      source:document.querySelector('#editor-source').getBoundingClientRect().height,
      draft:document.querySelector('#editor-text').getBoundingClientRect().height,
    };
  });
  assert.ok(editorGeometry.columns[0].right<=editorGeometry.columns[1].left,
    'desktop: source and translation remain side by side');
  assert.ok(editorGeometry.columns[1].right<=editorGeometry.columns[2].left,
    'desktop: notes remain in a third column');
  assert.ok(editorGeometry.source<=450 && editorGeometry.draft<=450,
    'desktop: long original and draft have bounded reading panes');
  await screenshot(page,'07-editor-durable-draft.png');
  await page.reload();
  await login(page,issued);
  await page.locator('#editor-draft-rev').getByText('rev 3').waitFor();
  assert.match(await page.locator('#editor-text').inputValue(),/Русский литературный перевод/);
  assert.match(await page.locator('#editor-notes-list').innerText(),/Проверить термин/);
  assert.equal(await page.locator('#editor-accept').isEnabled(),true);
  await page.locator('#editor-accept').click();
  await page.locator('#editor-accepted-rev').getByText('rev 1').waitFor();
  const translation=await api('/v1/books/browser-novel/chapters/12',issued);
  assert.equal(translation.data.accepted_revision,1);
  assert.equal(translation.data.accepted_current,true);
  assert.match(translation.data.accepted_html,/Русский литературный перевод/);
  assert.equal(await page.locator('#editor-accept').isEnabled(),false);
  await screenshot(page,'08-editor-accepted.png');

  // The saved draft was based on accepted rev 0; deliberate rebase is required
  // before editing against accepted rev 1.
  await page.locator('#editor-rebase-wrap').waitFor({state:'visible'});
  await page.locator('#editor-rebase').check();
  await page.locator('#editor-save-draft').click();
  await page.locator('#editor-draft-rev').getByText('rev 4').waitFor();
  await page.locator('#editor-text').fill('Вторая редакция модератора.');
  const elsewhere=await api('/v1/books/browser-novel/chapters/12/accepted-translation',
    issued,'PUT',{translated_html:'<p>Редакция другого редактора.</p>',
      expected_revision:1,expected_source_revision:1});
  assert.equal(elsewhere.status,200);
  await page.locator('#editor-accept').click();
  await page.locator('#flash').getByText(/Конфликт версий/).waitFor({timeout:15000});
  assert.equal(await page.locator('#editor-unsaved').isVisible(),true);
  assert.equal((await api('/v1/books/browser-novel/chapters/12',issued)).data.accepted_revision,2);
  await screenshot(page,'09-editor-conflict.png');
  page.once('dialog',d=>void d.accept());
  await page.locator('#editor-reload').click();
  await page.locator('#editor-accepted-rev').getByText('rev 2').waitFor();
  // Shared draft remains intact; it is marked stale instead of replacing
  // the last accepted translation silently.
  assert.match(await page.locator('#editor-text').inputValue(),/Русский литературный перевод/);
  assert.equal(await page.locator('#editor-draft-stale').isVisible(),true);
  await page.locator('.editor-revision').filter({hasText:'Ревизия 1'}).getByText('Посмотреть').click();
  await page.locator('#editor-history-preview').getByText(/Русский литературный перевод/).waitFor({timeout:15000});
  page.once('dialog',d=>void d.accept());
  await page.locator('.editor-revision').filter({hasText:'Ревизия 1'}).getByText('Восстановить').click();
  await page.locator('#editor-accepted-rev').getByText('rev 3').waitFor();
  const history=await api('/v1/books/browser-novel/chapters/12/accepted-revisions',issued);
  assert.equal(history.status,200);
  assert.deepEqual(history.data.revisions.map(x=>x.revision),[3,2,1]);
  assert.equal((await api('/v1/books/browser-novel/chapters/12/draft',issued)).data.revision,4);
  await screenshot(page,'09-editor-history-rollback.png');
  await fit(page,'editor desktop');

  await choose(page,'jobs');
  await page.locator('#jobs-refresh').click();
  await screenshot(page,'07-jobs-desktop.png');

  // Parallel author updates source after checked replacement; transaction rejects all.
  await choose(page,'import');
  // A reload intentionally discards browser-only import state, not committed books.
  if(await page.locator('#import-another').isVisible()){
    await page.locator('#import-another').click();
  }
  await upload(page,'replace.txt','Глава 12. Первая\nНовый текст после обновления.\n');
  await page.locator('#import-map input[name="book_mode"][value="existing"]').check();
  await page.locator('#import-existing').selectOption('browser-novel');
  await page.locator('#chapter-map tr select').first().selectOption('replace');
  await page.locator('#chapter-map tr button').first().click();
  await page.locator('#chapter-map tr [data-rev-note]').first().getByText('rev 1').waitFor();
  await page.locator('#import-review-button').click();
  await page.locator('#import-review').waitFor({state:'visible'});
  const external=await api('/v1/books/browser-novel/chapters/12',issued,'PUT',{
    chapter_index:12,chapter_path:'chapters/12.xhtml',
    source_html:'<h1>Глава 12</h1><p>Правка второго модератора.</p>',expected_revision:1,
  });
  assert.equal(external.status,200);
  await page.locator('#import-ack').check();
  await page.locator('#import-confirm').click();
  await page.locator('#flash').getByText(/Конфликт версий/).waitFor({timeout:15000});
  assert.equal(await page.locator('#import-success').isVisible(),false);
  assert.equal((await api('/v1/books/browser-novel/chapters/12',issued)).data.source_revision,2);
  await screenshot(page,'08-conflict-protected.png');
  await page.locator('#import-back').click();
  await page.locator('#import-map').waitFor({state:'visible'});
  await page.locator('#import-abort').click();
  await page.locator('#import-abort-confirm').waitFor({state:'visible'});
  await page.locator('#import-abort-yes').click();
  await page.locator('#import-upload').waitFor({state:'visible'});
  assert.equal((await api('/v1/books/browser-novel',issued)).status,200);
  await page.reload();
  assert.equal(await page.locator('#login-form').isVisible(),true);
  const stored=await page.evaluate(()=>({
    local:Object.keys(localStorage),session:Object.keys(sessionStorage),
  }));
  assert.deepEqual(stored,{local:[],session:[]});
  assert.ok(!page.url().includes(issued));

  // Responsive routing and no horizontal body overflow.
  const mobile=await browser.newContext({viewport:{width:390,height:844},locale:'ru-RU'});
  const mpage=await mobile.newPage();spy(mpage,'mobile');
  await mpage.goto(origin+'/app/books');
  await login(mpage,modKey);
  await mpage.locator('#book-list').getByText('Браузерная проверка').waitFor();
  await fit(mpage,'books mobile');await screenshot(mpage,'09-library-mobile.png');
  await choose(mpage,'import');await fit(mpage,'import mobile');
  await screenshot(mpage,'10-import-mobile.png');
  await choose(mpage,'editor');
  await mpage.locator('#editor-workspace').waitFor({state:'visible'});
  await fit(mpage,'editor mobile');
  async function assertMobileNavigation(label){
    const nav=await mpage.locator('.sidebar-nav').evaluate(el=>{
      const box=el.getBoundingClientRect();
      const active=el.querySelector('a[aria-current="page"]').getBoundingClientRect();
      return {height:box.height,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,
        visible:active.left>=box.left-1&&active.right<=box.right+1};
    });
    assert.ok(nav.height<=70,label+': navigation should fit into a single row');
    assert.ok(nav.scrollWidth>nav.clientWidth,label+': section list remains horizontally scrollable');
    assert.ok(nav.visible,label+': active route must be visible');
  }
  await assertMobileNavigation('mobile editor nav');
  await screenshot(mpage,'11-editor-mobile.png');
  // A direct editor URL after refresh should position the scrollable menu
  // on the active route without storing a session token.
  await mpage.reload();
  await login(mpage,modKey);
  await mpage.locator('#editor-workspace').waitFor({state:'visible'});
  await assertMobileNavigation('mobile direct-route nav');
  await mobile.close();

  // Book responsibility is shared visibility, but transferring it is admin-only.
  // Exercise real native dialog: cancellation, confirmation and stale CAS.
  await page.goto(origin+'/app/books');await login(page,adminKey);
  await page.locator('#book-owner-filter').selectOption('mine');
  assert.equal(await page.locator('#book-list .library-book-row').count(),0);
  await page.locator('#book-owner-filter').selectOption('unassigned');
  assert.equal(await page.locator('#book-list .library-book-row').count(),0);
  await page.locator('#book-owner-filter').selectOption('all');
  const ownerBook=page.locator('#book-list .library-book-row')
    .filter({hasText:'Браузерная проверка'});
  const openTransfer=()=>ownerBook.getByRole('button',{
    name:'Переназначить ответственным за книгу Браузерная проверка',
  });
  await openTransfer().click();
  const ownerDialog=page.locator('#book-owner-dialog');
  await ownerDialog.waitFor({state:'visible'});
  assert.match(await ownerDialog.locator('#book-owner-help').innerText(),/writer/);
  assert.deepEqual(await ownerDialog.locator('option').allTextContents(),['moderator']);
  await ownerDialog.getByRole('button',{name:'Отмена'}).click();
  await ownerDialog.waitFor({state:'hidden'});
  assert.equal((await api('/v1/books/browser-novel',adminKey)).data.owner_member_id,'writer');

  await openTransfer().click();
  await ownerDialog.waitFor({state:'visible'});
  await ownerDialog.locator('#book-owner-target').selectOption('moderator');
  await ownerDialog.getByRole('button',{name:'Переназначить'}).click();
  await ownerDialog.waitFor({state:'hidden'});
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('#book-list .library-book-row'))
    .some(r=>r.textContent.includes('Браузерная проверка')
      && r.querySelectorAll('td')[3]?.textContent==='moderator'));
  const transferred=await api('/v1/books/browser-novel',adminKey);
  assert.equal(transferred.data.owner_member_id,'moderator');
  assert.equal(transferred.data.owner_revision,1);

  await openTransfer().click();
  await ownerDialog.waitFor({state:'visible'});
  const concurrentOwner=await api('/v1/books/browser-novel/owner',adminKey,'PUT',{
    owner_member_id:'writer',expected_owner_revision:1,
  });
  assert.equal(concurrentOwner.status,200);
  await ownerDialog.locator('#book-owner-target').selectOption('writer');
  await ownerDialog.getByRole('button',{name:'Переназначить'}).click();
  await ownerDialog.waitFor({state:'hidden'});
  await page.locator('#flash').getByText(/Конфликт версий/).waitFor({timeout:15000});
  await page.waitForFunction(()=>Array.from(document.querySelectorAll('#book-list .library-book-row'))
    .some(r=>r.textContent.includes('Браузерная проверка')
      && r.querySelectorAll('td')[3]?.textContent==='writer'));
  assert.equal((await api('/v1/books/browser-novel',adminKey)).data.owner_revision,2);
  await screenshot(page,'12-book-owner-dialog.png');

  // Book ownership prevents disabling an account until explicit reassignment.
  await page.goto(origin+'/app/team');await login(page,adminKey);
  const row=page.locator('#members-list tr').filter({hasText:'writer'});
  await row.waitFor();
  page.once('dialog',d=>void d.accept());
  await row.getByText('Отключить').click();
  await page.locator('#flash').getByText(/Конфликт версий/).waitFor({timeout:15000});
  assert.equal((await api('/v1/me',issued)).status,200);
  assert.deepEqual(pageErrors,[],'No uncaught browser errors');
  console.log('PASS: real Chromium desktop/mobile, roles, source import and replacement CAS,');
  console.log('      stage abort, memory-only keys, ownership, responsive widths, screenshots.');
  await context.close();
}finally{await browser.close();}
