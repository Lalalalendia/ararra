import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createDraft, changeAction, destinationEdited, verifiedReplacement,
  validateDraft, confirmation, importWarningText, sourceChapterLabel,
  sourceArchiveSummary,
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

test('one uploaded unnumbered TXT becomes one suggested destination, not a fabricated author number',()=>{
  const p=preview([null]);
  p.chapters[0].title='0010-15869792';
  p.chapters[0].suggested_index=1; // Comes from the actual Python preview.
  const [row]=createDraft(p);
  assert.equal(row.title,'0010-15869792');
  assert.equal(row.detected_number,null);
  assert.equal(row.author_number,null);
  assert.equal(row.provisional_assignment,true);
  assert.equal(row.chapter_id,'1');
  assert.equal(row.chapter_index,'1');
  assert.equal(row.chapter_path,'chapters/1.xhtml');
  assert.deepEqual(validateDraft([row],newBook,stage),[]);
  assert.equal(confirmation([row],newBook,stage).chapters[0].chapter_index,1);
  assert.ok(validateDraft([row],existing,stage).some(s=>s.includes('подтвердите номер')));
  const edited=destinationEdited(row,'chapter_index','10');
  assert.equal(edited.provisional_assignment,false);
  assert.equal(edited.chapter_id,'10');
  assert.equal(edited.chapter_path,'chapters/10.xhtml');
  assert.equal(edited.author_number,null);
  assert.deepEqual(validateDraft([edited],existing,stage),[]);
});

test('one TXT with multiple normal chapters proposes distinct destinations without changing author numbers',()=>{
  const p=preview([null,2,null]);
  p.chapters.forEach((c,i)=>{c.suggested_index=i+1;});
  const rows=createDraft(p);
  assert.deepEqual(rows.map(r=>r.chapter_id),['1','2','3']);
  assert.deepEqual(rows.map(r=>r.chapter_index),['1','2','3']);
  assert.deepEqual(rows.map(r=>r.chapter_path),[
    'chapters/1.xhtml','chapters/2.xhtml','chapters/3.xhtml',
  ]);
  assert.deepEqual(rows.map(r=>r.author_number),[null,2,null]);
  assert.deepEqual(rows.map(r=>r.provisional_assignment),[true,false,true]);
  assert.deepEqual(validateDraft(rows,newBook,stage),[]);
});

test('do not suggest a destination overlapping an explicit chapter or another suggestion',()=>{
  const p=preview([1,null,3,null]);
  p.chapters[1].suggested_index=3; // Conflicts with the explicit chapter 3.
  p.chapters[3].suggested_index=4;
  const rows=createDraft(p);
  assert.equal(rows[1].chapter_index,'');
  assert.equal(rows[3].chapter_index,'4');
  assert.ok(validateDraft(rows,newBook,stage).some(s=>s.includes('ID и номер')));
  const noUnique=preview([null,null]);
  noUnique.chapters[0].suggested_index=1;
  noUnique.chapters[1].suggested_index=1;
  const duplicate=createDraft(noUnique);
  assert.equal(duplicate[0].chapter_index,'1');
  assert.equal(duplicate[1].chapter_index,'');
});

test('service/extra entries never auto-assign even when preview includes position hints',()=>{
  const p=preview([null,null]);
  p.chapters[0].entry_type='service';
  p.chapters[1].entry_type='extra';
  p.chapters.forEach((c,i)=>{c.suggested_index=i+1;});
  const rows=createDraft(p);
  assert.deepEqual(rows.map(r=>r.action),['review','review']);
  assert.deepEqual(rows.map(r=>r.chapter_index),['','']);
  assert.ok(validateDraft(rows,newBook,stage).some(s=>s.includes('выберите действие')));
});


test('manually edited chapter IDs and paths are never overwritten by later number edits',()=>{
  const p=preview([null]);
  p.chapters[0].suggested_index=1;
  let row=createDraft(p)[0];
  row=destinationEdited(row,'chapter_id','custom');
  row=destinationEdited(row,'chapter_path','volumes/custom.xhtml');
  row=destinationEdited(row,'chapter_index','10');
  assert.equal(row.provisional_assignment,false);
  assert.equal(row.chapter_id,'custom');
  assert.equal(row.chapter_path,'volumes/custom.xhtml');
  assert.deepEqual(validateDraft([row],newBook,stage),[]);
});

test('source labels and warning translations preserve original numbering',()=>{
  assert.match(importWarningText('Some chapters have no explicit number: sequence numbers are suggestions only'),/Предложенные номера/);
  assert.equal(importWarningText('Other note'),'Other note');
  assert.equal(sourceChapterLabel({detected_number:null,provisional_assignment:true,chapter_index:'1'}),'Без авторского номера · предложена позиция 1');
  assert.equal(sourceChapterLabel({detected_number:null,provisional_assignment:false}),'Без авторского номера');
  assert.equal(sourceChapterLabel({detected_number:10}),'Авторский № 10');
});


test('Full Cycle ZIP keeps manifest positions and original-versus-working proof',()=>{
  const archive=preview([null,null,null]);
  archive.source_archive={
    boundaries:'manifest',source_domain:'source.example',
    source_url:'https://source.example/book/77',
    missing_positions:[[8,8],[10,12]],
  };
  const indexes=[7,9,13];
  archive.chapters.forEach((c,i)=>{
    c.destination_position=indexes[i];
    c.catalog_ordinal=indexes[i];
    c.source_url='https://source.example/chapter/'+indexes[i];
    c.source_original_sha256='a'.repeat(64);
    c.source_working_sha256=(i===1?'b':'a').repeat(64);
    c.title='Повторяющееся название';
  });
  const rows=createDraft(archive);
  assert.deepEqual(rows.map(r=>r.chapter_index),['7','9','13']);
  assert.deepEqual(rows.map(r=>r.chapter_id),['7','9','13']);
  assert.deepEqual(rows.map(r=>r.detected_number),[null,null,null]);
  assert.equal(rows[1].source_url,'https://source.example/chapter/9');
  assert.equal(rows[1].source_original_sha256,'a'.repeat(64));
  assert.equal(rows[1].source_working_sha256,'b'.repeat(64));
  assert.deepEqual(validateDraft(rows,newBook,stage),[]);
  assert.deepEqual(sourceArchiveSummary(archive),{
    domain:'source.example',
    sourceUrl:'https://source.example/book/77',
    chapterCount:3,
    modifiedCount:1,
    gapsLabel:'8, 10–12',
    gapCount:2,
    completenessWarning:'Архив содержит только скачанные главы. Отсутствие пропусков внутри ZIP не доказывает, что весь сайт собран.',
  });
});
test('archive provenance is absent for ordinary TXT and gap lists are bounded',()=>{
  assert.equal(sourceArchiveSummary(preview([1])),null);
  const archive=preview([null]);
  archive.source_archive={boundaries:'manifest',missing_positions:
    Array.from({length:15},(_,i)=>[i*3+2,i*3+2])};
  const summary=sourceArchiveSummary(archive);
  assert.equal(summary.gapCount,15);
  assert.match(summary.gapsLabel,/ещё 7 диапазонов/);
  assert.equal(summary.domain,'Не указан');
  assert.match(importWarningText(
    'Archive has internal gaps in source indexes; verify missing chapters'
  ),/пропуски/);
  assert.match(importWarningText(
    'Archive includes collected chapters only; full source TOC coverage is not proven'
  ),/Полнота оглавления сайта не подтверждена/);
});
