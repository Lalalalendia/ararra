import test from 'node:test';
import assert from 'node:assert/strict';
import {describeExportPreview,safeExportFilename} from './book-export-view.mjs';

test('ready preview explicitly requires complete contiguous chapters',()=>{
  const message=describeExportPreview({
    ready:true,ready_chapter_count:5,expected_last_chapter:5,
  });
  assert.match(message,/Готово к экспорту: 5/);
  assert.match(message,/глава 5/);
});

test('incomplete accepted manuscript never looks ready',()=>{
  const message=describeExportPreview({
    ready:false,issue_count:3,
    issues:[
      {code:'missing_source',chapter_index:2},
      {code:'stale_accepted',chapter_index:4},
      {code:'unsafe_accepted_html',chapter_index:5},
    ],
  });
  assert.match(message,/Отсутствует исходная глава 2/);
  assert.match(message,/Принятый перевод устарел 4/);
  assert.match(message,/3/);
});

test('file names are ASCII-only and cannot carry paths or external code',()=>{
  assert.equal(safeExportFilename('../my/novel','txt'),'readera-___my_novel.txt');
  assert.equal(safeExportFilename('中文','html'),'readera-__.html');
  assert.equal(safeExportFilename('good-book','txt'),'readera-good-book.txt');
});
