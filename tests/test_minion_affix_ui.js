'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function page(){
  const ids=new Map();
  function element(id){
    if(!ids.has(id))ids.set(id,{value:'',textContent:'',innerHTML:'',dataset:{},events:{},
      addEventListener(name,handler){this.events[name]=handler;},classList:{toggle(){}},
      setAttribute(){},querySelectorAll(){return [];}});
    return ids.get(id);
  }
  const directory=path.join(__dirname,'../deskrawl_assistant/web');
  const document={getElementById:element,querySelector:()=>({content:'test-token',addEventListener(){}}),
    querySelectorAll:()=>[],addEventListener(){}};
  const context=vm.createContext({document,location:{search:''},URLSearchParams,console,
    window:{addEventListener(){}},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){}});
  const run=code=>vm.runInContext(code,context);
  run(fs.readFileSync(path.join(directory,'app.js'),'utf8').replace(/^boot\(\);$/m,''));
  run(fs.readFileSync(path.join(directory,'rule-tools.js'),'utf8'));
  const raw=JSON.parse(fs.readFileSync(path.join(__dirname,'../data/affix-groups-static.json'),'utf8'));
  const minion=raw.slotPools.find(p=>p.slot==='Minion');
  context.fixture={pools:{Minion:{primary:minion.primary,secondary:minion.secondary,
    random:[...minion.primary,...minion.secondary]}},classes:[{value:1,name:'战士'}]};
  run("catalog=fixture;gearKey='LegendaryMinionGear1';"+
    "gearMap.set(gearKey,{key:gearKey,slot:'Minion',class_mask:15,name:'仆从装备',en:'Minion'});"+
    "for(const key of catalog.pools.Minion.random)statMap.set(key,{name:key,en:key});"+
    "state={rules:[]};draft=makeDraft();affixClass=0;");
  return {run,ids,context,click:id=>ids.get('rule-editor').events.click({target:{closest:()=>({id})}})};
}

test('minion editor shows a single random pool containing both old pools',()=>{
  const p=page();p.run('renderEditor()');
  const html=p.ids.get('rule-editor').innerHTML;
  assert.match(html,/data-group="random"/);
  assert.match(html,/data-affix="Stats.Strength"/);
  assert.match(html,/data-affix="Stats.LifeOnHit"/);
  assert.doesNotMatch(html,/data-group="primary"|data-group="secondary"|secondary-enabled/);
  assert.match(html,/稀有 2 条 · 传说 3 条/);
});

test('new rules save one group and enforce the native three-affix maximum',async()=>{
  const p=page();
  p.run('draft.random=["Stats.LifeOnHit","Stats.LifeOnKill","Stats.ManaOnKill","Stats.Strength"];draft.randomCount=4;');
  assert.match(p.run('validation()'),/不能超过 3/);
  p.run('draft.randomCount=3;api=async(path,data)=>{globalThis.captured=data;return {rule:{id:"new"}}};pollState=async()=>{};toast=()=>{};');
  assert.equal(await p.run('saveRule()'),true);
  assert.deepEqual(JSON.parse(p.run('JSON.stringify(Object.keys(captured.groups))')),['random']);
  assert.equal(p.run('captured.groups.random.count'),3);
});

test('primary-only old rule converts losslessly without deleting selected types',async()=>{
  const p=page();p.run("state.rules=[{id:'old',name:'旧组合',enabled:false,equipment_keys:[gearKey],"+
    "groups:{primary:{selected_stats:['Stats.Strength','Stats.CritDamage'],operator:'>',count:1}}}];"+
    "draft=makeDraft(state.rules[0]);");
  assert.equal(p.run('draft.legacyGroups'),null);
  assert.equal(p.run('draft.randomCount'),2);
  p.run('api=async(path,data)=>{globalThis.captured=data;return {rule:{id:"old"}}};pollState=async()=>{};toast=()=>{};');
  assert.equal(await p.run('saveRule()'),true);
  assert.equal(p.run('captured.enabled'),false);
  assert.equal(p.run('captured.name'),'旧组合');
  assert.deepEqual(JSON.parse(p.run('JSON.stringify(captured.groups)')),
    {random:{selected_stats:['Stats.Strength','Stats.CritDamage'],operator:'>=',count:2}});
});

function legacy(p){
  p.run("state.rules=[{id:'old',name:'旧 AND',enabled:true,equipment_keys:[gearKey],"+
    "groups:{primary:{selected_stats:['Stats.Strength','Stats.CritDamage'],operator:'>=',count:2},"+
    "secondary:{selected_stats:['Stats.LifeOnHit','Stats.LifeOnKill'],operator:'>',count:0}}}];"+
    "draft=makeDraft(state.rules[0]);renderEditor();");
}

test('old AND combination stays active with unchanged constraints on rename/save',async()=>{
  const p=page();legacy(p);
  const before=p.run('JSON.stringify(state.rules[0].groups)');
  assert.match(p.ids.get('rule-editor').innerHTML,/旧组合兼容中/);
  assert.match(p.ids.get('rule-editor').innerHTML,/data-count="random"[^>]*disabled/);
  p.run('draft.name="改名";api=async(path,data)=>{globalThis.captured=data;return {rule:{id:"old"}}};pollState=async()=>{};toast=()=>{};');
  assert.equal(await p.run('saveRule()'),true);
  assert.equal(p.run('captured.groups.primary.count'),2);
  assert.equal(p.run('captured.groups.secondary.count'),1);
  assert.equal(p.run('captured.groups.secondary.operator'),'>=');
  assert.equal(p.run('JSON.stringify(state.rules[0].groups)'),before);
  assert.equal(p.run('"random" in captured.groups'),false);
});

test('legacy conversion needs confirmation and is unsaved until the user saves',async()=>{
  const p=page();legacy(p);const before=p.run('JSON.stringify(draft)');
  p.run('confirmAction=async()=>false');await p.click('convert-minion-rule');
  assert.equal(p.run('JSON.stringify(draft)'),before);
  p.run('confirmAction=async()=>true');await p.click('convert-minion-rule');
  assert.equal(p.run('draft.legacyGroups'),null);
  assert.equal(p.run('draft.randomCount'),3);
  assert.equal(p.run('dirty'),true);
  assert.equal(p.run('state.rules[0].groups.primary.count'),2);
  assert.equal(p.run('draft.random.length'),4);
  await p.click('reset-rule');
  assert.equal(p.run('draft.legacyGroups.primary.count'),2);
  assert.equal(p.run('dirty'),false);
});

test('old impossible four-affix threshold is preserved until explicit conversion',()=>{
  const p=page();p.run("draft=makeDraft({id:'old',groups:{primary:{selected_stats:"+
    "catalog.pools.Minion.primary.slice(0,5),operator:'>=',count:4}}});renderEditor();");
  assert.equal(p.run('draft.legacyGroups.primary.count'),4);
  assert.match(p.ids.get('rule-editor').innerHTML,/旧组合兼容中/);
});

test('imported pool-invalid old selections are not silently broadened by migration',()=>{
  const p=page();p.run("draft=makeDraft({id:'old',groups:{primary:{selected_stats:"+
    "['Stats.LifeOnHit'],operator:'>=',count:1}}});renderEditor();");
  assert.equal(p.run('draft.legacyGroups.primary.selected_stats[0]'),'Stats.LifeOnHit');
  assert.match(p.ids.get('rule-editor').innerHTML,/旧组合兼容中/);
});

test('combination manager and inventory/preview labels support random rules',()=>{
  const p=page();p.run("state.rules=[{id:'new',name:'三词条',enabled:true,equipment_keys:[gearKey],"+
    "groups:{random:{selected_stats:['Stats.LifeOnHit'],operator:'>=',count:1}}}];"+
    "managerGear=gearKey;renderCombinationManager();");
  assert.match(p.ids.get('manage-list').innerHTML,/随机词条 ≥1 类/);
  assert.match(p.run('resultBadge({is_equipment:true,is_minion_equipment:true,primary_perfect:true,matches:true})'),/随机词条全中/);
  assert.match(p.run("lootPreviewGroups({groups:{random:[{name:'击中回血',display_value:'1'}]}})"),/随机词条/);
});

test('inventory only-match filter excludes minion all-three-perfect matches',()=>{
  const {select}=require('../deskrawl_assistant/web/inventory-tools.js');
  const rows=[{selection_id:'all',slot:'Minion',is_equipment:true,matches:true,primary_perfect:true},
    {selection_id:'some',slot:'Minion',is_equipment:true,matches:true,primary_perfect:false}];
  assert.deepEqual(select(rows,{rules:['matches_only']}).map(r=>r.selection_id),['some']);
  assert.deepEqual(select(rows,{rules:['perfect']}).map(r=>r.selection_id),['all']);
});
