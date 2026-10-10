const {test}=require('node:test');
const assert=require('node:assert/strict');
const {select}=require('../deskrawl_assistant/web/inventory-tools.js');
const gear=(id,extra={})=>({selection_id:id,name:id,kind:'equipment',is_equipment:true,container:'storage',
  slot_index:1,slot:'Chest',class_mask:15,rarity:3,level:800,upgrade:5,sockets:2,locked:false,
  matches:false,review:false,power:{available:true,delta:10,percent:1},...extra});
const ids=(items,f,role)=>select(items,f,role).map(i=>i.selection_id);

test('class, slot, level, upgrade, socket and rule filters combine',()=>{
  const items=[gear('wanted',{class_mask:4,matches:true}),gear('wrong role',{class_mask:1,matches:true}),
    gear('old',{level:700,matches:true}),gear('no socket',{sockets:0,matches:true}),gear('no rule')];
  assert.deepEqual(ids(items,{class:'current',slot:'Chest',levelMin:780,levelMax:850,upgrade:5,sockets:2,lock:'matches'},4),['wanted']);
  assert.deepEqual(ids(items,{class:'current'},0),[]);
});
test('ancient, mist and divine tags intersect and retain independent flags',()=>{
  const items=[gear('all',{is_ancient:true,is_black_mist:true,is_divine:true,rarity:4}),
    gear('ancient',{is_ancient:true}),gear('mist',{is_black_mist:true}),gear('divine',{is_divine:true,rarity:4})];
  assert.deepEqual(ids(items,{ancient:true,mist:true,divine:true}),['all']);
  assert.deepEqual(ids(items,{divine:true,rarity:'4'}),['all','divine']);
});
test('unknown comparison never becomes unchanged or a zero gain when sorted',()=>{
  const items=[gear('unknown',{power:{available:false}}),gear('same',{power:{available:true,delta:0,percent:0}}),
    gear('loss',{power:{available:true,delta:-3,percent:-1}}),gear('gain',{power:{available:true,delta:8,percent:2}})];
  assert.deepEqual(ids(items,{power:'equal'}),['same']);
  assert.deepEqual(ids(items,{power:'up'}),['gain']);
  assert.deepEqual(ids(items,{power:'down'}),['loss']);
  assert.deepEqual(ids(items,{power:'unavailable'}),['unknown']);
  assert.deepEqual(ids(items,{sort:'power-asc'}),['loss','same','gain','unknown']);
  assert.deepEqual(ids(items,{sort:'power-desc'}),['gain','same','loss','unknown']);
});
test('materials remain visible until an equipment filter is applied',()=>{
  const items=[gear('armor'),{selection_id:'gem',name:'钻石',en:'Diamond',kind:'gem',container:'inventory',slot_index:0,count:3}];
  assert.deepEqual(ids(items,{}),['gem','armor']);
  assert.deepEqual(ids(items,{search:'DIAMOND',kind:'gem'}),['gem']);
  assert.deepEqual(ids(items,{sockets:'none'}),[]);
  assert.deepEqual(ids(items,{class:'4'}),['armor']);
});
test('known numeric values sort before missing values in either direction',()=>{
  const items=[gear('unknown',{level:null}),gear('low',{level:100}),gear('high',{level:900})];
  assert.deepEqual(ids(items,{sort:'level-asc'}),['low','high','unknown']);
  assert.deepEqual(ids(items,{sort:'level-desc'}),['high','low','unknown']);
});
test('review is distinct from miss and perfect, and filtering does not mutate items',()=>{
  const items=[gear('review',{review:true}),gear('miss'),gear('perfect',{matches:true,primary_perfect:true}),
    gear('partial',{matches:true})];const before=JSON.stringify(items);
  assert.deepEqual(ids(items,{lock:'miss'}),['miss']);
  assert.deepEqual(ids(items,{lock:'review'}),['review']);
  assert.deepEqual(ids(items,{lock:'perfect'}),['perfect']);
  assert.equal(JSON.stringify(items),before);
});
test('column filters combine with AND while choices in one column use OR',()=>{
  const items=[gear('bag match',{container:'inventory',locked:true,matches:true}),
    gear('store perfect',{locked:true,primary_perfect:true}),gear('store miss'),
    gear('bag unlocked',{container:'inventory',matches:true}),gear('store review',{review:true})];
  assert.deepEqual(ids(items,{containers:['inventory','storage'],locks:['locked'],rules:['matches','perfect']}),['bag match','store perfect']);
  assert.deepEqual(ids(items,{containers:['storage'],rules:['miss']}),['store miss','store perfect']);
});
test('null clears a column, empty choices hide everything, unknown lock is excluded',()=>{
  const items=[gear('locked',{locked:true}),gear('unlocked'),gear('unknown',{locked:null}),
    {selection_id:'gem',name:'gem',container:'inventory',slot_index:0,kind:'gem'}];
  assert.deepEqual(ids(items,{containers:null,locks:null,rules:null}),['gem','locked','unknown','unlocked']);
  for(const key of ['containers','locks','rules'])assert.deepEqual(ids(items,{[key]:[]}),[]);
  assert.deepEqual(ids(items,{locks:['locked','unlocked']}),['locked','unlocked']);
});
test('position ascending and descending compare the displayed index across both containers',()=>{
  const items=[gear('bag 8',{container:'inventory',slot_index:7}),gear('store 2',{slot_index:1}),
    gear('bag 2',{container:'inventory',slot_index:1}),gear('store 9',{slot_index:8})];
  assert.deepEqual(ids(items,{sort:'position-asc'}),['bag 2','store 2','bag 8','store 9']);
  assert.deepEqual(ids(items,{sort:'position-desc'}),['store 9','bag 8','bag 2','store 2']);
});
test('socket ordering uses total holes and ignores installed gem count',()=>{
  const items=[gear('three empty',{sockets:3,occupied_sockets:0}),gear('one filled',{sockets:1,occupied_sockets:1}),
    gear('two filled',{sockets:2,occupied_sockets:2}),gear('unknown',{sockets:null,occupied_sockets:0})];
  assert.deepEqual(ids(items,{sort:'sockets-desc'}),['three empty','two filled','one filled','unknown']);
  assert.deepEqual(ids(items,{sort:'sockets-asc'}),['one filled','two filled','three empty','unknown']);
});
test('upgrade sorts in both directions, and clearing one filter retains another',()=>{
  const items=[gear('high',{upgrade:10,locked:true}),gear('low',{upgrade:0,locked:true}),gear('unlocked',{upgrade:5})];
  assert.deepEqual(ids(items,{sort:'upgrade-asc',locks:['locked'],containers:null}),['low','high']);
  assert.deepEqual(ids(items,{sort:'upgrade-desc',locks:['locked'],containers:null}),['high','low']);
});
test('materials with placeholder zeros have no meaningful upgrade or socket rank',()=>{
  const items=[gear('armor',{upgrade:1,sockets:1}),{selection_id:'gem',name:'gem',container:'inventory',slot_index:0,kind:'gem',upgrade:0,sockets:0}];
  for(const sort of ['upgrade-asc','upgrade-desc','sockets-asc','sockets-desc'])assert.deepEqual(ids(items,{sort}),['armor','gem']);
});
