/** Real existing UI in Chromium, all requests handled by the Rust preview.
 * No page.route().fulfill, fake backend, injected CSRF or programmatic UI writes.
 * The Python launcher creates only a fixture book/source via Rust HTTP first.
 */
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright';

const origin=process.env.READ_E2E_BASE_URL;
assert.equal(origin,'http://127.0.0.1:18492','test may only target disposable loopback');
const adminKey=process.env.READ_E2E_ADMIN_KEY;
assert.ok(adminKey);
const output=fileURLToPath(new URL('./artifacts/rust-ui/',import.meta.url));
mkdirSync(output,{recursive:true});
const endpoints=[],pageErrors=[],unexpected=[],headerChecks=[];
const privateDraft='Private working draft not for export.';
const privateNote='Private editorial note not for export.';
const firstAccepted='First accepted translation from real Chromium.';
const secondAccepted='Second accepted translation from real Chromium.';

async function login(page,key){
  await page.waitForFunction(()=>document.body?.dataset.authReady==='1');
  await page.locator('#login-secret').fill(key);
  await page.locator('#login-form button[type=submit]').click();
  await page.locator('#authenticated').waitFor({state:'visible'});
  assert.equal(await page.locator('#login-secret').inputValue(),'');
}
async function choose(page,name){
  await page.locator(`.sidebar-nav a[data-page-link="${name}"]`).click();
  await page.locator(`[data-page="${name}"]`).waitFor({state:'visible'});
}
async function action(page,method,suffix,click,expected=200){
  const pending=page.waitForResponse(response=>{
    const url=new URL(response.url());
    return url.origin===origin && url.pathname.endsWith(suffix)
      && response.request().method()===method;
  });
  await click();
  const response=await pending;
  assert.equal(response.status(),expected,`${method} ${suffix}`);
  return response;
}
async function screenshot(page,name){
  assert.equal(await page.locator('#member-secret').isVisible(),false);
  assert.equal(await page.locator('#login-secret').inputValue(),'');
  await page.screenshot({path:output+name,fullPage:true,animations:'disabled'});
}
function observe(context){
  context.on('page',page=>page.on('pageerror',error=>pageErrors.push(error.name)));
  context.on('request',request=>{
    if(!request.url().startsWith(origin+'/'))return;
    const path=new URL(request.url()).pathname;
    endpoints.push({method:request.method(),path});
    headerChecks.push(request.allHeaders().then(headers=>{
      assert.equal(headers['x-api-key'],undefined,'browser must authenticate with cookie, not injected API key');
    }));
  });
  context.on('response',response=>{
    if(!response.url().startsWith(origin+'/'))return;
    const path=new URL(response.url()).pathname;
    if((path.startsWith('/v1/')||path.startsWith('/app-static/')) && response.status()>=400
       && !(path==='/v1/session' && response.status()===401)){
      unexpected.push({path,status:response.status()});
    }
  });
}
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--no-sandbox']});
try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'ru-RU',acceptDownloads:true});
  observe(context);
  await context.route('**/*',route=>{
    const url=route.request().url();
    if(url.startsWith(origin+'/')||url.startsWith('blob:'))return route.continue();
    unexpected.push({path:'external-request-blocked',status:0});
    return route.abort();
  });
  const page=await context.newPage();page.setDefaultTimeout(15000);
  const loaded=await page.goto(origin+'/app/overview');
  assert.equal(loaded.status(),200);
  assert.equal(loaded.headers()['x-readera-runtime'],'rust-ui-preview');
  assert.ok(loaded.headers()['content-security-policy'].includes("default-src 'none'"));
  await page.waitForFunction(()=>document.body?.dataset.authReady==='1');
  assert.equal(await page.locator('#authenticated').isVisible(),false);
  assert.equal(await page.locator('.sidebar').isVisible(),false);
  await login(page,adminKey);
  await choose(page,'team');
  await page.locator('#member-invite [name=member_id]').fill('ui-editor');
  await action(page,'POST','/v1/admin/members',()=>page.locator('#member-invite button[type=submit]').click(),201);
  await page.locator('#member-secret').waitFor({state:'visible'});
  const editorKey=await page.locator('#member-secret-code').innerText();
  assert.ok(editorKey.startsWith('re_'));
  await page.locator('#member-secret-hide').click();
  await action(page,'POST','/v1/session/logout',()=>page.locator('#logout').click(),204);
  await page.locator('#login-form').waitFor({state:'visible'});
  await login(page,editorKey);
  await choose(page,'books');
  await page.locator('#book-list .library-book-row').waitFor();
  assert.equal(await page.locator('#rust-preview-warning').isVisible(),true);
  assert.equal(await page.locator('.sidebar-nav [data-page-link=import]').getAttribute('aria-disabled'),'true');
  assert.equal(await page.locator('#book-list button').nth(3).isDisabled(),true);
  await screenshot(page,'01-books.png');

  await page.locator('#book-list .library-book-row button').nth(1).click();
  await page.locator('#editor-workspace').waitFor({state:'visible'});
  await page.locator('#editor-history-list p').waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('#editor-qa-summary')?.textContent.includes('Rust'));
  assert.equal(await page.locator('#editor-qa-scan').isDisabled(),true);
  assert.ok((await page.locator('#editor-source').innerText()).includes('Original source'));
  await page.locator('#editor-text').fill(privateDraft);
  await action(page,'PUT','/chapters/c1/draft',()=>page.locator('#editor-save-draft').click());
  await page.waitForFunction(()=>document.querySelector('#editor-draft-rev')?.textContent==='rev 1');
  await page.locator('#editor-note-body').fill(privateNote);
  await action(page,'POST','/chapters/c1/notes',()=>page.locator('#editor-note-form button[type=submit]').click(),201);
  await page.locator('#editor-notes-list article').waitFor();
  const noteChange=page.waitForResponse(r=>r.request().method()==='PATCH' && new URL(r.url()).pathname.includes('/notes/'));
  await page.locator('#editor-notes-list article button').click();
  assert.equal((await noteChange).status(),200);
  await page.locator('#editor-text').fill(firstAccepted);
  const accepted=await action(page,'PUT','/chapters/c1/accepted-translation',()=>page.locator('#editor-accept').click());
  assert.equal((await accepted.json()).accepted_revision,1);
  await page.waitForFunction(()=>document.querySelector('#editor-accepted-rev')?.textContent==='rev 1');
  await page.locator('#editor-draft-stale').waitFor({state:'visible'});
  await screenshot(page,'02-editor.png');
  // The dropdown changes before the new chapter GET completes. Wait for
  // actual chapter/draft/history responses and the rendered chapter identity,
  // otherwise typing can race displayChapter() and be replaced by source load.
  const chapterLoaded=page.waitForResponse(r=>r.request().method()==='GET'
    &&new URL(r.url()).pathname==='/v1/books/m1k-book/chapters/c2');
  const draftLoaded=page.waitForResponse(r=>r.request().method()==='GET'
    &&new URL(r.url()).pathname==='/v1/books/m1k-book/chapters/c2/draft');
  const historyLoaded=page.waitForResponse(r=>r.request().method()==='GET'
    &&new URL(r.url()).pathname==='/v1/books/m1k-book/chapters/c2/accepted-revisions');
  await page.locator('#editor-next').click();
  for(const response of await Promise.all([chapterLoaded,draftLoaded,historyLoaded])){
    assert.equal(response.status(),200,'next chapter loading failed');
  }
  await page.waitForFunction(()=>document.querySelector('#editor-chapter')?.value==='c2'
    &&document.querySelector('#editor-info')?.textContent.includes('c2')
    &&document.querySelector('#editor-accepted-rev')?.textContent==='rev 0'
    &&document.querySelector('#editor-draft-stale')?.hidden===true
    &&document.querySelector('#editor-text')?.value==='');
  await page.locator('#editor-text').fill(secondAccepted);
  await action(page,'PUT','/chapters/c2/accepted-translation',()=>page.locator('#editor-accept').click());
  await page.waitForFunction(()=>document.querySelector('#editor-accepted-rev')?.textContent==='rev 1');
  await choose(page,'books');
  await page.waitForFunction(()=>document.querySelector('#book-list')?.textContent.includes('2 / 2'));
  // Browser reload must restore the real HttpOnly cookie without re-entering the key.
  await page.reload();
  await page.locator('#authenticated').waitFor({state:'visible'});
  await page.waitForFunction(()=>document.querySelector('#book-list')?.textContent.includes('2 / 2'));
  const cookies=await context.cookies();
  assert.ok(cookies.some(c=>c.name==='readera_session'&&c.httpOnly&&c.sameSite==='Strict'));
  assert.equal(await page.evaluate(()=>document.cookie.includes('readera_session')),false);
  assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);

  await page.locator('#book-list .library-book-row button').nth(2).click();
  await page.locator('#book-export-dialog').waitFor({state:'visible'});
  await page.locator('#book-export-last').fill('2');
  await action(page,'GET','/export/preview',()=>page.locator('#book-export-preview').click());
  await page.waitForFunction(()=>!document.querySelector('#book-export-txt')?.disabled);
  const hashes={};
  for(const format of ['txt','html']){
    const pending=page.waitForEvent('download');
    const responsePending=page.waitForResponse(r=>new URL(r.url()).pathname.endsWith('/export')&&new URL(r.url()).searchParams.get('format')===format);
    await page.locator('#book-export-'+format).click();
    const response=await responsePending;assert.equal(response.status(),200);
    const download=await pending;
    assert.equal(await download.failure(),null);
    const filename=output+'accepted.'+format;
    await download.saveAs(filename);
    const data=readFileSync(filename),text=data.toString('utf8');
    assert.ok(text.includes(firstAccepted)&&text.includes(secondAccepted));
    for(const excluded of [privateDraft,privateNote,'Original source'])assert.ok(!text.includes(excluded));
    hashes[format]=createHash('sha256').update(data).digest('hex');
    assert.equal(response.headers()['x-readera-content-sha256'],hashes[format]);
    assert.match(response.headers()['x-readera-manifest-sha256'],/^[a-f0-9]{64}$/);
  }
  await screenshot(page,'03-export.png');
  await page.keyboard.press('Escape');
  await action(page,'POST','/v1/session/logout',()=>page.locator('#logout').click(),204);
  await page.locator('#login-form').waitFor({state:'visible'});
  await page.reload();
  await page.waitForFunction(()=>document.body?.dataset.authReady==='1');
  assert.equal(await page.locator('#authenticated').isVisible(),false);
  await context.close();

  const mobile=await browser.newContext({viewport:{width:390,height:844},locale:'ru-RU'});
  observe(mobile);
  const small=await mobile.newPage();
  await small.goto(origin+'/app/books');await login(small,editorKey);
  await small.locator('#book-list .library-book-row').waitFor();
  const [width,viewport]=await small.evaluate(()=>[document.documentElement.scrollWidth,document.documentElement.clientWidth]);
  assert.ok(width<=viewport+2,'mobile page overflows horizontally');
  await screenshot(small,'04-mobile-books.png');
  await action(small,'POST','/v1/session/logout',()=>small.locator('#logout').click(),204);
  await small.locator('#login-form').waitFor({state:'visible'});
  await mobile.close();
  await Promise.all(headerChecks);
  assert.deepEqual(pageErrors,[],'browser JS errors');
  assert.deepEqual(unexpected,[],'unexpected HTTP failures or external requests');
  assert.ok(endpoints.some(e=>e.path.endsWith('/accepted-translation')&&e.method==='PUT'));
  assert.ok(!endpoints.some(e=>e.path.endsWith('/qa')),'unimplemented QA must never be presented as passed');
  writeFileSync(output+'evidence.json',JSON.stringify({
    status:'PASS',backend:'Rust only',ui:'canonical existing shell and JS; explicit QA capability adapter',
    fixture_creation:'Rust HTTP before browser; NOT UI import',
    limitations:['import','QA','quotas','Gemini','full HTML grammar','live HTTPS/Nginx'],
    downloads:hashes,endpoints,pageErrors,unexpected,
  },null,2));
  console.log('rust_chromium_login_team_edit_draft_notes_accept_download_logout=PASS');
  console.log('rust_chromium_cookie_restore_mobile_headers_and_no_secret_storage=PASS');
  console.log('python_http_api_used=NO');
}finally{await browser.close();}
