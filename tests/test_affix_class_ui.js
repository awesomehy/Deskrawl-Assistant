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
      addEventListener(name,handler){this.events[name]=handler;},classList:{toggle(){}},setAttribute(){}});
    return ids.get(id);
  }
  const directory=path.join(__dirname,'../deskrawl_assistant/web');
  const document={getElementById:element,querySelector:()=>({content:'test-token',addEventListener(){}}),
    querySelectorAll:()=>[],addEventListener(){}};
  const context=vm.createContext({document,location:{search:''},URLSearchParams,console,
    window:{addEventListener(){}},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){}});
  vm.runInContext(fs.readFileSync(path.join(directory,'app.js'),'utf8').replace(/^boot\(\);$/m,''),context);
  const run=code=>vm.runInContext(code,context);
  const dataDirectory=path.join(__dirname,'../data');
  const raw=JSON.parse(fs.readFileSync(path.join(dataDirectory,'affix-groups-static.json'),'utf8'));
  const filters=JSON.parse(fs.readFileSync(path.join(dataDirectory,'affix-class-filters.json'),'utf8'));
  const weapon=raw.slotPools.find(p=>p.slot==='Weapon');
  const pools={Weapon:{primary:weapon.primary,secondary:weapon.secondary}};
  const class_pools=Object.fromEntries(Object.values(filters.classes).map(value=>[value,{Weapon:{
    primary:weapon.primary.filter(key=>!(value&(filters.primary_exclusion_masks[key]||0))),secondary:weapon.secondary}}]));
  context.fixture={pools,class_pools,classes:[{value:1,name:'战士'},{value:2,name:'法师'},{value:4,name:'猎人'},{value:8,name:'武僧'}]};
  run(`catalog=fixture;gearKey='bow';gearMap.set('bow',{key:'bow',slot:'Weapon',class_mask:15,name:'测试传说',en:'Test'});
    for(const key of [...catalog.pools.Weapon.primary,...catalog.pools.Weapon.secondary])statMap.set(key,{name:key,en:key});
    state={rules:[]};draft=makeDraft();affixClass=1;`);
  return {run,ids,context};
}

test('primary candidates follow the selected profession while secondary stays native',()=>{
  const p=page();
  const warrior=p.run('groupMarkup("primary","主词条")');
  assert.match(warrior,/data-affix="Stats.Strength"/);
  assert.doesNotMatch(warrior,/data-affix="Stats.Dexterity"|data-affix="Stats.DamageCold"/);
  const secondary=p.run('groupMarkup("secondary","副词条")');
  p.run('affixClass=4');
  const hunter=p.run('groupMarkup("primary","主词条")');
  assert.match(hunter,/data-affix="Stats.Dexterity"/);assert.match(hunter,/data-affix="Stats.DamageArcane"/);
  assert.doesNotMatch(hunter,/data-affix="Stats.Strength"|data-affix="Stats.DamageFire"/);
  assert.equal(p.run('groupMarkup("secondary","副词条")'),secondary);
});

test('switching view keeps existing cross-class choices checked and saveable',()=>{
  const p=page();
  p.run('draft.primary=["Stats.Intelligence","Stats.DamageCold"];draft.primaryCount=2;draft.name="保留原组合";dirty=true;affixClass=1;');
  const before=p.run('JSON.stringify(draft)');
  const markup=p.run('groupMarkup("primary","主词条")');
  assert.match(markup,/Stats.Intelligence" data-group="primary" checked/);
  assert.match(markup,/其他职业已选/);assert.doesNotMatch(markup,/不参与筛选，请取消勾选/);
  assert.equal(p.run('validation()'),'');
  p.ids.get('rule-editor').events.change({target:{id:'affix-class',value:'2',dataset:{}}});
  assert.equal(p.run('affixClass'),2);assert.equal(p.run('JSON.stringify(draft)'),before);
  assert.equal(p.run('dirty'),true);assert.match(p.ids.get('rule-editor').innerHTML,/词条池职业/);
});

test('invalid legacy headline choices remain visible and invalid rather than silently erased',()=>{
  const p=page();p.run('draft.primary=["Stats.WeaponDamage"];');
  assert.match(p.run('groupMarkup("primary","主词条")'),/不参与筛选，请取消勾选/);
  assert.match(p.run('validation()'),/不参与此部位/);
  assert.equal(p.run('draft.primary[0]'),'Stats.WeaponDamage');
});

test('profession context prefers list filter, then explicit view, then connected class',()=>{
  const p=page();
  p.run('selectedClass=2;affixClass=4;');assert.equal(p.run('preferredAffixClass(gearMap.get(gearKey))'),2);
  p.run('selectedClass=0;');assert.equal(p.run('preferredAffixClass(gearMap.get(gearKey))'),4);
  p.run('affixClass=null;state.character={available:true,class_mask:8};');
  assert.equal(p.run('preferredAffixClass(gearMap.get(gearKey))'),8);
  p.run('state.character.available=false;gearMap.get(gearKey).class_mask=4;');
  assert.equal(p.run('preferredAffixClass(gearMap.get(gearKey))'),4);
  p.run('affixClass=2;');assert.equal(p.run('preferredAffixClass(gearMap.get(gearKey))'),4);
  p.run('affixClass=0;');assert.equal(p.run('preferredAffixClass(gearMap.get(gearKey))'),0);
});

test('left profession tabs update candidates without rewriting or marking the rule dirty',()=>{
  const p=page();p.run('renderCatalog=()=>{};draft.primary=["Stats.Strength"];');
  const before=p.run('JSON.stringify(draft)');
  p.ids.get('class-filters').events.click({target:{closest:()=>({dataset:{class:'8'}})}});
  assert.equal(p.run('affixClass'),8);assert.equal(p.run('JSON.stringify(draft)'),before);
  assert.equal(p.run('dirty'),false);assert.match(p.run('groupMarkup("primary","主词条")'),/其他职业已选/);
});

test('all-profession view and saving retain the original rule format',async()=>{
  const p=page();p.run('affixClass=0;draft.primary=["Stats.Strength","Stats.Dexterity"];');
  assert.deepEqual(JSON.parse(p.run('JSON.stringify(currentAffixPool("primary"))')),p.context.fixture.pools.Weapon.primary);
  let submitted;
  p.context.capture=value=>{submitted=value;};
  p.run('api=async(path,data)=>{capture(data);return {rule:{id:"saved"}}};pollState=async()=>{};toast=()=>{};');
  assert.equal(await p.run('saveRule()'),true);
  assert.deepEqual(JSON.parse(JSON.stringify(submitted.groups.primary.selected_stats)),['Stats.Strength','Stats.Dexterity']);
  assert.equal(submitted.group_mode,'all');assert.equal('affix_class' in submitted,false);
});
