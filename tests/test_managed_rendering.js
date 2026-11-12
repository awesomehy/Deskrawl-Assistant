'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const managed=require('../deskrawl_assistant/web/managed-mode.js');
const growth=require('../deskrawl_assistant/web/managed-growth.js');
const automation=require('../deskrawl_assistant/web/automation-tools.js');
const auction=require('../deskrawl_assistant/web/auction-ui.js');
const clone=value=>JSON.parse(JSON.stringify(value));

function page(){
  const nodes=new Map(),growthCalls=[],timers=[];
  function node(id){
    if(!nodes.has(id)){
      let markup='',text='';
      nodes.set(id,{id,value:'',checked:false,hidden:false,disabled:false,options:[],dataset:{},events:{},writes:0,textWrites:0,
        get innerHTML(){return markup;},set innerHTML(value){markup=value;this.writes++;},
        get textContent(){return text;},set textContent(value){text=String(value);this.textWrites++;},
        addEventListener(type,fn){this.events[type]=fn;},setAttribute(){},querySelectorAll(){return [];},
        classList:{toggle(){}},contains(){return false;}});
    }
    return nodes.get(id);
  }
  const document={getElementById:node,querySelector:node,querySelectorAll(){return [];},addEventListener(){},activeElement:null};
  let onShared;
  const sharedStorage={current:{rune:[4],gem:[5]},saved:{rune:[4],gem:[5]},subscribe(fn){onShared=fn;}};
  const window={document,AutomationTools:automation,ManagedGrowthUI:{helpers:growth,init(){},render(...args){growthCalls.push(args);}},
    AuctionUI:{helpers:auction,init(){},render(){}},setInterval(fn){timers.push(fn);},addEventListener(){}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../deskrawl_assistant/web/managed-mode.js'),'utf8'),{window});
  const ui=window.ManagedModeUI;ui.init({catalog:{},sharedStorage,api:async()=>({}),onDirty(){}});
  const settings=managed.normalizeSettings({auto_equip:{enabled:true}});
  const packet={settings,warehouse_ready:true,warehouse_pages:[{index:1,capacity:50,free:20},{index:4,capacity:50,free:10},{index:5,capacity:50,free:10}],
    capabilities:{transfer:true,equip:true,lock:true},progression_state:{available:true,class_mask:4},stats:{gold:{count:10,available:true}},history:[]};
  const apply=(value=packet)=>ui.applyPacket(clone(value));
  ui.setConnection(true);apply();
  return {ui,node,apply,packet,growthCalls,sharedStorage,sharedChanged:()=>onShared()};
}

test('unchanged managed polls retain warehouse choices/capabilities and do not repeat connection rendering',()=>{
  const p=page(),pages=p.node('managed-rune-pages'),caps=p.node('managed-capabilities');
  const pageWrites=pages.writes,capWrites=caps.writes,calls=p.growthCalls.length;
  for(let n=0;n<100;n++){p.ui.setConnection(true);p.apply();}
  assert.equal(pages.writes,pageWrites);assert.equal(caps.writes,capWrites);
  assert.equal(p.growthCalls.length,calls+100,'each new packet renders live growth once');
  p.packet.warehouse_pages[1].free=2;p.apply();
  assert.equal(pages.writes,pageWrites+1);assert.match(pages.innerHTML,/2 空/);
  p.packet.capabilities.equip={available:false,reason:'等待核验'};p.apply();
  assert.equal(caps.writes,capWrites+1);assert.match(caps.innerHTML,/等待核验/);
});

test('gold updates preserve item bucket nodes while actual new loot still updates the right bucket',()=>{
  const p=page(),ids=['managed-equipment-stats','managed-minion-stats','managed-rune-stats','managed-gem-stats','managed-other-stats'];
  const before=ids.map(id=>p.node(id).writes);
  for(let n=0;n<100;n++){p.packet.stats.gold.count++;p.apply();}
  assert.deepEqual(ids.map(id=>p.node(id).writes),before);assert.equal(p.node('managed-gold-total').textContent,'110');
  p.packet.stats.runes=[{name:'测试符文',count:2}];p.apply();
  assert.equal(p.node('managed-rune-stats').writes,before[2]+1);assert.match(p.node('managed-rune-stats').innerHTML,/测试符文/);
  assert.equal(p.node('managed-equipment-stats').writes,before[0]);
});

test('shared destination edits and reconnection still invalidate cached controls and old growth state',()=>{
  const p=page(),runes=p.node('managed-rune-pages'),gems=p.node('managed-gem-pages');
  const runeWrites=runes.writes;
  p.sharedStorage.current={rune:[5],gem:[4]};p.sharedChanged();
  assert.equal(runes.writes,runeWrites+1);assert.match(runes.innerHTML,/value="5" checked/);
  assert.match(gems.innerHTML,/value="4" checked/);assert.equal(p.ui.isDirty(),true);
  p.apply();assert.match(runes.innerHTML,/value="5" checked/,'polls preserve the unsaved shared selection');
  p.ui.setConnection(false);assert.equal(p.growthCalls.at(-1)[5],false);
  assert.equal(p.growthCalls.at(-1)[2].available,false);
  p.ui.setConnection(false);p.ui.setConnection(true);
  assert.equal(p.growthCalls.at(-1)[2].available,false,'reconnecting cannot revive the old character snapshot');
  p.apply();assert.equal(p.growthCalls.at(-1)[2].available,true);
});
