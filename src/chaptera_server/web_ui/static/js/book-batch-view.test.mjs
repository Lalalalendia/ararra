import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildBatchPayload,batchProgressSummary,batchStatusLabel,
} from './book-batch-view.mjs';
import {importedChapterSelection,withDriveConsent,driveStatusLabel,driveFileUrl} from './book-batch-presenter.mjs';

const book={book_id:'novel',glossary_revision:7};
const toc=[
  {chapter_id:'c11',chapter_index:11,source_revision:3},
  {chapter_id:'c3',chapter_index:3,source_revision:1},
  {chapter_id:'c1',chapter_index:1,source_revision:5},
];

test('batch payload pins current source and glossary revisions in numeric chapter order',()=>{
  const result=buildBatchPayload(book,toc,new Set(['c11','c1']),{
    transport:'atoms_v1',maxAttempts:2,
  });
  assert.deepEqual(result.chapters,[
    {chapter_id:'c1',expected_source_revision:5},
    {chapter_id:'c11',expected_source_revision:3},
  ]);
  assert.equal(result.expected_glossary_revision,7);
  assert.equal(result.transport,'atoms_v1');
  assert.equal(result.max_attempts,2);
  assert.equal(result.source_language,'auto');
  assert.equal(result.target_language,'Russian');
  assert.equal(result.style_instructions,'');
});

test('preview cannot submit empty, invalid or oversized selections',()=>{
  assert.throws(()=>buildBatchPayload(book,toc,new Set()),/от 1 до 100/);
  assert.throws(()=>buildBatchPayload({...book,glossary_revision:null},toc,new Set(['c1'])),
    /версию словаря/);
  assert.throws(()=>buildBatchPayload(book,toc,new Set(['c1']),{maxAttempts:11}),
    /от 1 до 10/);
  assert.throws(()=>buildBatchPayload(book,toc,new Set(['c1']),{transport:'other'}),
    /протокол/);
  const damaged=[{chapter_id:'c1',chapter_index:1,source_revision:0}];
  assert.throws(()=>buildBatchPayload(book,damaged,new Set(['c1'])),/ревизии/);
  const long=Array.from({length:101},(_,i)=>({
    chapter_id:'c'+i,chapter_index:i+1,source_revision:1,
  }));
  assert.throws(()=>buildBatchPayload(book,long,new Set(long.map(x=>x.chapter_id))),
    /от 1 до 100/);
});

test('progress aggregates recorded durable job states, not accepted or published text',()=>{
  const summary=batchProgressSummary({
    chapter_count:7,counts:{
      queued:2,running:1,retry_wait:1,completed:2,failed:1,cancelled:0,
    },
  });
  assert.match(summary,/Глав 7/);
  assert.match(summary,/готово 2/);
  assert.match(summary,/ошибки 1/);
  assert.doesNotMatch(summary,/Опубликовано|accepted/);
  assert.equal(batchStatusLabel('completed'),'Переведена (не принята)');
  assert.equal(batchStatusLabel('retry_wait'),'Ожидание повтора');
  assert.equal(batchStatusLabel('untrusted status'),'Неизвестный статус');
});

test('after one uploaded TXT, only chapters in the exact import receipt are selected',()=>{
  const receipt={
    stage_id:'92b6fe77-27e0-4c32-b2b0-e915fb73aa02',
    imported_count:2,
    chapters:[
      {sequence:1,action:'create',chapter_id:'c3'},
      {sequence:2,action:'skip'},
      {sequence:3,action:'replace',chapter_id:'c11'},
    ],
  };
  const extra=[...toc,{chapter_id:'unrelated',chapter_index:2,source_revision:1}];
  const chosen=importedChapterSelection(receipt,extra);
  assert.deepEqual([...chosen].sort(),['c11','c3']);
  const payload=buildBatchPayload(book,extra,chosen);
  assert.deepEqual(payload.chapters,[
    {chapter_id:'c3',expected_source_revision:1},
    {chapter_id:'c11',expected_source_revision:3},
  ]);
  assert.equal(chosen.has('unrelated'),false);
});
test('missing, stale, duplicate or oversized import receipts cannot trigger implicit translation',()=>{
  const one={stage_id:'a7481196-a7d4-48d0-88f2-0a4c4619251e',imported_count:1,
    chapters:[{sequence:1,action:'create',chapter_id:'c3'}]};
  assert.deepEqual([...importedChapterSelection(one,toc)],['c3']);
  assert.throws(()=>importedChapterSelection({...one,stage_id:'bad'},toc),/Квитанция/);
  assert.throws(()=>importedChapterSelection({...one,imported_count:2},toc),/от 1 до 100/);
  assert.throws(()=>importedChapterSelection({...one,chapters:[{sequence:1,action:'create',chapter_id:'deleted'}]},toc),/изменились/);
  assert.throws(()=>importedChapterSelection({...one,imported_count:2,chapters:[one.chapters[0],one.chapters[0]]},toc),/изменились/);
  assert.throws(()=>importedChapterSelection({...one,imported_count:0,chapters:[{action:'skip'}]},toc),/от 1 до 100/);
  const tooMany=Array.from({length:101},(_,i)=>({action:'create',chapter_id:'x'+i}));
  assert.throws(()=>importedChapterSelection({...one,imported_count:101,chapters:tooMany},toc),/от 1 до 100/);
});

test('per-batch Drive request is explicit and is part of the pinned paid submission',()=>{
  const payload={book_id:'novel',expected_manifest_sha256:'a'.repeat(64)};
  assert.deepEqual(withDriveConsent(payload,false),{...payload,drive_export_requested:false});
  assert.deepEqual(withDriveConsent(payload,true),{...payload,drive_export_requested:true});
  assert.equal(payload.drive_export_requested,undefined,'do not mutate original manifest');
  assert.throws(()=>withDriveConsent(payload,undefined),/explicit/);
});
test('Drive delivery is distinct from completed translation job',()=>{
  const batch={chapter_count:2,counts:{completed:2},drive_export_requested:true,
    drive_counts:{exported:1,awaiting_exporter:1}};
  assert.match(batchProgressSummary(batch),/На Google Drive 1\/2/);
  assert.equal(driveStatusLabel('awaiting_exporter'),'Ожидает выгрузки');
  assert.equal(driveStatusLabel('exported'),'На Google Drive');
  assert.equal(driveStatusLabel('requires_review'),'Нужна проверка');
  assert.equal(driveStatusLabel('not_requested'),'Не запрошено');
});

test('only a verified Drive receipt identifier may produce an external link',()=>{
  assert.equal(driveFileUrl('abc'),null);
  assert.equal(driveFileUrl('javascript:alert(1)'),null);
  assert.equal(driveFileUrl('a'.repeat(260)),null);
  assert.equal(driveFileUrl('offline-translation-12345678'),
    'https://drive.google.com/file/d/offline-translation-12345678/view');
});
