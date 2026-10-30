'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const managed=require('../deskrawl_assistant/web/managed-mode.js');
const inventory=require('../deskrawl_assistant/web/inventory-tools.js');
const auction=require('../deskrawl_assistant/web/auction-ui.js');

test('legacy character disposal does not authorize disposal or upload of minion gear',()=>{
  const settings=managed.normalizeSettings({cleanup:{equipment_action:'salvage',equipment_pages:[2,4]}});
  assert.equal(settings.cleanup.minion_equipment_action,'store');
  assert.deepEqual(settings.cleanup.minion_equipment_pages,[2,4]);
  assert.equal(settings.auction.categories.includes('minion_equipment'),false);
  assert.deepEqual(managed.normalizeSettings(settings),settings);
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
