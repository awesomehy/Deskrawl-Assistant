'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const managed=require('../deskrawl_assistant/web/managed-mode.js');

test('pumpkin boss is absent from original map lists and retains independent quota labels',()=>{
  const settings=managed.normalizeSettings({farming:{difficulty:'Normal'}});
  const boss={map_id:'WorldBossPumpkin',name:'南瓜王',map_type:4,unlocked:true,can_enter:true,
    world_boss:{available:true,remaining:36,daily_limit:100}};
  const packet={maps_by_difficulty:Object.fromEntries(['Normal','Nightmare','Inferno'].map(tier=>[tier,[boss,{id:'Volcano7',name:'灰烬之冠 7',map_type:0}]]))};
  for(const tier of ['Normal','Nightmare','Inferno']){
    assert.deepEqual(managed.availableMaps(packet,settings,tier).map(row=>row.id),['Volcano7']);
    assert.equal(managed.pumpkinLabel(packet,{...settings,pumpkin:{enabled:true,difficulty:tier}}),'南瓜王 · 世界首领 · 今日剩余 36/100 次');
  }
});

test('legacy boss loops migrate without changing other map quotas or enabling a disabled plan',()=>{
  for(const enabled of [true,false]){
    const raw={farming:{enabled,dungeon_id:'WorldBossPumpkin',difficulty:'Inferno',repeat_maps:[
      {dungeon_id:'WorldBossPumpkin',difficulty:'Inferno',runs:100},{dungeon_id:'Volcano7',difficulty:'Normal',runs:300}]}};
    const before=JSON.stringify(raw),settings=managed.normalizeSettings(raw);
    assert.deepEqual(settings.pumpkin,{enabled,difficulty:'Inferno'});
    assert.deepEqual(settings.farming.repeat_maps,[{dungeon_id:'Volcano7',difficulty:'Normal',runs:300}]);
    assert.equal(settings.farming.dungeon_id,'Volcano7');assert.equal(JSON.stringify(raw),before);
    assert.deepEqual(managed.normalizeSettings(settings),settings);
  }
  const only=managed.normalizeSettings({farming:{enabled:true,dungeon_id:'WorldBossPumpkin',difficulty:'Normal'}});
  assert.equal(only.pumpkin.enabled,true);assert.equal(only.farming.enabled,false);
  assert.equal(only.farming.dungeon_id,null);assert.deepEqual(only.farming.repeat_maps,[]);
});

test('independent boss switch works with original farming off and no selected maps',()=>{
  const settings=managed.normalizeSettings({pumpkin:{enabled:true,difficulty:'Inferno'}});
  assert.equal(managed.validateSettings(settings),'');
  const required=managed.requiredCapabilities(settings);
  for(const key of ['travel','difficulty','transfer','chests','ground_pickup'])assert.ok(required.includes(key));
  const capabilities=Object.fromEntries(required.map(key=>[key,true]));
  assert.equal(managed.controlState({capabilities,control_available:true},settings,true).canStart,true);
  assert.equal(managed.controlState({capabilities,control_available:false},settings,true).canStart,false);
  assert.equal(managed.defaultFolds(settings).farming,true);
  assert.match(managed.foldSummaries(settings,{}).farming.summary,/优先南瓜王/);
  assert.equal(managed.normalizeSettings().pumpkin.enabled,false);
});

test('boss unavailability gives a specific reason while preserving ordinary labels',()=>{
  assert.match(managed.mapOptionLabel({name:'南瓜王',map_type:4,unlocked:true,can_enter:false,
    entry_reason:'南瓜王活动已结束。'}),/活动已结束/);
  assert.match(managed.mapOptionLabel({name:'南瓜王',map_type:4,unlocked:false}),/未解锁/);
  assert.equal(managed.mapOptionLabel({name:'灰烬之冠 7',map_type:0}),'灰烬之冠 7');
});
