import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildBatchPayload,batchProgressSummary,batchStatusLabel,
} from './book-batch-view.mjs';

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
