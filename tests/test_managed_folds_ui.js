const {test}=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../deskrawl_assistant/web/managed-mode.js');

function fixture(initial={}){
  let stored=JSON.stringify(initial),writes=0;
  const storage={getItem:()=>stored,setItem:(key,value)=>{assert.equal(key,ui.foldStorageKey);stored=value;writes++;}};
  const elements=ui.foldKeys.map(key=>({dataset:{managedFold:key},open:false,listeners:{},
    addEventListener(type,fn){this.listeners[type]=fn;},toggle(){this.listeners.toggle();}}));
  const controller=ui.createFoldState(elements,storage);
  return {elements,controller,element:key=>elements.find(n=>n.dataset.managedFold===key),
    flush:()=>elements.forEach(n=>n.toggle()),saved:()=>JSON.parse(stored),writes:()=>writes};
}

test('first view opens enabled functions and leaves unused plans folded without changing presets',()=>{
  const settings=ui.normalizeSettings({auto_equip:{enabled:true,slot_locks:{8:'LegendaryNeck3'}},
    farming:{enabled:false},progression:{talents_enabled:true,talent_targets:{4:{a:10}}},
    life_skills:{enabled:false,priorities:['first']},merchant:{enabled:false}});
  const before=JSON.stringify(settings),f=fixture();f.controller.apply(settings);f.flush();
  assert.deepEqual(Object.fromEntries(f.elements.map(n=>[n.dataset.managedFold,n.open])),{
    background:true,equip:true,farming:false,merchant:false,growth:true,synthesis:false,cleanup:false,
    talents:true,life:false,skills:false});
  assert.equal(f.writes(),0);assert.equal(JSON.stringify(settings),before);
});

test('user fold choices persist independently and polling never reopens or collapses them',()=>{
  const f=fixture({farming:false,equip:true,life:true}),settings=ui.normalizeSettings({farming:{enabled:true,dungeon_id:12}});
  f.controller.apply(settings);f.flush();
  assert.equal(f.element('farming').open,false);assert.equal(f.element('equip').open,true);
  assert.equal(f.writes(),0);
  f.element('merchant').open=true;f.element('merchant').toggle();
  f.element('equip').open=false;f.element('equip').toggle();
  const saved=f.saved();assert.deepEqual(saved,{farming:false,equip:false,life:true,merchant:true});
  for(let n=0;n<5;n++){f.controller.apply(ui.normalizeSettings({auto_equip:{enabled:true},farming:{enabled:false}}));f.flush();}
  assert.equal(f.element('merchant').open,true);assert.equal(f.element('equip').open,false);
  assert.equal(f.writes(),2);
  const restored=fixture(saved);restored.controller.apply(settings);restored.flush();
  assert.equal(restored.element('merchant').open,true);assert.equal(restored.element('farming').open,false);
  assert.equal(restored.element('equip').open,false);assert.equal(restored.writes(),0);
});

test('a choice before the first settings reply wins over enabled defaults',()=>{
  const f=fixture();f.element('farming').open=true;f.element('farming').toggle();
  f.element('farming').open=false;f.element('farming').toggle();
  f.controller.apply(ui.normalizeSettings({farming:{enabled:true,dungeon_id:12}}));f.flush();
  assert.equal(f.element('farming').open,false);assert.equal(f.writes(),2);
});

test('malformed or unavailable preference storage cannot prevent keyboard or mouse folding',()=>{
  assert.deepEqual(ui.normalizeFoldPreferences({equip:false,farming:true,merchant:'true',bad:true}),{equip:false,farming:true});
  for(const invalid of [null,[],true,'x'])assert.deepEqual(ui.normalizeFoldPreferences(invalid),{});
  const element={dataset:{managedFold:'farming'},open:false,addEventListener(type,fn){this.listener=fn;}};
  const storage={getItem:()=>'{bad',setItem:()=>{throw new Error('blocked');}};
  const revealed=[];const controller=ui.createFoldState([element],storage,(key,open)=>revealed.push([key,open]));
  assert.doesNotThrow(()=>controller.apply({farming:{enabled:true}}));element.listener();
  element.open=false;assert.doesNotThrow(()=>element.listener());
  assert.deepEqual(revealed,[['farming',true],['farming',false]]);
});

test('collapsed headers describe saved map loops, protected storage, growth and spending without game access',()=>{
  const settings={auto_equip:{enabled:true,slot_locks:{0:'weapon',8:'neck'},disabled_slots:[0]},
    farming:{enabled:true,repeat_maps:[{dungeon_id:0,difficulty:'Inferno',runs:300},{dungeon_id:1,difficulty:'Inferno',runs:200}],free_threshold:8},
    merchant:{enabled:true,trigger_percent:85,spend_to_percent:0,weights:{2:10,4:1}},
    progression:{talents_enabled:true,skills_enabled:true,talent_targets:{4:{a:10,b:8}},skill_stages:{4:[{level:30,slots:['basic',null,'special',null]}]}},
    life_skills:{enabled:true,priorities:['first','second'],gold_reserve:1000}};
  const packet={progression_state:{class_mask:4},maps_by_difficulty:{Inferno:[{id:0,name:'第一张图'},{id:1,name:'第二张图'}]}};
  const before=JSON.stringify(settings),rows=ui.foldSummaries(settings,packet);
  assert.match(rows.farming.summary,/第一张图 · 300 局 → 第二张图 · 200 局/);
  assert.match(rows.farming.summary,/空格 < 8 回城/);assert.equal(rows.farming.status,'已启用');
  assert.match(rows.equip.summary,/暗金限制 1 部位 · 跳过 1 部位/);
  assert.match(rows.cleanup.summary,/装备 1 \/ 2 \/ 3 页 · 符文 4 页 · 宝石 5 页/);assert.equal(rows.cleanup.status,'固定保护');
  assert.match(rows.merchant.summary,/85% 回城 · 保留 0% · 2 部位 \/ 每轮 11 件/);
  assert.match(rows.talents.summary,/18 \/ 70 点/);assert.match(rows.skills.summary,/2 个预设技能/);
  assert.match(rows.life.summary,/最终点满 · 2 个优先节点/);
  assert.equal(JSON.stringify(settings),before);
  assert.match(ui.foldSummaries(settings,null).talents.summary,/连接后显示/);
  const running={...packet,running:true,settings:ui.normalizeSettings(settings),repeat_maps:settings.farming.repeat_maps,repeat_index:0,repeat_completed:17};
  assert.match(ui.foldSummaries(settings,running).farming.summary,/图 1 · 本轮 17 \/ 300 局/);
  running.repeat_completed=18;
  assert.match(ui.foldSummaries(settings,running).farming.summary,/本轮 18 \/ 300 局/);
});
