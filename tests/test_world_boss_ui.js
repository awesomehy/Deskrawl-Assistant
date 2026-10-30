'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const managed=require('../deskrawl_assistant/web/managed-mode.js');

test('pumpkin boss is available at all world difficulties and retains daily quota labels',()=>{
  const settings=managed.normalizeSettings({farming:{difficulty:'Normal'}});
  const boss={map_id:'WorldBossPumpkin',name:'南瓜王',map_type:4,unlocked:true,can_enter:true,
    world_boss:{available:true,remaining:36,daily_limit:100}};
  const packet={maps_by_difficulty:Object.fromEntries(['Normal','Nightmare','Inferno'].map(tier=>[tier,[boss]]))};
  for(const tier of ['Normal','Nightmare','Inferno']){
    const row=managed.availableMaps(packet,settings,tier)[0];
    assert.equal(row.map_id,'WorldBossPumpkin');
    assert.equal(managed.mapOptionLabel(row),'南瓜王 · 世界首领 · 今日剩余 36/100 次');
  }
});

test('boss unavailability gives a specific reason while preserving ordinary labels',()=>{
  assert.match(managed.mapOptionLabel({name:'南瓜王',map_type:4,unlocked:true,can_enter:false,
    entry_reason:'南瓜王活动已结束。'}),/活动已结束/);
  assert.match(managed.mapOptionLabel({name:'南瓜王',map_type:4,unlocked:false}),/未解锁/);
  assert.equal(managed.mapOptionLabel({name:'灰烬之冠 7',map_type:0}),'灰烬之冠 7');
});
