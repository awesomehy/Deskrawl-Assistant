'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function page(){
  const directory=path.join(__dirname,'../deskrawl_assistant/web');
  const html=fs.readFileSync(path.join(directory,'index.html'),'utf8');
  const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],{
    hidden:false,disabled:false,textContent:'',innerHTML:'',value:'',dataset:{},
    events:{},addEventListener(name,handler){this.events[name]=handler;},replaceChildren(){this.innerHTML='';},contains(){return false;},
    classList:{toggle(){},add(){},remove(){}},setAttribute(){}
  }]));
  const tabs=[...html.matchAll(/data-tab="([^"]+)"/g)].map(match=>({
    dataset:{tab:match[1]},classList:{toggle(){}},setAttribute(){},addEventListener(){}
  }));
  const timers=[],managed=[];
  const document={
    getElementById(id){assert.ok(ids.has(id),'Missing merged element: '+id);return ids.get(id);},
    querySelector(){return {content:'isolated-test-token',addEventListener(){}};},
    querySelectorAll(selector){return selector==='.tab'?tabs:[];},addEventListener(){}
  };
  const context=vm.createContext({document,location:{search:''},URLSearchParams,
    window:{addEventListener(){},ManagedModeUI:{setActive:value=>managed.push(value),isDirty:()=>false}},
    setInterval:(callback,delay)=>{timers.push({callback,delay});return timers.length;},
    setTimeout(){},clearTimeout(){},clearInterval(){},console});
  const source=fs.readFileSync(path.join(directory,'app.js'),'utf8');
  assert.equal((source.match(/^boot\(\);$/gm)||[]).length,1);
  vm.runInContext(source.replace(/^boot\(\);$/m,''),context,{filename:'app.js'});
  const run=code=>vm.runInContext(code,context);
  run('renderInventory=()=>{};loadLogs=()=>{};');
  return {context,run,ids,tabs,timers,managed};
}

function plan(key='first',items=[{name:'测试装备',kind:'equipment',rarity:3,quantity:1,level:800,
    sockets:2,modifiers:[{text:'敏捷 +60'}]}]){
  return {available:true,phase:'active',run:{key,map_name:key,current_wave:1,total_waves:1},
    totals:{items:items.length,equipment:items.filter(item=>item.kind==='equipment').length,gold:300,potion_charges:2},
    waves:[{number:1,state:'current',gold:300,potion_charges:2,items}],warnings:[],updated_at:'2026-10-11T12:00:00Z'};
}

test('navigation exposes six pages and preserves managed activation',()=>{
  const p=page();p.run('loadLootPreview=()=>{};');
  assert.deepEqual(p.tabs.map(tab=>tab.dataset.tab),['rules','inventory','managed','loot-preview','logs','feedback']);
  for(const selected of p.tabs.map(tab=>tab.dataset.tab)){
    p.run('switchTab('+JSON.stringify(selected)+')');
    for(const tab of p.tabs)assert.equal(p.ids.get(tab.dataset.tab+'-page').hidden,tab.dataset.tab!==selected);
    assert.equal(p.managed.at(-1),selected==='managed');
  }
});

test('equipment effects are visible on inventory and worn equipment previews',()=>{
  const p=page();
  p.context.gear={name:'炼血者之履',name_key:'LegendaryBoots4',is_equipment:true,kind:'equipment',
    slot_label:'靴子',class_mask:15,rarity:3,rarity_label:'传说',level:70,
    groups:{base:[],primary:[],secondary:[],unknown:[]},reasons:[],
    effect_groups:[{title:'物品效果',values:[],texts:['<color=orange>击杀敌人时有 10% 几率掉落生命药水。</color>']}]};
  for(const container of ['inventory','equipped']){
    p.context.gear.container=container;
    const html=p.run('equipmentPreviewHtml(gear)');
    assert.match(html,/击杀敌人时有 10% 几率掉落生命药水。/);
    assert.doesNotMatch(html,/<color=/);
  }
});

test('retired reports cannot be opened and register no sampling or calibration timers',()=>{
  const p=page();p.run("switchTab('managed');switchTab('recommendations');switchTab('experience');");
  assert.equal(p.run('activeTab'),'managed');
  assert.equal(p.ids.has('experience-page'),false);
  assert.equal(p.ids.has('recommendations-page'),false);
  assert.deepEqual(p.timers.map(timer=>timer.delay),[2000]);
  assert.equal(p.run('hasPendingDrafts()'),false);
});

test('installing an update no longer reads retired experience or recommendation APIs',async()=>{
  const p=page();p.context.calls=[];
  p.run("api=async path=>{calls.push(path);return path==='update'?{phase:'idle',message:'已检查'}:{};};");
  await p.ids.get('install-update').events.click();
  assert.deepEqual([...p.context.calls],['update/install','update']);
});

test('an available plan with no items still displays coins and potion charges',()=>{
  const p=page();p.context.packet=plan('empty',[]);p.run('renderLootPreview(packet)');
  assert.equal(p.ids.get('loot-preview-gold').textContent,'300');
  assert.equal(p.ids.get('loot-preview-potions').textContent,'2');
  assert.equal(p.ids.get('loot-preview-items').textContent,'0');
  assert.equal(p.ids.get('loot-preview-empty').hidden,false);
  assert.match(p.ids.get('loot-preview-waves').innerHTML,/此波无计划物品/);
});

test('new runs replace previous items and unavailable plans clear every summary',()=>{
  const p=page();p.context.packet=plan('old',[{name:'旧装备',quantity:1}]);p.run('renderLootPreview(packet)');
  p.context.packet=plan('new',[{name:'新装备',quantity:1}]);p.run('renderLootPreview(packet)');
  assert.match(p.ids.get('loot-preview-waves').innerHTML,/新装备/);
  assert.doesNotMatch(p.ids.get('loot-preview-waves').innerHTML,/旧装备/);
  p.context.packet={available:false,phase:'waiting',reason:'已回城'};p.run('renderLootPreview(packet)');
  assert.equal(p.ids.get('loot-preview-waves').innerHTML,'');
  for(const key of ['items','gold','potions','wave'])assert.equal(p.ids.get('loot-preview-'+key).textContent,'—');
});

test('untrusted names, affixes and icon attributes are escaped and remote icons omitted',()=>{
  const p=page();p.context.item={name:'<script>bad()</script>',kind:'equipment',rarity:3,
    modifiers:[{text:'<img src=x onerror=bad()>'}],icon:'https://example.invalid/image.png'};
  const markup=p.run('lootPreviewItem(item)');
  assert.match(markup,/&lt;script&gt;/);assert.match(markup,/&lt;img/);
  assert.doesNotMatch(markup,/<script>|<img src=x|example\.invalid/);
  p.context.item.icon='/icons/a" onerror="bad()';
  assert.match(p.run('lootPreviewItem(item)'),/a&quot; onerror=&quot;/);
});

test('a pending read cannot repopulate a preview cleared by disconnection',async()=>{
  const p=page();let resolve;
  p.context.getPreview=()=>new Promise(done=>{resolve=done;});
  p.run('state={connected:true};api=()=>getPreview();');
  const pending=p.run('loadLootPreview()');
  p.run("clearLootPreview('已断开');state.connected=false;");
  resolve(plan('obsolete'));await pending;
  assert.equal(p.ids.get('loot-preview-waves').innerHTML,'');
  assert.equal(p.ids.get('loot-preview-map').textContent,'等待读取关卡');
  assert.equal(p.ids.get('loot-preview-refresh').disabled,false);
});

test('black mist and divine previews retain the same quality distinction as managed equipment',()=>{
  const p=page();p.context.item={name:'黑雾装备',kind:'equipment',rarity:3,black_mist:true};
  assert.match(p.run('lootPreviewItem(item)'),/class="loot-preview-item loot-rarity-3 loot-black-mist"/);
  p.context.item.rarity=4;
  assert.doesNotMatch(p.run('lootPreviewItem(item)'),/class="[^"]*loot-black-mist/);
});

test('polling runs only while the preview tab is active and avoids overlapping reads',async()=>{
  const p=page();let calls=0,resolve;
  p.context.getPreview=()=>{calls++;return new Promise(done=>{resolve=done;});};
  p.run('state={connected:true};api=()=>getPreview();');
  const timer=p.timers.find(value=>String(value.callback).includes("activeTab==='loot-preview'"));
  assert.equal(timer.delay,2000);
  p.run("activeTab='managed'");timer.callback();assert.equal(calls,0);
  p.run("activeTab='loot-preview'");timer.callback();timer.callback();assert.equal(calls,1);
  resolve(plan());await new Promise(setImmediate);
  assert.match(p.ids.get('loot-preview-waves').innerHTML,/测试装备/);
});

test('a failed read clears previous items and a subsequent successful read restores the new plan',async()=>{
  const p=page();p.context.packet=plan('old');p.run('renderLootPreview(packet);state={connected:true};');
  p.context.getPreview=async()=>{throw new Error('temporary failure');};p.run('api=()=>getPreview();');
  await p.run('loadLootPreview()');assert.equal(p.ids.get('loot-preview-waves').innerHTML,'');
  p.context.getPreview=async()=>plan('recovered');await p.run('loadLootPreview()');
  assert.equal(p.ids.get('loot-preview-map').textContent,'recovered');
});

test('equipment cards split affix groups and highlight only matched rows',()=>{
  const p=page();p.context.item={name:'装备',kind:'equipment',rarity:3,groups:{
    base:[{name:'护甲',display_value:'100',matched:false}],
    primary:[{name:'敏捷',display_value:'60',matched:true,matched_rules:['组合甲']}],
    secondary:[{name:'击杀恢复生命',display_value:'15',matched:false}],unknown:[]},
    effect_groups:[{title:'装备效果',texts:['<color=orange>测试说明</color>'],values:[]}],
    matches:true,rule_note:'组合甲'};
  const markup=p.run('lootPreviewItem(item)');
  for(const text of ['基础属性 · 不参与筛选','主词条','副词条','60 ✓','测试说明','符合规则'])assert.ok(markup.includes(text));
  assert.match(markup,/affix-row matched/);assert.match(markup,/title="组合甲"/);
  assert.doesNotMatch(markup,/<color/);
});

test('unknown rarity is not styled as common and monitor state survives plan clearing',()=>{
  const p=page();p.context.item={name:'未知',rarity:null};
  assert.doesNotMatch(p.run('lootPreviewItem(item)'),/loot-rarity-0/);
  p.context.monitor={enabled:true,saved_runs:2,directory:'C:\\captures',message:'正在监控'};
  p.run("renderLootCapture(monitor);clearLootPreview('已回城');");
  assert.equal(p.ids.get('loot-capture-enabled').checked,true);
  assert.equal(p.ids.get('loot-capture-summary').textContent,'留档 2 份记录');
  assert.equal(p.ids.get('loot-capture-directory').textContent,'C:\\captures');
});

test('capture summary reflects reduced file counts and unreadable directories',()=>{
  const p=page();p.context.monitor={enabled:true,saved_runs:1};
  p.run('renderLootCapture(monitor)');
  assert.equal(p.ids.get('loot-capture-summary').textContent,'留档 1 份记录');
  p.context.monitor.saved_runs=0;
  p.run('renderLootCapture(monitor)');
  assert.equal(p.ids.get('loot-capture-summary').textContent,'留档 0 份记录');
  p.context.monitor.saved_runs=null;p.context.monitor.error='文件数量未确认';
  p.run('renderLootCapture(monitor)');
  assert.equal(p.ids.get('loot-capture-summary').textContent,'留档 — 份记录');
  assert.equal(p.ids.get('loot-capture-status').textContent,'文件数量未确认');
});
