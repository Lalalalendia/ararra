/** Operator-facing ZIP provenance, chapter lineage and confirmation summary.
 * Content is always passed through the caller's textContent-based helper:
 * untrusted titles, source URLs and manifest metadata are never HTML.
 */
import {sourceArchiveSummary} from './import-model.mjs';

export function renderArchiveDetails(stage, $, text) {
  const archive=sourceArchiveSummary(stage.preview);
  const provenance=$('import-archive-details');
  provenance.replaceChildren();
  provenance.hidden=!archive;
  $('import-archive-ack-wrap').hidden=!archive;
  $('import-archive-ack').checked=false;
  if(!archive)return;
  text(provenance,'Архив Full Cycle App · границы глав из manifest.json','strong');
  text(provenance,'Исходный сайт: '+archive.domain+' · '+archive.sourceUrl,'p');
  text(provenance,'В ZIP: '+archive.chapterCount+' глав. Рабочий текст отличается от оригинала у '+archive.modifiedCount+' глав.','p');
  text(provenance,'Пропуски позиций: '+archive.gapsLabel,'p');
  if(archive.repeatedUrlCount){
    text(provenance,'Повторяющиеся URL глав: '+archive.repeatedUrlCount+
      '. Позиции: '+archive.repeatedUrlsLabel+'. Проверьте, не дублируются ли страницы.','p');
  }
  if(archive.sourceOrderChanged){
    text(provenance,'Внимание: позиции глав в архиве идут не по возрастанию. Проверьте порядок перед импортом.','p');
  }
  text(provenance,archive.completenessWarning,'p');
}

export function renderArchiveRowDetails(row, info, text, shorten) {
  if(!row.source_original_sha256 || !row.source_working_sha256)return;
  text(info,'Позиция в архиве: '+row.catalog_ordinal+' · URL: '+shorten(row.source_url||'не указан',100),'small');
  text(info,'Оригинал SHA-256: '+row.source_original_sha256.slice(0,12)+
    '… · Рабочая версия: '+row.source_working_sha256.slice(0,12)+'…'+
    (row.source_original_sha256!==row.source_working_sha256?' (исправлена)':''),'small');
}

export function renderImportReviewSummary(root, rows, targetBook, staged, text) {
  root.replaceChildren();
  const counts={create:0,replace:0,skip:0};
  rows.forEach(r=>counts[r.action]++);
  const archive=sourceArchiveSummary(staged.preview);
  const fields=[
    ['Книга',targetBook.book_id],
    ['Режим',targetBook.book_mode==='create'?'Создание книги':'Дополнение книги'],
    ['Создать глав',counts.create],
    ['Заменить глав',counts.replace],
    ['Пропустить глав',counts.skip],
    ['Предисловие',staged.unassigned_prefix_saved?targetBook.prefix_policy:'Не обнаружено'],
    ['Хеш файла',staged.file_sha256],
    ...(archive ? [
      ['ZIP Full Cycle App',archive.domain],
      ['Пропуски позиций',archive.gapsLabel],
      ...(archive.repeatedUrlCount ? [['Повторяющиеся URL',archive.repeatedUrlsLabel]] : []),
      ...(archive.sourceOrderChanged ? [['Порядок глав','Требует проверки: позиции не возрастают']] : []),
      ['Полнота сайта','Не подтверждена по архиву'],
    ] : []),
  ];
  for(const [label,value] of fields){
    const div=document.createElement('p');
    text(div,label+': ','strong');
    text(div,value);
    root.append(div);
  }
}
