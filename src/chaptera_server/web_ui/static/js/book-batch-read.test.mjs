import test from 'node:test';
import assert from 'node:assert/strict';
import {createBatchReads,nextBatchRefreshDelay} from './book-batch-read.mjs';

function batch({pending=0,drive=null,consent=false,failed=0}={}){
  const counts={completed:pending?0:1,queued:pending,running:0,retry_wait:0,failed};
  return {batch_id:'batch-001',chapter_count:1,counts,
    drive_export_requested:consent,drive_counts:drive||{},chapters:[]};
}
test('only pending translation or consented Drive work needs automatic refresh',()=>{
  assert.equal(nextBatchRefreshDelay(batch({pending:1})),15000);
  assert.equal(nextBatchRefreshDelay(batch({consent:true,drive:{uploading:1}})),60000);
  assert.equal(nextBatchRefreshDelay(batch({consent:true,drive:{awaiting_exporter:1}})),60000);
  assert.equal(nextBatchRefreshDelay(batch({consent:true,drive:{exported:1}})),null);
  assert.equal(nextBatchRefreshDelay(batch({consent:true,drive:{requires_review:1}})),null);
  assert.equal(nextBatchRefreshDelay(batch({consent:false,drive:{awaiting_exporter:1}})),null);
  assert.equal(nextBatchRefreshDelay(batch({failed:1})),null);
  assert.equal(nextBatchRefreshDelay({batch_id:'',chapter_count:0}),null);
});
test('polls one durable batch at a time, stops on delivery and requires no write requests',async()=>{
  let visible=true,ticket=1,opened=true,handles=[],canceled=[],renders=[],calls=[];
  const book={book_id:'novel'};
  let pending=batch({pending:1});
  let onVisible;
  const reads=createBatchReads({
    getActive:()=>opened?book:null,getTicket:()=>ticket,
    activeFor:(t,b)=>opened&&t===ticket&&b===book,
    path:()=>'/v1/books/novel/translation-batches',
    request:async(path)=>{calls.push(path);return pending;},
    historyList:{replaceChildren(){}},progressPanel:{},progressTitle:{},
    progressSummary:{textContent:''},progressBody:{},progressRefresh:{},
    showError:error=>{throw error},
    render:value=>renders.push(value),
    scheduleTimer:(fn,ms)=>{const item={fn,ms};handles.push(item);return item;},
    cancelTimer:handle=>canceled.push(handle),
    isVisible:()=>visible,onVisibilityChange:fn=>{onVisible=fn;},
  });
  reads.renderProgress(pending);
  assert.equal(handles.length,1);
  assert.equal(handles.at(-1).ms,15000);
  pending=batch({consent:true,drive:{awaiting_exporter:1}});
  handles.at(-1).fn();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,1);
  assert.equal(calls[0],'/v1/books/novel/translation-batches/batch-001');
  assert.equal(renders.length,2);
  assert.equal(handles.at(-1).ms,60000);
  // No background requests while a tab is hidden. Revalidate on return.
  visible=false;onVisible();
  assert.ok(canceled.length>=1);
  const before=calls.length;
  visible=true;
  pending=batch({consent:true,drive:{exported:1}});
  onVisible();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,before+1);
  assert.equal(nextBatchRefreshDelay(renders.at(-1)),null);
  assert.equal(handles.length,2,'a completed delivery has no further timer');
  reads.reset();opened=false;ticket++;
  onVisible();
  assert.equal(calls.length,before+1);
});
test('ignore delayed response from a closed or switched batch',async()=>{
  const book={book_id:'novel'};
  let ticket=1,resolveOld,shown=[];
  const reads=createBatchReads({
    getActive:()=>book,getTicket:()=>ticket,
    activeFor:(t,b)=>t===ticket&&b===book,
    path:()=>'/book',
    request:path=>path.endsWith('/first')
      ?new Promise(resolve=>{resolveOld=resolve;})
      :Promise.resolve({...batch(),batch_id:'second'}),
    historyList:{},progressPanel:{},progressTitle:{},
    progressSummary:{textContent:''},progressBody:{},progressRefresh:{},
    showError:()=>{},render:x=>shown.push(x.batch_id),
    scheduleTimer:()=>({}),cancelTimer:()=>{},
    isVisible:()=>true,onVisibilityChange:()=>{},
  });
  const first=reads.loadProgress('first');
  await reads.loadProgress('second');
  resolveOld({...batch(),batch_id:'first'});
  await first;
  assert.deepEqual(shown,['second']);
  reads.reset();ticket++;
  assert.equal(reads.batchId(),'');
});
