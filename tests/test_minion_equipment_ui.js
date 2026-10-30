'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const managed=require('../deskrawl_assistant/web/managed-mode.js');
const inventory=require('../deskrawl_assistant/web/inventory-tools.js');
const auction=require('../deskrawl_assistant/web/auction-ui.js');

test('legacy character disposal does not authorize disposal or upload of minion gear',()=>{
  const settings=managed.normalizeSettings({cleanup:{equipment_action:'salvage',equipment_pages:[2,4]}});
  assert.equal(settings.cleanup.minion_equipment_action,'store');
  assert.deepEqual(settings.cleanup.minion_equipment_policies,{legendary:'store',rare:'store'});
  assert.deepEqual(settings.cleanup.minion_equipment_pages,[2,4]);
  assert.equal(settings.auction.categories.includes('minion_equipment'),false);
  assert.deepEqual(managed.normalizeSettings(settings),settings);
});

test('old servant policy migrates both qualities and new per-quality choices persist',()=>{
  for(const action of ['store','sell','salvage']){
    const raw={cleanup:{minion_equipment_action:action,minion_equipment_pages:[2,4]}};
    const settings=managed.normalizeSettings(raw);
    assert.deepEqual(settings.cleanup.minion_equipment_policies,{legendary:action,rare:action});
    settings.cleanup.minion_equipment_policies={legendary:'salvage',rare:'sell'};
    assert.deepEqual(managed.normalizeSettings(settings),settings);
    assert.deepEqual(settings.cleanup.minion_equipment_pages,[2,4]);
  }
});

test('capabilities follow both servant qualities rather than obsolete global choice',()=>{
  const settings=managed.normalizeSettings({farming:{enabled:true,dungeon_id:'a'},cleanup:{equipment_action:'store',equipment_fallback:'salvage',minion_equipment_action:'sell',minion_equipment_policies:{legendary:'store',rare:'store'}}});
  assert.equal(managed.requiredCapabilities(settings).includes('sell'),false);
  settings.cleanup.minion_equipment_policies.rare='sell';
  assert.equal(managed.requiredCapabilities(settings).includes('sell'),true);
  const html=require('node:fs').readFileSync(require('node:path').join(__dirname,'../deskrawl_assistant/web/index.html'),'utf8');
  assert.match(html,/仆从装备 · 黑雾传说<\/strong><small>固定保护，不可修改/);
  for(const quality of ['legendary','rare'])assert.match(html,new RegExp(`id="managed-minion-equipment-${quality}-action"`));
  assert.doesNotMatch(html,/id="managed-minion-equipment-action"/);
});

test('inventory filters split character and minion gear while retaining all-item view',()=>{
  const rows=[{kind:'equipment',selection_id:'minion',slot:'Minion'},
    {kind:'equipment',selection_id:'character',slot:'Boots'}, {kind:'gem',selection_id:'gem'}];
  const ids=kind=>inventory.select(rows,{kind}).map(r=>r.selection_id);
  assert.deepEqual(ids('minion_equipment'),['minion']);
  assert.deepEqual(ids('equipment'),['character']);
  assert.equal(ids('').length,3);
});

test('minion category is explicit, searchable, and blocked by item exclusions',()=>{
  const row={key:'LegendaryMinionGear1',quality:'black_mist',kind:'equipment'};
  assert.equal(auction.policyAllows({categories:['black_mist_equipment']},row.key,row),false);
  assert.equal(auction.policyAllows({categories:['minion_equipment']},row.key,row),true);
  assert.equal(auction.policyAllows({categories:['minion_equipment'],excluded_keys:[row.key]},row.key,row),false);
  assert.equal(auction.items({auction_items:[row]},'仆从装备').length,1);
});

test('statistics include minion items once alongside the character totals',()=>{
  const stats=managed.statistics({equipment:{rare:{count:3}},minion_equipment:[{name:'马鞍',count:2}]});
  assert.equal(stats.minionCount,2);assert.equal(stats.total,5);
  assert.equal(stats.minions[0].name,'马鞍');
});
