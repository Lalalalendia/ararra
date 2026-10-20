import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createTocPlan,selectedOrdinals,visibleOrdinals,pageOf,toggleSelected,
  selectOrdinals,moveVisible,moveBefore,planSummary,
} from './source-toc-plan.mjs';

const sample=Array.from({length:2000},(_,i)=>({
  ordinal:i+1,title:'Глава '+(i+1),source_id:'id-'+(i+1),entry_type:'chapter',
}));

test('2000-item pagination and search never lose checked state or identity',()=>{
  let plan=createTocPlan(sample);
  assert.equal(pageOf(plan,'',0).ordinals.length,80);
  assert.deepEqual(pageOf(plan,'',24).ordinals.slice(-2),[1999,2000]);
  plan=toggleSelected(plan,1234,false);
  plan=selectOrdinals(plan,visibleOrdinals(plan,'Глава 2000'),false);
  assert.equal(planSummary(plan).selected,1998);
  assert.equal(pageOf(plan,'Глава 1999',0).ordinals[0],1999);
  assert.equal(planSummary(plan).excluded,2);
  assert.equal(plan.original[1233],1234);
  assert.equal(planSummary(plan).unresolved,0);
});

test('selection order survives reordering and restore; hidden choices stay selected',()=>{
  const entries=sample.slice(0,7);
  let plan=createTocPlan(entries);
  plan=selectOrdinals(plan,[2,4,5,6],false);
  plan=moveVisible(plan,7,-1);
  plan=moveBefore(plan,7,1);
  assert.deepEqual(selectedOrdinals(plan),[7,1,3]);
  const restored=createTocPlan(entries,selectedOrdinals(plan));
  assert.deepEqual(selectedOrdinals(restored),[7,1,3]);
  assert.equal(planSummary(restored).selected,3);
  assert.equal(planSummary(restored).moved>0,true);
});

test('extra and service records require an explicit checkbox decision',()=>{
  const entries=[
    {ordinal:1,title:'Первая',source_id:'1',entry_type:'chapter'},
    {ordinal:2,title:'Объявление',source_id:'2',entry_type:'service'},
    {ordinal:3,title:'Вставка',source_id:'3',entry_type:'extra'},
  ];
  let plan=createTocPlan(entries);
  assert.deepEqual(selectedOrdinals(plan),[1]);
  assert.equal(planSummary(plan).unresolved,2);
  plan=toggleSelected(plan,2,false);
  plan=toggleSelected(plan,3,true);
  assert.equal(planSummary(plan).unresolved,0);
  assert.deepEqual(selectedOrdinals(plan),[1,3]);
});

test('duplicates, unknown entries and malformed restore fail closed',()=>{
  assert.throws(()=>createTocPlan([sample[0],sample[0]]),/нумерац/);
  const plan=createTocPlan(sample.slice(0,2));
  assert.throws(()=>toggleSelected(plan,30,false),/Unknown/);
  assert.throws(()=>createTocPlan(sample.slice(0,2),[1,1]),/соответствует/);
  assert.throws(()=>createTocPlan(sample.slice(0,2),[3]),/соответствует/);
});
