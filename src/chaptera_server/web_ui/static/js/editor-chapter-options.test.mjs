import test from 'node:test';
import assert from 'node:assert/strict';
import {chapterOptionLabel,syncChapterOption} from './editor-chapter-options.mjs';

const source={book_id:'book',chapter_id:'c1',chapter_index:1,source_revision:1,
  accepted_revision:0,accepted_source_revision:0,accepted_current:false};
const accepted={...source,accepted_revision:1,accepted_source_revision:1,accepted_current:true};
function fixture(){
  const toc=[{...source},{...source,chapter_id:'c2',chapter_index:2}];
  const options=toc.map(row=>({value:row.chapter_id,textContent:chapterOptionLabel(row)}));
  for(const item of options)Object.defineProperty(item,'innerHTML',{
    set(){throw Error('Option labels must never render HTML');},
  });
  return {toc,select:{value:'c1',options},first:options[0],second:options[1]};
}

test('labels use authoritative accepted_revision and accepted_current',()=>{
  assert.equal(chapterOptionLabel(source),'\u0413\u043b\u0430\u0432\u0430 1 \u00b7 c1 \u00b7 \u0438\u0441\u0445\u043e\u0434\u043d\u0438\u043a');
  assert.equal(chapterOptionLabel(accepted),'\u0413\u043b\u0430\u0432\u0430 1 \u00b7 c1 \u00b7 \u043f\u0440\u0438\u043d\u044f\u0442\u043e');
  assert.equal(chapterOptionLabel({...accepted,accepted_current:false}),
    '\u0413\u043b\u0430\u0432\u0430 1 \u00b7 c1 \u00b7 \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u043e');
});

test('source -> accepted -> stale source -> reaccepted updates one existing node',()=>{
  const f=fixture(), other=f.second.textContent;
  for(const data of [accepted,{...accepted,source_revision:2,accepted_current:false},
    {...accepted,source_revision:2,accepted_source_revision:2,accepted_revision:2}]){
    assert.equal(syncChapterOption('book',f.toc,f.select,data),true);
    assert.equal(f.first.textContent,chapterOptionLabel(data));
    assert.equal(f.toc[0].accepted_revision,data.accepted_revision);
    assert.equal(f.toc[0].source_revision,data.source_revision);
    assert.equal(f.toc[0].accepted_current,data.accepted_current);
    assert.equal(f.select.options[0],f.first,'do not rebuild selected node');
    assert.equal(f.select.value,'c1','do not change selected chapter');
    assert.equal(f.second.textContent,other,'do not relabel a different chapter');
  }
});

test('history restore and reread are idempotent and do not retain literary text',()=>{
  const f=fixture(),data={...accepted,accepted_revision:3,accepted_html:'private',source_html:'source'};
  syncChapterOption('book',f.toc,f.select,data);
  const before=JSON.stringify(f);
  syncChapterOption('book',f.toc,f.select,data);
  assert.equal(JSON.stringify(f),before);
  assert.equal(f.toc[0].accepted_revision,3);
  assert.equal(f.toc[0].accepted_html,undefined);
  assert.equal(f.toc[0].source_html,undefined);
});

test('a late response for the same chapter ID in a different book changes nothing',()=>{
  const f=fixture(),before=JSON.stringify(f);
  assert.equal(syncChapterOption('other-book',f.toc,f.select,accepted),false);
  assert.equal(JSON.stringify(f),before);
});

test('unknown or removed chapter options never mutate cached metadata',()=>{
  const f=fixture(),before=JSON.stringify(f);
  assert.equal(syncChapterOption('book',f.toc,f.select,{...accepted,chapter_id:'missing'}),false);
  assert.equal(JSON.stringify(f),before);
  f.select.options=[];
  assert.equal(syncChapterOption('book',f.toc,f.select,accepted),false);
  assert.deepEqual(f.toc[0],source);
});

test('updating a nonselected row never changes selection or neighbouring labels',()=>{
  const f=fixture(),before=f.first.textContent;
  const data={...accepted,chapter_id:'c2',chapter_index:2};
  syncChapterOption('book',f.toc,f.select,data);
  assert.equal(f.select.value,'c1');
  assert.equal(f.first.textContent,before);
  assert.equal(f.second.textContent,chapterOptionLabel(data));
});

test('special chapter identifiers remain literal text, without selector interpolation',()=>{
  const f=fixture(),id='c1\"<img src=x onerror=x>[name]';
  f.toc[0].chapter_id=id;f.first.value=id;
  assert.equal(syncChapterOption('book',f.toc,f.select,{...accepted,chapter_id:id}),true);
  assert.ok(f.first.textContent.includes(id));
});
