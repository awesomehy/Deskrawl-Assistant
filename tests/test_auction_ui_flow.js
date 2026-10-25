'use strict';
const fs=require('node:fs'),vm=require('node:vm'),{test}=require('node:test'),assert=require('node:assert/strict');
const managed=require('../deskrawl_assistant/web/managed-mode.js');
const source=fs.readFileSync('deskrawl_assistant/web/auction-ui.js','utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const clone=value=>JSON.parse(JSON.stringify(value));
const defaultState=()=>({login_available:true,login_reason:'',steam:{authenticated:false,login_pending:false,account_name:'',wallet_currency:null},
  upload:{phase:'idle',message:'仅上传白名单内已核验的单件物品。',listing_count:null,listing_limit:8,pending:false},
  sell:{phase:'idle',message:'',pending:0,pending_confirmation:false,confirmation_count:0},error:''});
const catalog={auction_items:[
  {key:'LegendaryBoots1',name:'风行者之履',icon:'/icons/LegendaryBoots1.png',kind:'equipment',rarity:3,quality:'black_mist'},
  {key:'DivineBow1',name:'神圣之弓',icon:'/icons/DivineBow1.png',kind:'equipment',rarity:4,quality:'divine'},
  {key:'HunterSetRune2',name:'二连矢',icon:'/icons/HunterSetRune2.png',kind:'rune',rarity:5,quality:'set'},
  {key:'GoldBar',name:'金条',icon:'/icons/GoldBar.png',kind:'material',rarity:3,quality:'legendary',available:false,support_reason:'无实例 ID 的金条／开光石暂需手动上传。'},
]};

function page(settings={}){
  const elements=new Map(),timers=[],calls=[],changes=[],toasts=[];
  const document={activeElement:null,getElementById:id=>node(id),querySelector:selector=>selector==='[data-managed-fold="auction"]'?fold:null};
  function node(id){
    if(elements.has(id))return elements.get(id);
    const events={},element={id,value:'',textContent:'',hidden:false,disabled:false,checked:false,open:false,dataset:{},choices:[],
      setAttribute(name,value){this[name]=value;},
      addEventListener(type,handler){(events[type]??=[]).push(handler);},
      emit(type,event={}){return Promise.all((events[type]||[]).map(handler=>handler(event)));},
      querySelectorAll(selector){return this.choices.filter(row=>selector==='[data-auction-category]'?row.dataset.auctionCategory!==undefined:selector==='[data-auction-item-key]'?row.dataset.auctionItemKey!==undefined:selector==='[data-auction-excluded-key]'?row.dataset.auctionExcludedKey!==undefined:false);},
      matches(selector){return selector==='input,select'&&(/enabled$|^auction-(?:allow-locked|retain-count|search|excluded-search|price-rule)$/.test(id)||!!this.dataset.auctionItemKey||!!this.dataset.auctionExcludedKey);},
      focus(){document.activeElement=this;}};
    let markup='';
    Object.defineProperty(element,'innerHTML',{get:()=>markup,set:value=>{
      markup=value;element.choices=[...value.matchAll(/<input\b([^>]*data-auction-(item-key|excluded-key|category)="([^"]+)"[^>]*)>/g)].map(([,attrs,kind,key])=>({
        dataset:{[kind==='item-key'?'auctionItemKey':kind==='category'?'auctionCategory':'auctionExcludedKey']:key},checked:/\bchecked\b/.test(attrs),disabled:/\bdisabled\b/.test(attrs),
        matches:selector=>selector==='input,select',focus(){document.activeElement=this;},id:''}));
    }});
    elements.set(id,element);return element;
  }
  const fold=node('fold-auction');
  let draft=managed.normalizeSettings(settings),apiState=defaultState(),handler=async()=>clone(apiState),confirmResult=true;
  const window={document,setInterval:(fn,ms)=>{timers.push({fn,ms});return timers.length;}};
  const context=vm.createContext({window});vm.runInContext(source,context);
  const api=async(path,data)=>{calls.push({path,...(data===undefined?{}:{data:clone(data)})});return handler(path,data);};
  window.AuctionUI.init({api,toast:message=>toasts.push(message),confirmAction:async()=>confirmResult,onChange:auction=>{
    draft=managed.normalizeSettings({...draft,auction:clone(auction)});changes.push(clone(draft));window.AuctionUI.render(draft.auction,catalog);
  }});
  window.AuctionUI.render(draft.auction,catalog);
  return {window,elements,timers,calls,changes,toasts,node,fold,get draft(){return draft;},
    setState:value=>{apiState=value;},setHandler:value=>{handler=value;},setConfirm:value=>{confirmResult=value;},
    async open(){fold.open=true;await fold.emit('toggle');await flush();},
    async poll(){for(const timer of timers)timer.fn();await flush();},
    async change(id,value){const element=node(id);if(typeof value==='boolean')element.checked=value;else element.value=String(value);
      await node('managed-auction').emit('change',{target:element});await flush();},
    async chooseCategory(key,checked){const element=node('auction-category-options').choices.find(row=>row.dataset.auctionCategory===key);
      assert.ok(element,key);element.checked=checked;await node('managed-auction').emit('change',{target:element});await flush();},
    async chooseExcluded(key,checked){const element=node('auction-excluded-options').choices.find(row=>row.dataset.auctionExcludedKey===key);
      assert.ok(element,key);element.checked=checked;await node('managed-auction').emit('change',{target:element});await flush();},
    async searchExcluded(value){node('auction-excluded-search').value=value;await node('auction-excluded-search').emit('input');await flush();},
    async click(id){
      const element=node(id),before=element.checked,event={defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};
      if(element.disabled)return event;
      if(id==='auction-sell-enabled')element.checked=!before;
      await element.emit('click',event);
      if(id==='auction-sell-enabled'){
        if(event.defaultPrevented)element.checked=before;
        else await node('managed-auction').emit('change',{target:element});
      }
      await flush();return event;
    },
    async save(){const result=clone(managed.normalizeSettings({...draft,auction:clone(window.AuctionUI.read(draft.auction))}));
      await api('managed/settings',{settings:result});return result;}};
}

test('new and pre-auction settings stay disabled and do not begin status, login or native requests',async()=>{
  for(const settings of [{},{auto_equip:{enabled:true},cleanup:{rune_pages:[2],gem_pages:[3]}}]){
    const p=page(settings);await flush();await p.poll();await p.poll();
    assert.equal(p.draft.auction.upload_enabled,false);assert.equal(p.draft.auction.sell_enabled,false);
    assert.deepEqual(p.calls,[]);assert.deepEqual(p.draft.auction.item_keys,[]);
    assert.deepEqual(p.draft.auction.excluded_keys,[]);
    assert.equal(p.node('auction-upload-enabled').checked,false);assert.equal(p.node('auction-sell-enabled').checked,false);
  }
});

test('opening an inactive module polls login status without enabling automation',async()=>{
  const p=page();await p.open();await p.poll();await p.poll();
  assert.deepEqual(p.calls,[{path:'auction/state'},{path:'auction/state'},{path:'auction/state'}]);
  assert.equal(p.node('auction-login').disabled,false);assert.equal(p.node('auction-upload-enabled').checked,false);
  assert.match(p.node('auction-listing-count').textContent,/名额待读取.*当前数量未知/);
  assert.doesNotMatch(p.node('auction-listing-count').textContent,/0 \/ 8/);
});

test('server catalog quality and material support notes render while unknown retained choices stay selectable',async()=>{
  const p=page({auction:{excluded_keys:['GoldBar','removed-item']}});
  const markup=p.node('auction-excluded-options').innerHTML;
  assert.match(markup,/data-quality="black_mist"/);assert.match(markup,/风行者之履/);
  assert.match(markup,/data-quality="set"/);assert.match(markup,/无实例 ID 的金条／开光石暂需手动上传/);
  assert.match(markup,/data-auction-excluded-key="GoldBar" checked/);assert.match(markup,/data-auction-excluded-key="removed-item" checked/);
  const material=p.node('auction-excluded-options').choices.find(row=>row.dataset.auctionExcludedKey==='GoldBar');
  assert.equal(material.disabled,false);assert.deepEqual(p.calls,[]);
});

test('search, checkbox edits and saved API configuration preserve hidden choices and unrelated managed plans',async()=>{
  const p=page({auto_equip:{enabled:true,slot_locks:{6:'LegendaryRing1'}},cleanup:{rune_pages:[2,4],gem_pages:[3]},
    synthesis:{runes:{enabled:true,excluded_keys:['RareRune_A']}},auction:{categories:['gold_bar','set_rune'],excluded_keys:['GoldBar','HunterSetRune2'],retain_count:2}});
  await p.searchExcluded('神圣');await p.chooseExcluded('DivineBow1',true);
  await p.change('auction-upload-enabled',true);const saved=await p.save();
  assert.deepEqual(saved.auction.excluded_keys,['DivineBow1','GoldBar','HunterSetRune2']);assert.equal(Object.hasOwn(saved.auction,'retain_count'),false);
  assert.deepEqual(saved.auction.item_keys,[]);assert.deepEqual(saved.auction.categories,['gold_bar','set_rune']);
  assert.equal(saved.auction.upload_enabled,true);assert.equal(saved.auction.sell_enabled,false);
  assert.deepEqual(saved.auto_equip.slot_locks,{6:'LegendaryRing1'});assert.deepEqual(saved.cleanup.rune_pages,[2,4]);
  assert.deepEqual(saved.synthesis.runes.excluded_keys,['RareRune_A']);
  const posted=p.calls.find(call=>call.path==='managed/settings');assert.deepEqual(posted.data,{settings:saved});
  assert.equal(p.calls.some(call=>!['auction/state','managed/settings'].includes(call.path)),false);
});

test('retention choices cover absent catalog items, survive searches and take precedence over categories',async()=>{
  const p=page({auto_equip:{enabled:true},auction:{categories:['gold_bar','divine_equipment'],excluded_keys:['HunterSetRune2','retired-item'],retain_count:2}});
  // No inventory snapshot is supplied: retained choices use the complete catalog.
  await p.searchExcluded('神圣');await p.chooseExcluded('DivineBow1',true);
  assert.deepEqual(p.draft.auction.categories,['divine_equipment','gold_bar']);
  assert.deepEqual(p.draft.auction.excluded_keys,['DivineBow1','HunterSetRune2','retired-item']);
  assert.equal(p.window.AuctionUI.helpers.policyAllows(p.draft.auction,'DivineBow1'),false);
  await p.searchExcluded('retired');await p.chooseExcluded('retired-item',false);
  await p.change('auction-upload-enabled',true);await p.change('auction-sell-enabled',true);
  const saved=await p.save();assert.deepEqual(saved.auction.item_keys,[]);
  assert.deepEqual(saved.auction.excluded_keys,['DivineBow1','HunterSetRune2']);assert.equal(saved.auto_equip.enabled,true);
  assert.equal(p.window.AuctionUI.helpers.policyAllows(saved.auction,'DivineBow1'),false);
  assert.equal(p.window.AuctionUI.helpers.policyAllows(saved.auction,'GoldBar'),true);
  assert.equal(p.calls.some(call=>!['auction/state','managed/settings'].includes(call.path)),false);
  const html=fs.readFileSync('deskrawl_assistant/web/index.html','utf8');
  assert.match(html,/强制保留商品（不自动上传或出售）/);assert.match(html,/锁定状态不影响上传/);
  assert.doesNotMatch(html,/id="auction-allow-locked"|id="auction-retain-count"/);
});

test('Steam login sends only an empty command and status exposes no credential fields or login inputs',async()=>{
  const p=page();await p.open();p.setHandler(async path=>path==='auction/login'?{
    ...defaultState(),steam:{authenticated:false,login_pending:true,account_name:'',wallet_currency:null}}:defaultState());
  await p.click('auction-login');
  assert.deepEqual(p.calls.find(call=>call.path==='auction/login'),{path:'auction/login',data:{}});
  assert.equal(p.node('auction-login').disabled,true);assert.match(p.node('auction-steam-status').textContent,/等待 Steam 登录完成/);
  const html=fs.readFileSync('deskrawl_assistant/web/index.html','utf8').match(/<details[^>]*data-managed-fold="auction"[\s\S]*?<\/details>/)[0];
  assert.doesNotMatch(html,/<input[^>]*(?:password|cookie|sessionid|token)/i);
  assert.equal(p.calls.some(call=>call.path.includes('cookie')||call.path.includes('session')),false);
});

test('ordinary browser state refuses even programmatically dispatched login and explains the desktop route',async()=>{
  const p=page();p.setState({...defaultState(),login_available:false,login_reason:'请在助手独立窗口中绑定 Steam。'});
  await p.open();await p.click('auction-login');
  assert.equal(p.node('auction-login').disabled,true);assert.match(p.node('auction-login-note').textContent,/助手独立窗口/);
  assert.deepEqual(p.calls,[{path:'auction/state'}]);
});

test('a cached login response cannot leave a manually opened Steam binding invisible when automation is off',async()=>{
  const p=page();await p.open();
  p.setHandler(async path=>path==='auction/login'?defaultState():{...defaultState(),
    steam:{authenticated:true,login_pending:false,account_name:'76561198000000001',wallet_currency:'CNY'}});
  await p.click('auction-login');
  assert.equal(p.node('auction-login').disabled,true);assert.match(p.node('auction-steam-status').textContent,/正在打开/);
  await p.poll();assert.match(p.node('auction-steam-status').textContent,/已登录/);assert.equal(p.node('auction-wallet-currency').textContent,'CNY');
  const count=p.calls.length;await p.poll();assert.equal(p.calls.length,count+1);
  assert.equal(p.draft.auction.upload_enabled,false);assert.equal(p.draft.auction.sell_enabled,false);
});

test('auction errors stay in their panel and never replace unrelated configurations or disable auto equip',async()=>{
  const p=page({auto_equip:{enabled:true},background:{rule_monitoring:false,inventory_automation:false}}),before=clone(p.draft);
  p.setHandler(async()=>{throw new Error('拍卖行暂不可用');});await p.open();
  assert.equal(p.node('auction-api-error').hidden,false);assert.equal(p.node('auction-api-error').textContent,'拍卖行暂不可用');
  assert.deepEqual(p.draft,before);assert.equal(p.changes.length,0);
  assert.equal(managed.controlState({capabilities:{transfer:true,equip:true}},p.draft,true).canStart,true);
  p.setHandler(async()=>defaultState());await p.click('auction-refresh');assert.equal(p.node('auction-api-error').hidden,true);
});

test('saved credentials continue local status polling until background verification finishes',async()=>{
  const p=page();
  p.setState({...defaultState(),steam:{bound:true,authenticated:false,login_pending:false,
    message:'Steam 请求被限制（HTTP 429），请稍后重试。'}});
  await p.open();
  assert.match(p.node('auction-steam-status').textContent,/凭证已保存/);
  assert.match(p.node('auction-login-note').textContent,/HTTP 429/);
  const count=p.calls.length;await p.poll();assert.equal(p.calls.length,count+1);
  p.setState({...defaultState(),steam:{bound:true,authenticated:true,login_pending:false,wallet_currency:'CNY'}});
  await p.poll();assert.match(p.node('auction-steam-status').textContent,/已登录/);
  const verifiedCount=p.calls.length;await p.poll();assert.equal(p.calls.length,verifiedCount+1);
  assert.equal(p.draft.auction.upload_enabled,false);assert.equal(p.draft.auction.sell_enabled,false);
});

test('saved enabled settings wait for login verification without erasing preferences',async()=>{
  const p=page({auction:{upload_enabled:true,sell_enabled:true,categories:['divine_equipment']}});await flush();
  assert.deepEqual(p.calls,[{path:'auction/state'}]);await p.poll();assert.equal(p.calls.length,1);
  await p.open();await p.poll();assert.equal(p.calls.length,3);
  assert.equal(p.node('auction-sell-enabled').checked,true);
  assert.match(p.node('auction-sell-login-note').textContent,/等待 Steam 登录核验/);
  await p.change('auction-sell-enabled',false);await p.poll();assert.equal(p.calls.length,4);
  assert.equal(p.draft.auction.upload_enabled,true);assert.equal(p.draft.auction.sell_enabled,false);
});

test('native upload and mobile sale confirmation remain separate with the actual Steam wallet currency',async()=>{
  const p=page();p.setState({...defaultState(),steam:{authenticated:true,account_name:'76561198000000001',wallet_currency:'HKD'},
    upload:{phase:'idle',message:'上传已核验',listing_count:2,listing_limit:8},
    sell:{phase:'pending',message:'等待手机确认',pending:1,pending_confirmation:true,confirmation_count:1},display_currency:'CNY'});
  await p.open();assert.equal(p.node('auction-wallet-currency').textContent,'HKD');
  assert.equal(p.node('auction-listing-count').textContent,'游戏上传 2 / 8 件');
  assert.equal(p.node('auction-upload-status').textContent,'上传已核验');assert.equal(p.node('auction-sell-status').textContent,'等待手机确认');
  assert.equal(p.node('auction-confirmation').hidden,false);assert.match(p.node('auction-confirmation').textContent,/Steam 手机应用/);
  p.setConfirm(false);await p.click('auction-logout');assert.equal(p.calls.some(call=>call.path==='auction/logout'),false);
});

test('running configuration cannot be edited through the module event handler',async()=>{
  const p=page({auction:{categories:['gold_bar'],excluded_keys:['DivineBow1'],retain_count:2}});p.window.AuctionUI.render(p.draft.auction,catalog,true);
  assert.equal(p.node('auction-excluded-search').disabled,true);
  assert.equal(p.node('auction-excluded-options').choices.every(input=>input.disabled),true);
  await p.change('auction-upload-enabled',true);await p.chooseCategory('gold_bar',false);await p.chooseExcluded('DivineBow1',false);
  assert.equal(p.draft.auction.upload_enabled,false);assert.deepEqual(p.draft.auction.categories,['gold_bar']);assert.equal(p.changes.length,0);
  assert.deepEqual(p.draft.auction.excluded_keys,['DivineBow1']);
});


test('category toggles save independently from exact retained items and remove hidden legacy permissions',async()=>{
  const p=page({auction:{upload_enabled:true,sell_enabled:true,item_keys:['GoldBar'],excluded_keys:['HunterSetRune2'],retain_count:1}});
  const option=p.node('auction-category-options').choices.find(row=>row.dataset.auctionCategory==='grand_rune');
  assert.ok(option);option.checked=true;
  await p.node('managed-auction').emit('change',{target:option});await flush();
  const saved=await p.save();
  assert.deepEqual(saved.auction.categories,['grand_rune']);
  assert.deepEqual(saved.auction.item_keys,[]);assert.equal(saved.auction.upload_enabled,false);assert.equal(saved.auction.sell_enabled,false);
  assert.deepEqual(saved.auction.excluded_keys,['HunterSetRune2']);
  const fresh=page();assert.equal(fresh.elements.has('auction-legacy-choices'),false);assert.equal(fresh.elements.has('auction-item-options'),false);
  assert.equal(fresh.node('auction-category-options').choices.length,7);
  assert.equal(fresh.node('auction-category-options').choices.every(row=>!row.checked),true);
});

test('auto sale can only be enabled with verified Steam login, never saved credentials or pending login',async()=>{
  const p=page({auction:{upload_enabled:true,categories:['grand_rune']}});
  for(const steam of [{},{bound:true},{login_pending:true},{authenticated:'true'}]){
    p.setState({...defaultState(),steam});await p.open();
    assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
    await p.change('auction-sell-enabled',true);
    assert.equal(p.node('auction-sell-enabled').checked,false);
    assert.equal(p.draft.auction.sell_enabled,false);
  }
  p.setState({...defaultState(),steam:{authenticated:true,bound:true}});await p.poll();
  assert.equal(p.node('auction-sell-enabled').disabled,false);
  await p.change('auction-sell-enabled',true);assert.equal(p.draft.auction.sell_enabled,true);
  p.setState(defaultState());await p.poll();
  assert.equal(p.draft.auction.sell_enabled,true,'saved preference survives temporary re-verification');
  assert.match(p.node('auction-sell-login-note').textContent,/暂停出售/);
  await p.change('auction-sell-enabled',false);assert.equal(p.draft.auction.sell_enabled,false);
  assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
});

test('failed status refresh revokes ability to newly enable sales',async()=>{
  const p=page({auction:{upload_enabled:true,categories:['grand_rune']}});
  await flush();
  p.setState({...defaultState(),steam:{authenticated:true}});await p.open();
  assert.equal(p.node('auction-sell-enabled').disabled,false);
  p.setHandler(async()=>{throw Error('offline');});await p.poll();
  assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
  await p.change('auction-sell-enabled',true);assert.equal(p.draft.auction.sell_enabled,false);
});

test('sale requires upload even with valid login and cancelling upload clears sale persistently',async()=>{
  const p=page({auction:{categories:['grand_rune'],excluded_keys:['HunterSetRune2'],price_rule:'highest_buy'}});
  p.setState({...defaultState(),steam:{authenticated:true}});await p.open();
  assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
  assert.match(p.node('auction-sell-login-note').textContent,/请先启用游戏拍卖行上传/);
  await p.change('auction-sell-enabled',true);
  assert.equal(p.node('auction-sell-enabled').checked,false);
  assert.equal(p.draft.auction.sell_enabled,false);
  await p.change('auction-upload-enabled',true);
  assert.equal(p.node('auction-sell-enabled').disabled,false);
  await p.change('auction-sell-enabled',true);
  assert.equal(p.draft.auction.sell_enabled,true);
  await p.change('auction-upload-enabled',false);
  assert.equal(p.node('auction-sell-enabled').checked,false);
  assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
  assert.equal(p.node('auction-price-rule').disabled,true);
  const saved=await p.save();
  assert.equal(saved.auction.upload_enabled,false);assert.equal(saved.auction.sell_enabled,false);
  assert.deepEqual(saved.auction.categories,['grand_rune']);
  assert.deepEqual(saved.auction.excluded_keys,['HunterSetRune2']);
  assert.equal(saved.auction.price_rule,'highest_buy');
  const reopened=page(saved);
  assert.equal(reopened.node('auction-sell-enabled').checked,false);
  await p.change('auction-upload-enabled',true);
  assert.equal(p.node('auction-sell-enabled').checked,false,'re-enabling upload does not authorize sale again');
  assert.equal(p.calls.some(call=>!['auction/state','managed/settings'].includes(call.path)),false);
});

test('cancelling upload clears saved sale preference even while Steam login is unavailable',async()=>{
  const p=page({auction:{upload_enabled:true,sell_enabled:true,categories:['gold_bar']}});await flush();
  await p.change('auction-upload-enabled',false);
  assert.equal(p.node('auction-sell-enabled').checked,false);
  assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
  const saved=await p.save();assert.equal(saved.auction.sell_enabled,false);
});

test('clicking unavailable sale explains upload and Steam binding without changing or saving settings',async()=>{
  const p=page({auction:{categories:['grand_rune']}});await p.open();
  const before=clone(p.draft),count=p.changes.length;
  assert.equal(p.node('auction-sell-enabled').disabled,false,'click must be delivered to explain prerequisites');
  assert.equal(p.node('auction-sell-enabled')['aria-disabled'],'true');
  const attempt=await p.click('auction-sell-enabled');
  assert.equal(attempt.defaultPrevented,true);assert.equal(p.node('auction-sell-enabled').checked,false);
  assert.match(p.toasts.at(-1),/请先启用“游戏拍卖行上传”/);
  assert.match(p.toasts.at(-1),/托管模式 → 拍卖行与 Steam 市场 → Steam 登录/);
  assert.deepEqual(p.draft,before);assert.equal(p.changes.length,count);
  await p.change('auction-upload-enabled',true);
  const notLogged=await p.click('auction-sell-enabled');
  assert.equal(notLogged.defaultPrevented,true);assert.equal(p.draft.auction.sell_enabled,false);
  assert.match(p.toasts.at(-1),/Steam 登录.*绑定 Steam 账号/);
  p.setState({...defaultState(),steam:{authenticated:true}});await p.poll();
  const notices=p.toasts.length;
  const allowed=await p.click('auction-sell-enabled');
  assert.equal(allowed.defaultPrevented,false);assert.equal(p.draft.auction.sell_enabled,true);
  assert.equal(p.toasts.length,notices);
  assert.equal(p.calls.some(call=>!['auction/state'].includes(call.path)),false);
});

test('saved sale may still be unchecked during login verification while running settings stay locked',async()=>{
  const p=page({auction:{upload_enabled:true,sell_enabled:true,categories:['gold_bar']}});await flush();
  const off=await p.click('auction-sell-enabled');
  assert.equal(off.defaultPrevented,false);assert.equal(p.draft.auction.sell_enabled,false);
  assert.equal(p.toasts.length,0);
  p.window.AuctionUI.render(p.draft.auction,catalog,true);
  assert.equal(p.node('auction-sell-enabled').disabled,true);
  await p.click('auction-sell-enabled');assert.equal(p.draft.auction.sell_enabled,false);
});
