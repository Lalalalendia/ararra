import test from 'node:test';
import assert from 'node:assert/strict';
import {
  renderArchiveDetails, renderArchiveRowDetails, renderImportReviewSummary,
} from './import-archive-review-view.mjs';

function node(tag='div') {
  return {
    tag,children:[],hidden:true,checked:true,
    append(child){this.children.push(child);},
    replaceChildren(){this.children=[];},
  };
}
function setup() {
  const widgets=new Map();
  const $=id=>{
    if(!widgets.has(id))widgets.set(id,node());
    return widgets.get(id);
  };
  const text=(parent,value,tag='span')=>{
    const child={tag,content:String(value)};
    parent.append(child);
    return child;
  };
  return {$,text};
}
function preview() {
  return {
    chapters:[
      {sequence:1,source_original_sha256:'a'.repeat(64),source_working_sha256:'a'.repeat(64)},
      {sequence:2,source_original_sha256:'a'.repeat(64),source_working_sha256:'b'.repeat(64)},
    ],
    source_archive:{
      boundaries:'manifest',
      source_domain:'books.example',
      source_url:'https://books.example/book/2',
      missing_positions:[[4,5]],
    },
  };
}

test('Full Cycle ZIP displays provenance and resets additional acknowledgement',()=>{
  const {$,text}=setup();
  const details=$('import-archive-details');
  renderArchiveDetails({preview:preview()},$,text);
  assert.equal(details.hidden,false);
  assert.equal($('import-archive-ack-wrap').hidden,false);
  assert.equal($('import-archive-ack').checked,false);
  const lines=details.children.map(x=>x.content).join(' ');
  assert.match(lines,/manifest.json/);
  assert.match(lines,/books.example/);
  assert.match(lines,/2 глав/);
  assert.match(lines,/1 глав/);
  assert.match(lines,/4–5/);
  assert.match(lines,/не доказывает/);
  $('import-archive-ack').checked=true;
  renderArchiveDetails({preview:{chapters:[]}},$,text);
  assert.equal(details.hidden,true);
  assert.deepEqual(details.children,[]);
  assert.equal($('import-archive-ack-wrap').hidden,true);
  assert.equal($('import-archive-ack').checked,false);
});
test('chapter provenance and hashes are plain text, no link/HTML injection',()=>{
  const {$,text}=setup(),info=node();
  const row={
    catalog_ordinal:9,source_url:'https://source.example/<img src=x>',
    source_original_sha256:'a'.repeat(64),
    source_working_sha256:'b'.repeat(64),
  };
  renderArchiveRowDetails(row,info,text,(url,n)=>url.slice(0,n));
  assert.equal(info.children.length,2);
  assert.ok(info.children.every(child=>child.tag==='small'));
  assert.match(info.children[0].content,/<img/);
  assert.match(info.children[1].content,/исправлена/);
  assert.ok(!info.children.some(child=>child.tag==='a'));
  const none=node();
  renderArchiveRowDetails({},none,text,x=>x);
  assert.equal(none.children.length,0);
});
test('review shows archive gaps only for ZIP, ordinary TXT flow unchanged',()=>{
  const {$,text}=setup(),root=node();
  const previous=globalThis.document;
  globalThis.document={createElement:tag=>node(tag)};
  try{
    const stage={preview:preview(),unassigned_prefix_saved:false,file_sha256:'f'.repeat(64)};
    const target={book_id:'book',book_mode:'create',prefix_policy:'reject'};
    const rows=[{action:'create'},{action:'skip'}];
    renderImportReviewSummary(root,rows,target,stage,text);
    const entries=root.children.map(ch=>ch.children.map(part=>part.content).join(''));
    assert.ok(entries.some(line=>line.includes('ZIP Full Cycle App: books.example')));
    assert.ok(entries.some(line=>line.includes('Пропуски позиций: 4–5')));
    assert.ok(entries.some(line=>line.includes('Полнота сайта: Не подтверждена')));
    stage.preview={chapters:[]};
    renderImportReviewSummary(root,rows,target,stage,text);
    const basic=root.children.map(ch=>ch.children.map(part=>part.content).join(''));
    assert.ok(!basic.some(line=>line.includes('ZIP Full Cycle App')));
    assert.ok(basic.some(line=>line.includes('Создать глав: 1')));
  }finally{
    globalThis.document=previous;
  }
});
