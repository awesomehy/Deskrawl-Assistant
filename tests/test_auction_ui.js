const {test}=require('node:test');
const assert=require('node:assert/strict');
const auction=require('../deskrawl_assistant/web/auction-ui.js');
const managed=require('../deskrawl_assistant/web/managed-mode.js');

test('auction is opt-in and saving its settings preserves every unrelated managed group',()=>{
  const defaults=managed.normalizeSettings();
  assert.deepEqual(defaults.auction,{upload_enabled:false,sell_enabled:false,price_rule:'lowest_sell',categories:[],item_keys:[],excluded_keys:[]});
  assert.equal(managed.defaultFolds(defaults).auction,false);
  const existing={auto_equip:{enabled:true,slot_locks:{6:'LegendaryRing1'}},cleanup:{rune_pages:[2,4],gem_pages:[3]},
    synthesis:{runes:{enabled:true,max_quality:'set',excluded_keys:['RareRune_A']}},
    auction:{upload_enabled:true,sell_enabled:true,price_rule:'highest_buy',allow_locked:true,categories:['gold_bar','black_mist_equipment'],item_keys:['GoldBar','LegendaryRing1_BlackMist','GoldBar'],excluded_keys:['GoldBar','GoldBar'],retain_count:7}};
  const before=JSON.stringify(existing),settings=managed.normalizeSettings(existing);
  assert.equal(JSON.stringify(existing),before);
  assert.deepEqual(managed.normalizeSettings(JSON.parse(JSON.stringify(settings))),settings);
  assert.deepEqual(settings.auto_equip.slot_locks,existing.auto_equip.slot_locks);
  assert.deepEqual(settings.cleanup.rune_pages,[2,4]);assert.deepEqual(settings.cleanup.gem_pages,[3]);
  assert.deepEqual(settings.synthesis.runes.excluded_keys,['RareRune_A']);
  assert.deepEqual(settings.auction.item_keys,[]);
  assert.deepEqual(settings.auction.categories,['black_mist_equipment','gold_bar']);
  assert.deepEqual(settings.auction.excluded_keys,['GoldBar']);assert.match(managed.foldSummaries(settings).auction.summary,/保留白名单 1 种优先/);
  const summary=managed.foldSummaries(settings).auction;
  assert.equal(summary.enabled,true);assert.match(summary.summary,/上传开 · 出售开/);assert.doesNotMatch(summary.summary,/每种保留/);
  assert.equal(Object.hasOwn(settings.auction,'retain_count'),false);assert.equal(Object.hasOwn(settings.auction,'allow_locked'),false);
  defaults.auction.item_keys.push('GoldBar');assert.deepEqual(managed.normalizeSettings().auction.item_keys,[]);
  defaults.auction.excluded_keys.push('GoldBar');assert.deepEqual(managed.normalizeSettings().auction.excluded_keys,[]);
});

test('legacy individual permissions are removed without broadening categories or losing retained items',()=>{
  const old=auction.normalize({upload_enabled:true,sell_enabled:true,item_keys:['GoldBar'],excluded_keys:['HunterSetRune2'],retain_count:2});
  assert.deepEqual(old.excluded_keys,['HunterSetRune2']);assert.equal(auction.policyAllows(old,'GoldBar'),false);
  assert.equal(old.upload_enabled,false);assert.equal(old.sell_enabled,false);assert.deepEqual(old.categories,[]);assert.equal(Object.hasOwn(old,'retain_count'),false);
  const both=auction.normalize({...old,sell_enabled:true,categories:['gold_bar'],excluded_keys:['GoldBar',' HunterSetRune2 ','GoldBar',7]});
  assert.deepEqual(both.excluded_keys,['GoldBar','HunterSetRune2']);assert.equal(auction.policyAllows(both,'GoldBar'),false);
  assert.equal(auction.policyAllows(both,'DivineBow1'),false);assert.deepEqual(both.item_keys,[]);
});

test('whitelist selection preserves items hidden by searches and stale saved items remain removable',()=>{
  const catalog={auction_items:[
    {key:'GoldBar',name:'金条',kind:'material',rarity:3,icon:'/icons/GoldBar.png'},
    {key:'DivineBow1',name:'神圣之弓',kind:'equipment',rarity:4,icon:'/icons/DivineBow1.png'},
    {key:'LegendaryBoots1_BlackMist',name:'黑雾靴子',kind:'equipment',icon:'/icons/LegendaryBoots1.png'},
    {key:'HunterSetRune2',name:'二连矢',kind:'rune',rarity:5,icon:'/icons/HunterSetRune2.png'}]};
  const selected=['GoldBar','HunterSetRune2','old-item'];
  const rows=auction.items(catalog,'神圣',selected);
  assert.equal(rows.length,1);assert.equal(rows[0].key,'DivineBow1');
  assert.deepEqual(auction.updateWhitelist(selected,[{key:rows[0].key,selected:true}]),['DivineBow1','GoldBar','HunterSetRune2','old-item']);
  assert.equal(auction.items(catalog,'',selected).find(row=>row.key==='old-item').unknown,true);
  assert.deepEqual(auction.updateWhitelist(selected,[{key:'old-item',selected:false}]),['GoldBar','HunterSetRune2']);
  assert.equal(auction.items(catalog,'黑雾')[0].quality,'black_mist');
  assert.equal(auction.items(catalog,'二连')[0].quality,'set');
  assert.equal(auction.items(catalog,'金条')[0].icon,'/icons/GoldBar.png');
  assert.equal(auction.items({auction_items:[{key:'unsafe',icon:'https://other.example/image.png'}]})[0].icon,'');
});

test('empty whitelist cannot authorize money operations and upload requires its own native capability',()=>{
  const settings=managed.normalizeSettings({background:{rule_monitoring:false,inventory_automation:false}});
  settings.auction.upload_enabled=true;
  assert.match(managed.validateSettings(settings),/至少勾选一种/);
  settings.auction.categories=['gold_bar'];
  assert.equal(managed.validateSettings(settings),'');
  assert.ok(managed.requiredCapabilities(settings).includes('auction_upload'));
  const capabilities=Object.fromEntries(managed.requiredCapabilities(settings).map(key=>[key,true]));
  assert.equal(managed.controlState({capabilities},settings,true).canStart,true);
  assert.deepEqual(managed.controlState({capabilities:{...capabilities,auction_upload:false}},settings,true).missing,['auction_upload']);
  settings.auction.upload_enabled=false;settings.auction.sell_enabled=true;
  assert.equal(managed.requiredCapabilities(settings).includes('auction_upload'),false);
  assert.equal(managed.controlState({},settings,true).canStart,false);
  const migrated=managed.normalizeSettings(settings);
  assert.equal(migrated.auction.upload_enabled,false);assert.equal(migrated.auction.sell_enabled,false);
  assert.deepEqual(migrated.auction.categories,['gold_bar']);
  assert.equal(Object.hasOwn(auction.normalize({allow_locked:true,retain_count:7}),'allow_locked'),false);
});

test('upload and real-sale state remain separate and money uses the exact wallet currency',()=>{
  const view=auction.stateView({login_available:true,steam:{authenticated:true,account_name:'玩家',wallet_currency:'HKD'},
    upload:{message:'上架结果已确认',listing_count:3,listing_limit:8},
    sell:{message:'等待手机确认',pending_confirmation:true,confirmation_count:2},display_currency:'CNY'});
  assert.equal(view.uploadText,'上架结果已确认');assert.equal(view.sellText,'等待手机确认');
  assert.equal(view.currency,'HKD');assert.equal(view.pendingConfirmation,true);assert.equal(view.confirmationCount,2);
  assert.equal(view.listingCount,3);assert.equal(view.listingLimit,8);
  const browser=auction.stateView({state:{login_available:false,login_reason:'请使用桌面助手登录',steam:{}}});
  assert.equal(browser.loginAvailable,false);assert.equal(browser.loginReason,'请使用桌面助手登录');
  assert.equal(browser.authenticated,false);assert.match(browser.currency,/等待读取/);
  assert.equal(auction.stateView({steam:{authenticated:'true'}}).authenticated,false);
  for(const listing_count of [null,undefined,-1,'0',1.5])assert.equal(auction.stateView({upload:{listing_count}}).listingCount,null);
  assert.equal(auction.stateView({upload:{listing_count:0}}).listingCount,0);
});

test('UID-less material limitations are visible without deleting a saved material whitelist',()=>{
  const catalog={auction_items:[{key:'GoldBar',name:'金条',kind:'material',rarity:3,available:false,
    support_reason:'无实例 ID 的材料暂需手动上传',icon:'/icons/GoldBar.png'}]};
  const rows=auction.items(catalog,'金条',['GoldBar']);
  assert.equal(rows.length,1);assert.equal(rows[0].support_reason,'无实例 ID 的材料暂需手动上传');
  assert.deepEqual(auction.updateWhitelist(['GoldBar'],[]),['GoldBar']);
  assert.deepEqual(auction.normalize({excluded_keys:['GoldBar']}).excluded_keys,['GoldBar']);
});

test('saved credentials awaiting verification show the backend reason without claiming authentication',()=>{
  const view=auction.stateView({login_available:true,steam:{bound:true,authenticated:false,login_pending:false,
    message:'Steam 请求被限制（HTTP 429），请稍后重试。'}});
  assert.equal(view.authenticated,false);
  assert.equal(view.loginPending,false);
  assert.equal(view.loginText,'登录凭证已保存，等待核验');
  assert.equal(view.loginMessage,'Steam 请求被限制（HTTP 429），请稍后重试。');
  assert.equal(view.currency,'等待读取 Steam 钱包币种');
});

test('a late pre-logout poll cannot resurrect a Steam login or overwrite pending action state',async()=>{
  let resolvePoll,resolveLogout;const requests=[],states=[];
  const controller=auction.createStateController({api:(path,body)=>{
    requests.push([path,body]);return new Promise(resolve=>{if(path==='auction/state')resolvePoll=resolve;else resolveLogout=resolve;});
  },onState:state=>states.push(state)});
  const polling=controller.refresh(),logout=controller.action('logout');
  assert.equal(controller.busy,true);await controller.refresh();
  resolveLogout({state:{steam:{authenticated:false}}});await logout;
  resolvePoll({steam:{authenticated:true}});await polling;
  assert.deepEqual(requests,[['auction/state',undefined],['auction/logout',{}]]);
  assert.deepEqual(states,[{steam:{authenticated:false}}]);assert.equal(controller.busy,false);
});

test('failed login does not remain busy and another attempt uses no client-supplied credentials',async()=>{
  const errors=[],requests=[];let first=true;
  const controller=auction.createStateController({api:async(path,body)=>{
    requests.push([path,body]);if(first){first=false;throw new Error('桌面登录不可用');}return {steam:{login_pending:true}};
  },onError:error=>errors.push(error.message)});
  await controller.action('login');assert.equal(controller.busy,false);assert.deepEqual(errors,['桌面登录不可用']);
  await controller.action('login');assert.deepEqual(requests,[['auction/login',{}],['auction/login',{}]]);
});


test('category selection covers seven families and specific retention always wins',()=>{
  const config=auction.normalize({categories:Object.keys(auction.categoryNames),excluded_keys:['HunterSetRune1_1']});
  assert.equal(config.categories.length,7);
  for(const [key,row] of [['DivineBow1',{}],['LegendaryBoots1',{quality:'black_mist'}],['HunterSetRune2',{}],['LegendaryRune_HunterSpecial1',{}],['Rein_VolcanoDragon',{rarity:3}],['UpgradeCommodity1',{}],['GoldBar',{}]])assert.equal(auction.policyAllows(config,key,row),true,key);
  assert.equal(auction.policyAllows(config,'HunterSetRune1_1'),false);
  assert.equal(auction.policyAllows(config,'MovementPotion1'),false);
  assert.equal(auction.policyAllows(config,'LegendaryBoots1'),false);
  assert.deepEqual(auction.normalize({item_keys:['GoldBar']}).categories,[]);
});
