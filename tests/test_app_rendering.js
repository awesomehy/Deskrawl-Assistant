'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const InventoryTools=require('../deskrawl_assistant/web/inventory-tools.js');
const AutomationTools=require('../deskrawl_assistant/web/automation-tools.js');
const directory=path.join(__dirname,'../deskrawl_assistant/web');
const source=fs.readFileSync(path.join(directory,'app.js'),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));

function element(){
  let html='';
  return {
    hidden:false,disabled:false,textContent:'',value:'',checked:false,open:false,scrollTop:0,
    dataset:{},events:{},children:new Map(),writes:0,
    get innerHTML(){return html;},set innerHTML(value){html=value;this.writes++;},
    addEventListener(name,handler){this.events[name]=handler;},
    replaceChildren(){this.innerHTML='';},querySelector(selector){return this.children.get(selector)||null;},
    querySelectorAll(){return [];},contains(){return false;},setAttribute(){},
    classList:{toggle(){},add(){},remove(){}},
    showModal(){this.open=true;},close(){this.open=false;this.events.close?.();}
  };
}
function gear(id='item-1',extra={}){
  return {selection_id:id,name:id,name_key:id,kind:'equipment',is_equipment:true,
    container:'inventory',slot_index:0,slot:'Weapon',slot_label:'武器',class_mask:15,
    rarity:3,rarity_label:'传说',count:1,level:70,upgrade:0,sockets:0,occupied_sockets:0,
    locked:false,movable:true,reasons:[],groups:{base:[],primary:[],secondary:[],unknown:[]},...extra};
}
function character(extra={}){
  return {available:true,name:'测试角色',class_mask:1,level:70,primary:0,attack:100,
    captured_at:'2026-11-06T00:00:00Z',health:100,health_max:100,attack_speed:1,
    attributes:[{stat:0,name:'力量',display_value:'100'}],warnings:[],
    equipment:[gear('worn',{container:'equipped',equipped_slot:0})],...extra};
}
function packet(extra={}){
  return {connected:true,complete:true,busy:false,rules:[],items:[gear()],character:character(),
    message:'正在刷图',counts:{},history:[],managed:{running:false},transfer:{available:true},...extra};
}
function page(){
  const html=fs.readFileSync(path.join(directory,'index.html'),'utf8');
  const ids=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match=>[match[1],element()]));
  const docEvents={},calls=[],pending=[],managed=[],auction=[],timers=[];
  const document={visibilityState:'visible',body:element(),
    getElementById(id){assert.ok(ids.has(id),'Missing element: '+id);return ids.get(id);},
    querySelector(selector){return selector.startsWith('meta')?{content:'test-token'}:element();},
    querySelectorAll(){return [];},addEventListener(name,handler){docEvents[name]=handler;}};
  const context=vm.createContext({document,location:{search:''},URLSearchParams,InventoryTools,
    window:{addEventListener(){},AutomationTools,
      ManagedModeUI:{setActive(){},setConnection(value){managed.push(value);},setCharacter(){},isDirty:()=>false},
      InventoryAuctionUI:{setActive(){},update(state,items){auction.push({state,items:[...items]});}}},
    setInterval(callback,delay){timers.push({callback,delay});},setTimeout(){},clearTimeout(){},console});
  vm.runInContext(source.replace(/^boot\(\);$/m,''),context,{filename:'app.js'});
  const run=code=>vm.runInContext(code,context);
  run('catalog={classes:[{value:1,name:"战士"}],equipment:[],slots:[],stats:[]};');
  context.request=route=>new Promise((resolve,reject)=>{calls.push(route);pending.push({resolve,reject});});
  run('api=route=>request(route);');
  function setState(value){context.packet=value;run('state=packet;');}
  setState(packet());
  return {run,context,ids,document,docEvents,calls,pending,managed,auction,timers,setState};
}

test('hidden inventory skips row/character reconstruction while global state and selection safety update',async()=>{
  const p=page();
  p.run("switchTab('inventory');selectedItems.add('item-1');renderInventory();switchTab('managed');");
  const rows=p.ids.get('item-rows').writes,body=p.ids.get('character-content').writes;
  const pending=p.run('pollState()');
  p.pending.shift().resolve(packet({items:[gear('new-item')],busy:true,message:'整理中',managed:{running:true}}));
  await pending;
  assert.equal(p.ids.get('item-rows').writes,rows);
  assert.equal(p.ids.get('character-content').writes,body);
  assert.equal(p.ids.get('operation-message').textContent,'整理中');
  assert.equal(p.ids.get('managed-tab-status').hidden,false);
  assert.equal(p.ids.get('lock-selected').disabled,true);
  assert.equal(p.ids.get('move-storage').disabled,true);
  assert.equal(p.run('selectedItems.size'),0,'removed selection is invalidated even on a hidden page');
  assert.equal(p.auction.at(-1).items.length,0);
  assert.equal(p.managed.at(-1),true);
  p.run("switchTab('inventory');");
  assert.match(p.ids.get('item-rows').innerHTML,/new-item/);
  assert.doesNotMatch(p.ids.get('item-rows').innerHTML,/item-1/);
});

test('sampling time and health update without rebuilding worn equipment; changed attributes still redraw',()=>{
  const p=page();p.run("switchTab('inventory');");
  const content=p.ids.get('character-content'),metrics=p.ids.get('character-metrics');
  const rows=content.writes,metricWrites=metrics.writes,status=p.ids.get('character-status').textContent;
  p.run("state.character.captured_at='2026-11-06T00:01:00Z';renderState();");
  assert.notEqual(p.ids.get('character-status').textContent,status);
  assert.equal(content.writes,rows);
  assert.equal(metrics.writes,metricWrites);
  p.run('state.character.health=25;renderState();');
  assert.equal(content.writes,rows);
  assert.equal(metrics.writes,metricWrites+1);
  assert.match(metrics.innerHTML,/25 \/ 100/);
  content.children.set('.all-character-attributes',{open:true});
  p.run("state.character.attributes[0].display_value='110';renderState();");
  assert.equal(content.writes,rows+1);
  assert.match(content.innerHTML,/all-character-attributes" open/);
  assert.match(content.innerHTML,/110/);
  p.run("state.character.equipment[0].name='新武器';renderState();");
  assert.match(content.innerHTML,/新武器/);
});

test('restoring a hidden window renders the latest inventory immediately without discarding drafts or filters',async()=>{
  const p=page();p.run("switchTab('inventory');draft={name:'未保存规则'};dirty=true;");
  p.ids.get('item-search').value='item';
  p.run("selectedItems.add('item-1');detailId='item-1';renderInventory();");
  const details=p.ids.get('item-detail');details.scrollTop=240;
  details.children.set('.comparison-attributes',{open:true});
  const writes=p.ids.get('item-rows').writes,body=p.ids.get('character-content').writes;
  p.document.visibilityState='hidden';
  p.run("state.items[0].count=2;state.character.level=71;state.character.equipment[0].name='恢复后的武器';renderState();");
  assert.equal(p.ids.get('item-rows').writes,writes);
  assert.equal(p.ids.get('character-content').writes,body);
  p.document.visibilityState='visible';p.docEvents.visibilitychange();
  assert.equal(p.ids.get('item-rows').writes,writes+1);
  assert.match(p.ids.get('character-name').textContent,/Lv.71/);
  assert.match(p.ids.get('character-content').innerHTML,/恢复后的武器/);
  assert.equal(details.scrollTop,240);
  assert.equal(details.children.get('.comparison-attributes').open,true);
  assert.equal(p.ids.get('item-search').value,'item');
  assert.equal(p.run('draft.name'),'未保存规则');assert.equal(p.run('dirty'),true);
  assert.equal(p.run("selectedItems.has('item-1')"),true);
  assert.deepEqual(p.calls,['state'],'restore also requests fresh global state');
  p.pending.shift().resolve(packet());await flush();
});

test('hidden inventory still synchronizes clean automation settings and preserves an unsaved automation draft',()=>{
  const p=page();
  const settings={carriage:{enabled:false,types:[]},gems:{enabled:true,pages:[5]},runes:{enabled:true,pages:[4]},
    pressure:{enabled:false,threshold:1,target_free:5,equipment_filter:'all',pages:[1]}};
  p.setState(packet({automation:{settings,running:false,status:'待命'}}));
  p.run('renderState();');
  assert.equal(p.run('automationDraft.gems.pages[0]'),5);
  p.run('automationDraft.pressure.target_free=9;automationDirty=true;');
  settings.pressure.target_free=6;
  p.run('renderState();');
  assert.equal(p.run('automationDraft.pressure.target_free'),9);
  assert.equal(p.run('automationDirty'),true);
  assert.equal(p.ids.get('character-content').writes,0);
  assert.equal(p.ids.get('item-rows').writes,0);
});

test('an open equipment preview refreshes and closes on disappearance even when its inventory page is hidden',()=>{
  const p=page();p.run("switchTab('inventory');openEquippedPreview('worn');switchTab('rules');");
  const body=p.ids.get('character-content').writes;
  p.run("state.character.equipment[0].name='新版装备';renderState();");
  assert.match(p.ids.get('equipped-preview').innerHTML,/新版装备/);
  assert.equal(p.ids.get('character-content').writes,body);
  p.run('state.character.equipment=[];renderState();');
  assert.equal(p.ids.get('equipped-dialog').open,false);
  assert.equal(p.run('equippedPreviewId'),null);
});

test('logs use one request in flight and periodic refreshes do not invalidate it',async()=>{
  const p=page();const first=p.run('loadLogs()');
  for(let n=0;n<8;n++)p.run('loadLogs(true)');
  assert.equal(p.calls.length,1);
  p.pending.shift().resolve({total:8,next_before:4,entries:[]});await first;
  assert.equal(p.ids.get('log-total').textContent,'共 8 条记录');
  assert.equal(p.calls.length,1,'quiet timers do not queue another slow request');
  assert.equal(p.run('logInFlight'),null);
});

test('identical polls retain connection and history nodes while real status/history changes render immediately',()=>{
  const p=page();p.setState(packet({history:[{time:'12:00',text:'正在整理'}]}));p.run('renderState();');
  const history=p.ids.get('history-list'),connection=p.ids.get('connection-state');
  const historyWrites=history.writes,connectionWrites=connection.writes;
  for(let n=0;n<100;n++){p.setState(packet({history:[{time:'12:00',text:'正在整理'}]}));p.run('renderState();');}
  assert.equal(history.writes,historyWrites);assert.equal(connection.writes,connectionWrites);
  p.setState(packet({busy:true,history:[{time:'12:01',text:'完成 <保护>'}]}));p.run('renderState();');
  assert.equal(history.writes,historyWrites+1);assert.match(history.innerHTML,/完成 &lt;保护&gt;/);
  assert.equal(connection.writes,connectionWrites+1);assert.match(connection.innerHTML,/处理中/);
  p.setState(packet({connected:false}));p.run('renderState();');
  assert.match(connection.innerHTML,/未连接游戏/);assert.match(history.innerHTML,/尚无操作记录/);
});

test('logs coalesce user filters/pages to the latest snapshot while ignoring obsolete results',async()=>{
  const p=page();const first=p.run('loadLogs()');
  p.ids.get('log-search').value='旧查询';p.run('loadLogs()');
  p.ids.get('log-search').value='最新查询';p.run('logBefore=123;loadLogs();');
  p.ids.get('log-search').value='尚未完成输入';p.run('loadLogs(true);');
  assert.equal(p.calls.length,1);
  p.pending.shift().resolve({total:99,next_before:50,entries:[]});await flush();
  assert.equal(p.calls.length,2);
  const query=new URLSearchParams(p.calls[1].split('?')[1]);
  assert.equal(query.get('query'),'最新查询');assert.equal(query.get('before'),'123');
  assert.equal(p.ids.get('log-total').textContent,'','obsolete response is not painted');
  p.pending.shift().resolve({total:3,next_before:null,entries:[]});await first;
  assert.equal(p.ids.get('log-total').textContent,'共 3 条记录');
  assert.equal(p.ids.get('log-latest').disabled,false,'the completed page is not silently reset by a timer');
  assert.equal(p.calls.length,2);
});

test('a failed stale logs request drains the latest request and releases singleflight for retries',async()=>{
  const p=page();const first=p.run('loadLogs()');
  p.ids.get('log-search').value='重试查询';p.run('loadLogs()');
  p.pending.shift().reject(new Error('old request failed'));await flush();
  assert.equal(p.ids.get('log-error').textContent,'');
  p.pending.shift().reject(new Error('new request failed'));await first;
  assert.equal(p.ids.get('log-error').textContent,'new request failed');
  assert.equal(p.run('logInFlight'),null);
  const retry=p.run('loadLogs()');p.pending.shift().resolve({total:1,next_before:null,entries:[]});await retry;
  assert.equal(p.ids.get('log-error').hidden,true);
  assert.equal(p.ids.get('log-total').textContent,'共 1 条记录');
});
