/** Durable batch history and progress reads, shared by library and import UX.
 * Read requests are always scoped to the active book and generation, and
 * stale HTTP replies cannot replace the currently open batch.
 */
import {renderBatchHistory,renderBatchProgress} from './book-batch-presenter.mjs';

export function createBatchReads({
  getActive,getTicket,activeFor,path,request,historyList,
  progressPanel,progressTitle,progressSummary,progressBody,progressRefresh,
  showError,
}){
  let reading=false;
  let currentBatchId='';
  function renderProgress(batch){
    currentBatchId=batch.batch_id;
    renderBatchProgress(batch,{
      progressPanel,progressTitle,progressSummary,progressBody,progressRefresh,
    });
  }
  async function loadHistory(){
    const book=getActive();
    if(!book||reading)return;
    const ticket=getTicket();
    reading=true;
    try{
      const result=await request(path()+'?limit=20');
      if(activeFor(ticket,book))
        renderBatchHistory(historyList,result.batches||[],loadProgress);
    }catch(error){
      if(activeFor(ticket,book))showError(error);
    }finally{reading=false;}
  }
  async function loadProgress(batchId){
    const book=getActive();
    if(!book||!batchId)return;
    const ticket=getTicket();
    progressSummary.textContent='Загрузка сохранённого прогресса…';
    try{
      const result=await request(path()+'/'+encodeURIComponent(batchId));
      if(activeFor(ticket,book))renderProgress(result);
    }catch(error){
      if(activeFor(ticket,book))showError(error);
    }
  }
  return {
    loadHistory,loadProgress,renderProgress,
    batchId:()=>currentBatchId,
    reset(){reading=false;currentBatchId='';},
  };
}
