'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),{test}=require('node:test');
const source=fs.readFileSync('deskrawl_assistant/web/assistant-settings.js','utf8');
const appSource=fs.readFileSync('deskrawl_assistant/web/app.js','utf8');
const confirmationSource=appSource.slice(appSource.indexOf('function confirmAction('),appSource.indexOf('function classesLabel('));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const defaults={steam_acceleration_enabled:false,boss_hotkey:'',desktop_supported:true,data_directory:'C:\\Test data',hotkey:{error:''}};

function page(options={}){
  const nodes={},calls=[],pending=[],toasts=[];
  let saved={...defaults,...options.saved},refreshes=0;
  const document={activeElement:null,getElementById:id=>nodes[id]};
  for(const id of ['dialog','open','steam','hotkey','save','error','steam-state','hotkey-status','hotkey-clear','open-folder','close','cancel','data-path','loading','form','reset-rules','reset-automation','reset-cache','reset-result']){
    nodes['settings-'+id]={value:'',textContent:'',hidden:false,disabled:false,open:false,attributes:{},handlers:{},placeholder:'',
      classList:{add(){},remove(){}},setAttribute(key,value){this.attributes[key]=value;},
      addEventListener(type,handler){this.handlers[type]=handler;},showModal(){this.open=true;},close(){this.open=false;},
      focus(){document.activeElement=this;return this.handlers.focus?.();},
      blur(){document.activeElement=null;return this.handlers.blur?.();}};
  }
  const context={document,window:{SteamMarketUI:{retry(){refreshes++;}},AssistantConfigurationUI:{refresh:options.refresh||(()=>{})}},confirmAction:async()=>options.confirm!==false,toast:message=>toasts.push(message),
    setTimeout:()=>1,clearTimeout(){},api:(path,data)=>{
      calls.push({path,data:data===undefined?undefined:JSON.parse(JSON.stringify(data))});
      if(path==='settings/reset')return options.resetError?Promise.reject(new Error(options.resetError)):Promise.resolve({backup_directory:'C:\\Backups'});
      if(path==='window/hotkey-capture'&&options.captureApi)return options.captureApi(data);
      if(path==='settings'&&data===undefined&&!options.deferRead)return Promise.resolve({...saved});
      if(path==='settings')return new Promise((resolve,reject)=>pending.push({
        resolve(value=data?{...saved,...data}:saved){saved={...value};resolve({...saved});},reject}));
      return Promise.resolve({});
    }};
  vm.createContext(context);
  if(options.realConfirmation){
    for(const name of ['dialog','title','body','ok','cancel']){
      nodes['confirm-'+name]={open:false,textContent:'',className:'',onclick:null,oncancel:null,
        showModal(){this.open=true;},close(){this.open=false;}};
    }
    vm.runInContext('const $=id=>document.getElementById(id);\n'+confirmationSource,context);
  }
  vm.runInContext(source,context);
  return {nodes,calls,pending,toasts,get refreshes(){return refreshes;},get saved(){return saved;},
    async fire(id,type='click',props={}){const el=nodes['settings-'+id];if(type==='click'&&el.disabled)return;
      await el.handlers[type]?.({preventDefault(){},stopPropagation(){},...props});},
    async open(){await nodes['settings-open'].handlers.click();},
    async key(props){await nodes['settings-hotkey'].handlers.keydown({preventDefault(){},stopPropagation(){},...props});}};
}

test('new settings open disabled acceleration and cancel discards a draft',async()=>{
  const p=page();await p.open();
  assert.equal(p.nodes['settings-steam'].attributes['aria-checked'],'false');
  await p.fire('steam');assert.equal(p.nodes['settings-steam'].attributes['aria-checked'],'true');
  await p.fire('cancel');assert.equal(p.nodes['settings-dialog'].open,false);
  await p.open();assert.equal(p.nodes['settings-steam'].attributes['aria-checked'],'false');
  assert.equal(p.calls.filter(call=>call.path==='settings'&&call.data!==undefined).length,0);
});

test('save blocks closing until its response and refreshes quotes once after a change',async()=>{
  const p=page();await p.open();await p.fire('steam');
  const request=p.fire('form','submit');await flush();
  assert.equal(p.nodes['settings-save'].disabled,true);
  await p.fire('dialog','cancel');await p.fire('close');
  assert.equal(p.nodes['settings-dialog'].open,true);
  assert.deepEqual(p.calls.at(-1),{path:'settings',data:{steam_acceleration_enabled:true,boss_hotkey:''}});
  p.pending.shift().resolve();await request;
  assert.equal(p.nodes['settings-dialog'].open,false);assert.equal(p.refreshes,1);
  await p.open();assert.equal(p.nodes['settings-steam'].attributes['aria-checked'],'true');
});

test('a failed or interrupted save reads back actual state and shows the error',async()=>{
  const p=page();await p.open();await p.fire('steam');
  const request=p.fire('form','submit');await flush();
  p.pending.shift().reject(new Error('快捷键被占用'));await request;
  assert.equal(p.nodes['settings-steam'].attributes['aria-checked'],'false');
  assert.equal(p.nodes['settings-error'].textContent,'快捷键被占用');
  assert.equal(p.nodes['settings-save'].disabled,false);
  assert.equal(p.nodes['settings-dialog'].open,true);assert.equal(p.refreshes,0);
});

test('a late read from a canceled dialog cannot overwrite a newly opened dialog',async()=>{
  const p=page({deferRead:true});const first=p.open();
  await p.fire('cancel');const second=p.open();
  p.pending[1].resolve({...defaults,steam_acceleration_enabled:true});await second;
  p.pending[0].resolve(defaults);await first;
  assert.equal(p.nodes['settings-steam'].attributes['aria-checked'],'true');
  assert.equal(p.nodes['settings-dialog'].open,true);
});

test('recording a shortcut normalizes modifiers and always ends capture',async()=>{
  const p=page();await p.open();await p.nodes['settings-hotkey'].focus();
  await p.key({key:'h',code:'KeyH',ctrlKey:true,altKey:true,shiftKey:true});
  assert.equal(p.nodes['settings-hotkey'].value,'Ctrl+Alt+Shift+H');
  assert.deepEqual(p.calls.slice(-2),[
    {path:'window/hotkey-capture',data:{capturing:true}},
    {path:'window/hotkey-capture',data:{capturing:false}}]);
});

test('top-row and keypad digits save the same numeric shortcut for both keyboards',async()=>{
  for(const code of ['Digit0','Numpad0']){
    const p=page();await p.open();await p.nodes['settings-hotkey'].focus();
    await p.key({key:'0',code,ctrlKey:true});
    assert.equal(p.nodes['settings-hotkey'].value,'Ctrl+0');
    const saving=p.fire('form','submit');await flush();
    assert.equal(p.calls.at(-1).path,'settings');
    assert.equal(p.calls.at(-1).data.boss_hotkey,'Ctrl+0');
    p.pending.shift().resolve();await saving;
    await p.open();assert.equal(p.nodes['settings-hotkey'].value,'Ctrl+0');
  }
});

test('keypad navigation with Num Lock off does not overwrite the shortcut',async()=>{
  const p=page({saved:{boss_hotkey:'Ctrl+Alt+I'}});await p.open();
  await p.nodes['settings-hotkey'].focus();
  await p.key({key:'Insert',code:'Numpad0',ctrlKey:true});
  assert.equal(p.nodes['settings-hotkey'].value,'Ctrl+Alt+I');
  assert.equal(p.nodes['settings-error'].hidden,false);
});

test('escape keeps the old shortcut and reserved or unmodified keys are rejected',async()=>{
  const p=page({saved:{boss_hotkey:'Ctrl+H'}});await p.open();
  await p.nodes['settings-hotkey'].focus();
  for(const key of [{key:'h',code:'KeyH'},{key:'F12',code:'F12',ctrlKey:true},{key:'F4',code:'F4',altKey:true}]){
    await p.key(key);assert.equal(p.nodes['settings-hotkey'].value,'Ctrl+H');
    assert.equal(p.nodes['settings-error'].hidden,false);
  }
  await p.key({key:'Escape'});assert.equal(p.nodes['settings-hotkey'].value,'Ctrl+H');
  assert.equal(p.calls.at(-1).data.capturing,false);
});

test('browsers cannot record global shortcuts but can open the fixed data folder',async()=>{
  const p=page({saved:{desktop_supported:false}});await p.open();
  assert.equal(p.nodes['settings-hotkey'].disabled,true);
  assert.equal(p.nodes['settings-data-path'].textContent,defaults.data_directory);
  await p.fire('open-folder');assert.deepEqual(p.calls.at(-1),{path:'settings/open-folder',data:{}});
});

test('reset requires confirmation and sends only the selected category',async()=>{
  let refreshed='';const p=page({refresh:section=>{refreshed=section;}});await p.open();
  await p.fire('reset-rules');
  assert.deepEqual(p.calls.at(-1),{path:'settings/reset',data:{section:'equipment_rules'}});
  assert.equal(refreshed,'equipment_rules');
  assert.match(p.nodes['settings-reset-result'].textContent,/C:\\Backups/);
  const canceled=page({confirm:false});await canceled.open();await canceled.fire('reset-automation');
  assert.equal(canceled.calls.some(call=>call.path==='settings/reset'),false);
});

for(const [button,section,title,impact,retained,label] of [
  ['reset-rules','equipment_rules','重置装备筛选规则？','所有已保存的筛选组合','游戏内装备锁定状态','清空筛选规则'],
  ['reset-automation','automation','重置托管与背包仓库设置？','全部托管设置','登录凭证','恢复默认设置'],
  ['reset-cache','browser_cache','清理内置浏览器缓存？','网页资源缓存','全部助手配置','确认清理缓存']]){
  test(`${section} explains its effect and waits for explicit confirmation`,async()=>{
    const p=page({realConfirmation:true});await p.open();
    const resetting=p.fire(button);await flush();
    assert.equal(p.nodes['settings-dialog'].open,true);
    assert.equal(p.nodes['confirm-dialog'].open,true);
    assert.equal(p.nodes['confirm-title'].textContent,title);
    assert.ok(p.nodes['confirm-body'].textContent.includes(impact));
    assert.ok(p.nodes['confirm-body'].textContent.includes(retained));
    assert.ok(p.nodes['confirm-body'].textContent.includes('无需再点击'));
    assert.equal(p.nodes['confirm-ok'].textContent,label);
    assert.equal(p.calls.some(call=>call.path==='settings/reset'),false);
    assert.equal(p.nodes['settings-close'].disabled,true);
    p.nodes['confirm-ok'].onclick();await resetting;
    assert.deepEqual(p.calls.filter(call=>call.path==='settings/reset'),[{path:'settings/reset',data:{section}}]);
    assert.equal(p.nodes['confirm-dialog'].open,false);
    assert.equal(p.nodes['settings-dialog'].open,true);
    assert.equal(p.nodes['settings-close'].disabled,false);
  });

  test(`${section} cancel and Escape leave settings unchanged`,async()=>{
    for(const cancelBy of ['button','escape']){
      let refreshed=false;
      const p=page({realConfirmation:true,refresh:()=>{refreshed=true;}});await p.open();
      const before={...p.saved};
      const resetting=p.fire(button);await flush();
      if(cancelBy==='button')p.nodes['confirm-cancel'].onclick();
      else p.nodes['confirm-dialog'].oncancel({preventDefault(){}});
      await resetting;
      assert.equal(p.calls.some(call=>call.path==='settings/reset'),false);
      assert.deepEqual(p.saved,before);assert.equal(refreshed,false);
      assert.equal(p.nodes['settings-dialog'].open,true);
      assert.equal(p.nodes['confirm-dialog'].open,false);
      assert.equal(p.nodes['settings-save'].disabled,false);
    }
  });
}

test('failed reset shows backend refusal and keeps the dialog usable',async()=>{
  const p=page({resetError:'请先暂停托管'});await p.open();await p.fire('reset-automation');
  assert.equal(p.nodes['settings-error'].textContent,'请先暂停托管');
  assert.equal(p.nodes['settings-save'].disabled,false);assert.equal(p.nodes['settings-dialog'].open,true);
});

test('blur before capture reply serializes release before saving the shortcut',async()=>{
  let releaseStart;const p=page({captureApi:data=>data.capturing?new Promise(resolve=>{releaseStart=resolve;}):Promise.resolve({})});
  await p.open();const focusing=p.nodes['settings-hotkey'].focus();await flush();
  p.nodes['settings-hotkey'].blur();const saving=p.fire('form','submit');await flush();
  assert.equal(p.calls.some(call=>call.path==='settings'&&call.data),false);
  releaseStart({});await focusing;await flush();
  assert.deepEqual(p.calls.slice(-2).map(call=>call.path),['window/hotkey-capture','settings']);
  assert.equal(p.calls.at(-2).data.capturing,false);
  p.pending.shift().resolve();await saving;
});
