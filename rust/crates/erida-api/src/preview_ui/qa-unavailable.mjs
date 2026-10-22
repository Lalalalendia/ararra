/** Explicit native capability gap. No fabricated QA report or API response. */
export function initChapterQaPanel(){
  const blank=()=>{
    document.getElementById('editor-qa-list').replaceChildren();
    document.getElementById('editor-qa-scan').disabled=true;
    document.getElementById('editor-qa-summary').textContent=
      'QA \u0435\u0449\u0451 \u043d\u0435 \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0451\u043d \u0432 Rust. \u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043d\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u043b\u0430\u0441\u044c.';
  };
  return {blank,load:async()=>blank()};
}
