import test from 'node:test';
import assert from 'node:assert/strict';
import {renderDailyQuota} from './jobs-view.mjs';

test('unconfigured cap is never presented as unlimited Google allowance',()=>{
  const element={textContent:''};
  renderDailyQuota(element,{enabled:false,model:'test-model'});
  assert.match(element.textContent,/не настроен/);
  assert.match(element.textContent,/неизвестна/);
});

test('quota presentation counts attempts and hides project scope',()=>{
  const element={textContent:''};
  renderDailyQuota(element,{
    enabled:true,model:'model-x',pacific_day:'2026-10-17',
    used:2,limit:21,remaining:19,reset_at:1792310400,
    externally_blocked:false,project:'private-not-for-ui',
  });
  assert.match(element.textContent,/2 из 21/);
  assert.match(element.textContent,/19/);
  assert.doesNotMatch(element.textContent,/private-not-for-ui/);
});

test('provider-confirmed daily block is explicit, unavailable ledger is unknown',()=>{
  const el={textContent:''};
  renderDailyQuota(el,{
    enabled:true,model:'model-x',used:3,limit:21,remaining:0,
    reset_at:1792310400,externally_blocked:true,
  });
  assert.match(el.textContent,/429/);
  renderDailyQuota(el,null);
  assert.match(el.textContent,/недоступны/);
});
