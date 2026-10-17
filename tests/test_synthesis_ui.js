const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const ui=require('../deskrawl_assistant/web/managed-mode.js');

test('synthesis is opt-in and quality caps and exclusions survive configuration saving',()=>{
  const defaults=ui.normalizeSettings();
  assert.deepEqual(defaults.synthesis,{gems:{enabled:false,max_quality:5},runes:{enabled:false,max_quality:'rare',excluded_keys:[]}});
  const old={auto_equip:{enabled:true,slot_locks:{6:'LegendaryRing1'}},cleanup:{rune_pages:[2,4],gem_pages:[3]},
    synthesis:{gems:{enabled:true,max_quality:5},runes:{enabled:true,max_quality:'set',excluded_keys:['RareRune_A','UncommonRune_B','RareRune_A']}}};
  const settings=ui.normalizeSettings(old),saved=ui.normalizeSettings(JSON.parse(JSON.stringify(settings)));
  assert.deepEqual(saved,settings);assert.deepEqual(saved.auto_equip.slot_locks,old.auto_equip.slot_locks);
  assert.deepEqual(saved.cleanup.rune_pages,[2,4]);assert.deepEqual(saved.cleanup.gem_pages,[3]);
  assert.deepEqual(saved.synthesis.runes.excluded_keys,['RareRune_A','UncommonRune_B']);
  assert.equal(ui.normalizeSynthesis({gems:{max_quality:7},runes:{max_quality:'invalid'}}).gems.max_quality,5);
  defaults.synthesis.runes.excluded_keys.push('RareRune_A');assert.deepEqual(ui.normalizeSettings().synthesis.runes.excluded_keys,[]);
});

test('all rune and valuable material disposal policies migrate to storage without replacing pages',()=>{
  const cleanup={equipment_action:'salvage',equipment_pages:[1],rune_action:'sell',rune_pages:[2,4],gem_action:'sell',gem_pages:[3],gold_bar_action:'keep',gold_bar_pages:[5],legendary_rein_action:'keep',legendary_rein_pages:[6]};
  const next=ui.normalizeSettings({cleanup}).cleanup;
  for(const kind of ['rune','gem','gold_bar','legendary_rein']){
    assert.equal(next[kind+'_action'],'store');assert.deepEqual(next[kind+'_pages'],cleanup[kind+'_pages']);
  }
  assert.equal(next.equipment_action,'salvage');
  const settings=ui.normalizeSettings({farming:{enabled:true,dungeon_id:1},cleanup});
  for(const policy of Object.values(settings.cleanup.equipment_policies))policy.action=policy.fallback='salvage';
  assert.equal(ui.requiredCapabilities(settings).includes('sell'),false);
});

test('synthesis alone requires cleanup capabilities but no travel or difficulty',()=>{
  const settings=ui.normalizeSettings({background:{rule_monitoring:false,inventory_automation:false}});
  assert.equal(ui.requiredCapabilities(settings).includes('gem_synthesis'),false);
  assert.equal(ui.requiredCapabilities(settings).includes('rune_synthesis'),false);
  settings.synthesis.gems.enabled=true;
  const base=['transfer','lock','chests','ground_pickup','gem_synthesis','sell'];
  assert.deepEqual(ui.requiredCapabilities(settings),base);
  const capabilities=Object.fromEntries(base.map(key=>[key,true]));
  assert.equal(ui.controlState({capabilities},settings,true).canStart,true);
  assert.deepEqual(ui.controlState({capabilities:{gem_synthesis:true,transfer:true}},settings,true).missing,['lock','chests','ground_pickup','sell']);
  settings.synthesis.runes.enabled=true;
  assert.deepEqual(ui.requiredCapabilities(settings),[...base.slice(0,5),'rune_synthesis',...base.slice(5)]);
  assert.deepEqual(ui.controlState({capabilities},settings,true).missing,['rune_synthesis']);
  capabilities.rune_synthesis=true;
  assert.equal(ui.controlState({capabilities},settings,true).canStart,true);
  settings.synthesis.gems.enabled=false;
  assert.deepEqual(ui.requiredCapabilities(settings),[...base.slice(0,4),'rune_synthesis',...base.slice(5)]);
  for(const policy of Object.values(settings.cleanup.equipment_policies))policy.action=policy.fallback='salvage';
  assert.equal(ui.requiredCapabilities(settings).includes('sell'),false);
  assert.equal(ui.requiredCapabilities(settings).includes('travel'),false);
  assert.equal(ui.requiredCapabilities(settings).includes('difficulty'),false);
});

test('rune picker includes not-owned runes from every class with quality and icons',()=>{
  const itemUi=JSON.parse(fs.readFileSync('data/item-ui.json','utf8'));
  const catalog={collectibles:Object.values(itemUi.collectibles)};
  const expected=Object.values(itemUi.collectibles).filter(row=>row.kind==='rune');
  const rows=ui.synthesisRunes(catalog);
  assert.equal(rows.length,expected.length);assert.ok(rows.length>150);
  assert.ok(rows.some(row=>row.key.startsWith('HunterSetRune')));
  for(const prefix of ['Sorcerer','Warrior','Monk'])assert.ok(rows.some(row=>row.key.startsWith(prefix+'SetRune')));
  assert.deepEqual(new Set(rows.map(row=>row.quality)),new Set(['uncommon','rare','grand','set']));
  assert.ok(rows.every(row=>row.icon.startsWith('/icons/')&&row.name&&row.quality_name));
  const target=rows.find(row=>row.quality==='rare');
  assert.ok(ui.synthesisRunes(catalog,target.name).some(row=>row.key===target.key));
  assert.deepEqual(ui.synthesisRunes(catalog,'宏伟').map(row=>row.quality).filter(q=>q!=='grand'),[]);
  assert.equal(ui.synthesisRunes(catalog,'a-key-that-does-not-exist').length,0);
});

test('every supported cap retains its exact boundary including the lowest no-recipe cap',()=>{
  for(let max_quality=1;max_quality<=6;max_quality++)assert.equal(ui.normalizeSynthesis({gems:{enabled:true,max_quality}}).gems.max_quality,max_quality);
  for(const max_quality of ['uncommon','rare','grand','set'])assert.equal(ui.normalizeSynthesis({runes:{enabled:true,max_quality}}).runes.max_quality,max_quality);
  assert.deepEqual(ui.gemQualities,['粗糙','碎裂','粗砺','抛光','无瑕','璀璨']);
});

test('filtered rune choices preserve hidden exclusions and unlisted saved choices remain removable',()=>{
  const keys=['RareRune_A','LegendaryRune_B','unlisted-rune'];
  const selected=ui.updateRuneExclusions(keys,[{key:'RareRune_A',excluded:false},{key:'RareRune_C',excluded:true}]);
  assert.deepEqual(selected,['LegendaryRune_B','RareRune_C','unlisted-rune']);
  const list=ui.synthesisRunes({collectibles:[]},'',selected);
  assert.equal(list.length,3);assert.ok(list.every(row=>row.unknown));
  assert.deepEqual(ui.normalizeSettings({synthesis:{runes:{excluded_keys:selected}}}).synthesis.runes.excluded_keys,selected);
});

test('synthesis folded summary describes output caps and both defaults stay folded',()=>{
  assert.equal(ui.defaultFolds({}).synthesis,false);
  const settings={synthesis:{gems:{enabled:true,max_quality:5},runes:{enabled:true,max_quality:'set',excluded_keys:['RareRune_A','RareRune_B']}}};
  assert.equal(ui.defaultFolds(settings).synthesis,true);assert.equal(ui.defaultFolds(settings).cleanup,true);
  const summary=ui.foldSummaries(settings,null).synthesis;
  assert.equal(summary.enabled,true);assert.match(summary.summary,/宝石 → 无瑕/);
  assert.match(summary.summary,/符文 → 符文套装/);assert.match(summary.summary,/跳过 2 种/);
});
