/** Bind RE-06C dialog controls without bloating the main browser controller. */
import {createBookBatchView} from './book-batch-view.mjs';

export function mountBookBatchView({lookup,request,flash}){
  const $=lookup;
  return createBookBatchView({
  dialog:$('book-batch-dialog'),title:$('book-batch-title'),help:$('book-batch-help'),
  selectionStatus:$('book-batch-selection-status'),
  chaptersBody:$('book-batch-chapters'),
  selectAll:$('book-batch-select-all'),clearSelection:$('book-batch-clear'),
  transport:$('book-batch-transport'),maxAttempts:$('book-batch-attempts'),
  previewButton:$('book-batch-preview'),review:$('book-batch-review'),
  reviewSummary:$('book-batch-review-summary'),ack:$('book-batch-ack'),
  submitButton:$('book-batch-submit'),status:$('book-batch-status'),
  historyList:$('book-batch-history-list'),historyRefresh:$('book-batch-history-refresh'),
  progressPanel:$('book-batch-progress'),progressTitle:$('book-batch-progress-title'),
  progressSummary:$('book-batch-progress-summary'),
  progressBody:$('book-batch-progress-rows'),
  progressRefresh:$('book-batch-progress-refresh'),
  request,showError:error=>flash(error.message,true),notify:message=>flash(message),
});
}
