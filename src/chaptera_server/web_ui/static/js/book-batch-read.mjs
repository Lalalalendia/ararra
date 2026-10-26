/** Durable batch history and bounded, visible-only progress polling.
 * Requests are scoped to the current book, dialog generation and batch.
 * Polling reads SQLite status only: NEVER submits Gemini jobs or Drive writes.
 */
import {renderBatchHistory,renderBatchProgress} from './book-batch-presenter.mjs';

export function nextBatchRefreshDelay(batch){
  if(!batch || typeof batch.batch_id!=='string' || !batch.batch_id
      || !Number.isSafeInteger(batch.chapter_count) || batch.chapter_count<1)
    return null;
  const c=batch.counts||{};
  if(['queued','running','retry_wait'].some(x=>Number(c[x])>0))
    return 15000;
  const drive=batch.drive_counts||{};
  if(batch.drive_export_requested===true &&
     ['awaiting_exporter','uploading'].some(x=>Number(drive[x])>0))
    return 60000;
  return null; // Nothing actionable will progress without user intervention.
}

export function createBatchReads({
  getActive,getTicket,activeFor,path,request,historyList,
  progressPanel,progressTitle,progressSummary,progressBody,progressRefresh,
  showError,
  render=renderBatchProgress,
  scheduleTimer=(callback,ms)=>setTimeout(callback,ms),
  cancelTimer=handle=>clearTimeout(handle),
  isVisible=()=>typeof document==='undefined'||!document.hidden,
  onVisibilityChange=callback=>{
    if(typeof document!=='undefined')document.addEventListener('visibilitychange',callback);
  },
}){
  let reading=false,requestSerial=0;
  let currentBatchId='',refreshTimer=null,pendingPoll=false;
  function clearTimer(){
    if(refreshTimer!==null){cancelTimer(refreshTimer);refreshTimer=null;}
  }
  function scheduleNext(batch){
    clearTimer();
    const delay=nextBatchRefreshDelay(batch);
    pendingPoll=delay!==null;
    const book=getActive(),ticket=getTicket(),batchId=batch.batch_id;
    if(!pendingPoll || !book || !activeFor(ticket,book)||!isVisible())return;
    refreshTimer=scheduleTimer(()=>{
      refreshTimer=null;
      if(currentBatchId===batchId && activeFor(ticket,book) && isVisible())
        void loadProgress(batchId,{quiet:true});
    },delay);
  }
  function renderProgress(batch){
    currentBatchId=batch.batch_id;
    render(batch,{
      progressPanel,progressTitle,progressSummary,progressBody,progressRefresh,
    });
    scheduleNext(batch);
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
  async function loadProgress(batchId,{quiet=false}={}){
    const book=getActive();
    if(!book||!batchId)return;
    const ticket=getTicket(),serial=++requestSerial;
    if(currentBatchId!==batchId){currentBatchId=batchId;pendingPoll=false;}
    clearTimer();
    if(!quiet)progressSummary.textContent='Загрузка сохранённого прогресса…';
    try{
      const result=await request(path()+'/'+encodeURIComponent(batchId));
      if(activeFor(ticket,book) && serial===requestSerial && currentBatchId===batchId)
        renderProgress(result);
    }catch(error){
      if(activeFor(ticket,book) && serial===requestSerial){
        showError(error);
        if(pendingPoll && isVisible()){
          refreshTimer=scheduleTimer(()=>{refreshTimer=null;
            if(activeFor(ticket,book))void loadProgress(batchId,{quiet:true});
          },60000);
        }
      }
    }
  }
  onVisibilityChange(()=>{
    clearTimer();
    const book=getActive();
    if(isVisible() && pendingPoll && book && activeFor(getTicket(),book)
        && currentBatchId)
      void loadProgress(currentBatchId,{quiet:true});
  });
  return {
    loadHistory,loadProgress,renderProgress,
    batchId:()=>currentBatchId,
    reset(){
      clearTimer();pendingPoll=false;requestSerial++;
      reading=false;currentBatchId='';
    },
  };
}
