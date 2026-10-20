const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const ui=require('../deskrawl_assistant/web/managed-mode.js');

test('old presets default reconnect to off and index one without changing other choices',()=>{
  const value=ui.normalizeSettings({auto_equip:{enabled:true},cleanup:{rune_pages:[3]}});
  assert.deepEqual(value.reconnect,{enabled:false,character_index:1});
  assert.equal(value.auto_equip.enabled,true);
  assert.deepEqual(value.cleanup.rune_pages,[3]);
});
test('reconnect configuration survives normalization and has bounded integer selection',()=>{
  const cfg=ui.normalizeSettings({auto_equip:{enabled:true},reconnect:{enabled:true,character_index:4}});
  assert.deepEqual(ui.normalizeSettings(cfg).reconnect,cfg.reconnect);
  assert.equal(ui.validateSettings(cfg),'');
  for(const n of [0,-1,1.5,101]){
    cfg.reconnect.character_index=n;
    assert.match(ui.validateSettings(cfg),/角色列表序号/);
  }
});
test('optional reconnect does not introduce a grey start dependency or enable login by itself',()=>{
  const cfg=ui.normalizeSettings({auto_equip:{enabled:true}});
  const before=ui.requiredCapabilities(cfg);
  cfg.reconnect.enabled=true;
  assert.deepEqual(ui.requiredCapabilities(cfg),before);
  const packet={can_start:true,capabilities:Object.fromEntries(before.map(key=>[key,true]))};
  assert.equal(ui.controlState(packet,cfg,true).canStart,true);
  assert.equal(ui.controlState({...packet,running:true,phase:'reconnecting'},cfg,true).canPause,true);
  const empty=ui.normalizeSettings({reconnect:{enabled:true}});
  assert.match(ui.controlState({},empty,true).configurationError,/至少启用/);
});
test('status area exposes persisted reconnect controls and guarded list ordinal',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../deskrawl_assistant/web/index.html'),'utf8');
  const status=html.slice(html.indexOf('aria-label="托管运行状态"'),html.indexOf('id="managed-error"'));
  assert.match(status,/id="managed-reconnect"/);
  assert.match(status,/id="managed-reconnect-index"[^>]*min="1"[^>]*max="100"/);
  assert.match(status,/保存托管设置/);
});
