import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createDraft, changeAction, destinationEdited, verifiedReplacement,
  validateDraft, confirmation,
} from './import-model.mjs';

const preview = detected => ({
  chapters:detected.map((n,i)=>({
    sequence:i+1,title:'Глава '+(i+1),sample:'Текст оригинала',
    detected_number:n,characters:31,
  })),
});
const stage={expires_at:Date.now()/1000+1800,unassigned_prefix_saved:false};
const newBook={book_mode:'create',book_id:'x',title:'Книга',owner_member_id:'mod',prefix_policy:'reject'};
const existing={book_mode:'existing',book_id:'x',title:'',owner_member_id:null,prefix_policy:'reject'};

test('duplicate and missing original chapter numbers are not guessed',()=>{
  const rows=createDraft(preview([1,1,null,3]));
  for(const r of rows.slice(0,3)){
    assert.equal(r.chapter_id,'');assert.equal(r.chapter_index,'');assert.equal(r.chapter_path,'');
  }
  assert.equal(rows[3].chapter_id,'3');
  assert.ok(validateDraft(rows,newBook,stage).some(x=>x.includes('ID и номер')));
});
test('skip is explicit; all skipped is rejected',()=>{
  const rows=createDraft(preview([1,2]));
  rows[1]=changeAction(rows[1],'skip');
  assert.equal(confirmation(rows,newBook,stage).chapters[1].action,'skip');
  assert.equal('chapter_id' in confirmation(rows,newBook,stage).chapters[1],false);
  assert.deepEqual(validateDraft(rows,newBook,stage),[]);
  const empty=rows.map(r=>changeAction(r,'skip'));
  assert.ok(validateDraft(empty,newBook,stage).some(x=>x.includes('хотя бы')));
});
test('replace requires live revision for exactly the right book and chapter',()=>{
  let row=changeAction(createDraft(preview([8]))[0],'replace');
  assert.equal(row.expected_revision,null);
  assert.ok(validateDraft([row],existing,stage).some(x=>x.includes('ревизию')));
  row=verifiedReplacement(row,'x',{chapter_id:'8',chapter_index:8,chapter_path:'chapters/8.xhtml',source_revision:12});
  assert.deepEqual(validateDraft([row],existing,stage),[]);
  assert.equal(confirmation([row],existing,stage).chapters[0].expected_revision,12);
  assert.ok(validateDraft([row],{...existing,book_id:'y'},stage).some(x=>x.includes('ревизию')));
  const edited=destinationEdited(row,'chapter_index','9');
  assert.equal(edited.expected_revision,null);
  assert.ok(validateDraft([edited],existing,stage).some(x=>x.includes('ревизию')));
});
test('reject duplicate ID, chapter number and normalized path',()=>{
  const rows=createDraft(preview([1,2]));
  rows[1]=destinationEdited(rows[1],'chapter_path',rows[0].chapter_path);
  assert.ok(validateDraft(rows,newBook,stage).some(x=>x.includes('дубликат')));
  rows[1]=destinationEdited(rows[1],'chapter_path','../unsafe');
  assert.ok(validateDraft(rows,newBook,stage).some(x=>x.includes('безопасный')));
});
test('reject expired stage, empty title and missing moderator owner',()=>{
  const rows=createDraft(preview([1]));
  assert.ok(validateDraft(rows,newBook,{...stage,expires_at:1}).some(x=>x.includes('истекла')));
  assert.ok(validateDraft(rows,{...newBook,title:'',owner_member_id:''},stage).some(x=>x.includes('ответственного')));
});
test('preface is never silently lost',()=>{
  const rows=createDraft(preview([1]));
  const prefaced={...stage,unassigned_prefix_saved:true};
  assert.ok(validateDraft(rows,newBook,prefaced).some(x=>x.includes('предисловию')));
  assert.equal(validateDraft(rows,{...newBook,prefix_policy:'preserve'},prefaced).length,0);
  assert.equal(confirmation(rows,{...newBook,prefix_policy:'discard'},prefaced).prefix_policy,'discard');
});


test('MED auxiliary and service entries cannot be silently imported or skipped',()=>{
  const snapshot=preview([1,null,null,3]);
  snapshot.chapters[1].entry_type='extra';
  snapshot.chapters[2].entry_type='service';
  snapshot.chapters[1].catalog_ordinal=2;
  const rows=createDraft(snapshot);
  assert.equal(rows[0].action,'create');
  assert.equal(rows[1].action,'review');
  assert.equal(rows[2].action,'review');
  assert.equal(rows[1].entry_type,'extra');
  assert.equal(rows[1].catalog_ordinal,2);
  assert.equal(rows[1].chapter_id,'');
  assert.ok(validateDraft(rows,newBook,stage).some(x=>x.includes('выберите действие')));
  assert.throws(()=>confirmation(rows,newBook,stage),/Unreviewed/);
  rows[1]=changeAction(rows[1],'skip');
  rows[2]=changeAction(rows[2],'create');
  assert.ok(validateDraft(rows,newBook,stage).some(x=>x.includes('ID и номер')));
  for(const [field,value] of [['chapter_id','announcement'],['chapter_index','99'],
                              ['chapter_path','chapters/announcement.xhtml']]){
    rows[2]=destinationEdited(rows[2],field,value);
  }
  assert.deepEqual(validateDraft(rows,newBook,stage),[]);
  assert.deepEqual(confirmation(rows,newBook,stage).chapters[1],{sequence:2,action:'skip'});
  assert.equal(confirmation(rows,newBook,stage).chapters[2].chapter_index,99);
});

test('ordered source positions are suggested without renumbering original chapter identity',()=>{
  const original=preview([7,1,3]);
  original.chapters.forEach((c,i)=>{c.destination_position=i+1;});
  const rows=createDraft(original);
  assert.deepEqual(rows.map(x=>x.detected_number),[7,1,3]);
  assert.deepEqual(rows.map(x=>x.chapter_index),['1','2','3']);
  assert.deepEqual(rows.map(x=>x.chapter_id),['1','2','3']);
  assert.deepEqual(validateDraft(rows,newBook,stage),[]);
});

test('ordered position never auto-confirms a service record',()=>{
  const snapshot=preview([7,null,3]);
  snapshot.chapters.forEach((c,i)=>{c.destination_position=i+1;});
  snapshot.chapters[1].entry_type='service';
  const rows=createDraft(snapshot);
  assert.deepEqual(rows.map(r=>r.detected_number),[7,null,3]);
  assert.deepEqual(rows.map(r=>r.chapter_index),['1','','3']);
  assert.equal(rows[1].action,'review');
  assert.deepEqual(rows.map(r=>r.chapter_id),['1','','3']);
  assert.ok(validateDraft(rows,newBook,stage).some(s=>s.includes('выберите действие')));
  assert.throws(()=>confirmation(rows,newBook,stage),/Unreviewed/);
});
