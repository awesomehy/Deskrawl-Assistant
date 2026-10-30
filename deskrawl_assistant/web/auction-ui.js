'use strict';
(function(root){
  const priceRules={lowest_sell:'最低在售价',highest_buy:'最高求购价'};
  const categoryNames={minion_equipment:'仆从装备（可交易）',divine_equipment:'神圣装备',black_mist_equipment:'黑雾装备',set_rune:'符文套装',grand_rune:'宏伟符文',legendary_rein:'传说缰绳',consecration_stone:'开光石',gold_bar:'金条'};
  const kindNames={equipment:'装备',rune:'符文',material:'材料',commodity:'材料',rein:'缰绳',consumable:'缰绳'};
  const qualityNames={divine:'神圣',black_mist:'黑雾传说',legendary:'传说',grand:'宏伟',set:'符文套装'};
  const cleanKeys=values=>[...new Set((Array.isArray(values)?values:[]).filter(key=>typeof key==='string'&&key.trim()).map(key=>key.trim()))].sort();
  function normalize(value={}){
    value=value&&typeof value==='object'?value:{};
    const categories=cleanKeys(value.categories).filter(key=>Object.hasOwn(categoryNames,key));
    const legacyOnly=cleanKeys(value.item_keys).length>0&&!categories.length;
    const uploadEnabled=!legacyOnly&&value.upload_enabled===true;
    return {upload_enabled:uploadEnabled,sell_enabled:uploadEnabled&&value.sell_enabled===true,
      price_rule:Object.hasOwn(priceRules,value.price_rule)?value.price_rule:'lowest_sell',
      categories,item_keys:[],excluded_keys:cleanKeys(value.excluded_keys)};
  }
  function enabled(value){const config=normalize(value);return config.upload_enabled||config.sell_enabled;}
  function category(row){
    const key=String(row.key??row.internal_name??row.name_key??'');
    if(row.is_minion_equipment===true||row.equip_slot===12||row.slot==='Minion'||/^(Normal|Uncommon|Rare|Legendary|Divine)MinionGear\d+$/.test(key))return 'minion_equipment';
    if(row.is_black_mist===true||row.variant==='black_mist'||row.quality==='black_mist')return 'black_mist_equipment';
    if(key.startsWith('Divine'))return 'divine_equipment';
    if(key.includes('SetRune'))return 'set_rune';
    if(key.startsWith('LegendaryRune_'))return 'grand_rune';
    if(key.startsWith('Rein_')&&(row.rarity??row.base_rarity)===3)return 'legendary_rein';
    return {GoldBar:'gold_bar',UpgradeCommodity1:'consecration_stone'}[key];
  }
  function policyAllows(value,key,row={}){const config=normalize(value);return !config.excluded_keys.includes(key)&&config.categories.includes(category({...row,key}));}
  function quality(row){
    if(Object.hasOwn(qualityNames,row.quality))return row.quality;
    if(row.is_black_mist===true||/black.?mist/i.test(row.key))return 'black_mist';
    if(row.kind==='rune')return row.rarity===5||/SetRune\d/.test(row.key)?'set':'grand';
    if(row.rarity===4||/^Divine/.test(row.key))return 'divine';
    return 'legendary';
  }
  function items(catalog={},query='',selected=[]){
    const source=catalog.auction_items||[],rows=Array.isArray(source)?source:Object.entries(source).map(([key,row])=>({key,...row})),all=new Map();
    for(const row of rows){
      if(!row||typeof row.key!=='string'||!row.key.trim())continue;
      const key=row.key.trim(),q=quality({...row,key});
      all.set(key,{...row,key,name:row.name||key,quality:q,quality_name:row.quality_name||qualityNames[q],kind_name:category({...row,key})==='minion_equipment'?'仆从装备':key.startsWith('Rein_')?'缰绳':kindNames[row.kind]||'道具',
        icon:typeof row.icon==='string'&&row.icon.startsWith('/icons/')?row.icon:''});
    }
    for(const key of cleanKeys(selected))if(!all.has(key))all.set(key,{key,name:key,quality:'legendary',quality_name:'暂未收录',kind_name:'待核验',icon:'',unknown:true});
    const search=String(query).trim().toLowerCase();
    return [...all.values()].filter(row=>!search||[row.name,row.key,row.quality_name,row.kind_name].some(text=>String(text||'').toLowerCase().includes(search)))
      .sort((a,b)=>Object.keys(qualityNames).indexOf(a.quality)-Object.keys(qualityNames).indexOf(b.quality)||a.name.localeCompare(b.name,'zh-CN')||a.key.localeCompare(b.key));
  }
  function updateWhitelist(existing,choices){
    const selected=new Set(cleanKeys(existing));
    for(const {key,selected:checked} of choices)if(typeof key==='string'&&key.trim()){if(checked)selected.add(key.trim());else selected.delete(key.trim());}
    return [...selected].sort();
  }
  function validation(value){
    const config=normalize(value);
    if(enabled(config)&&!config.categories.length)return '拍卖行与 Steam 市场：请至少勾选一种允许处理的商品类别。';
    return '';
  }
  function stateView(value={}){
    const state=value?.state||value,steam=state.steam||{},upload=state.upload||{},sell=state.sell||{};
    const count=v=>Number.isInteger(v)&&v>=0?v:0;
    const authenticated=steam.authenticated===true;
    return {authenticated,bound:steam.bound===true,loginAvailable:state.login_available===true,loginPending:steam.login_pending===true,
      loginReason:state.login_reason||'请在桌面助手中登录 Steam；普通浏览器不能打开桌面登录窗口。',
      loginText:authenticated?`已登录${steam.account_name?' · '+steam.account_name:''}`:steam.login_pending?'等待 Steam 登录完成':steam.bound?'登录凭证已保存，等待核验':'尚未登录 Steam',
      loginMessage:typeof steam.message==='string'?steam.message:'',
      currency:authenticated&&typeof steam.wallet_currency==='string'&&steam.wallet_currency?steam.wallet_currency:'等待读取 Steam 钱包币种',
      uploadText:upload.message||(upload.pending?'上传结果待确认':upload.phase==='disabled'?'上传未启用':'等待上传状态'),
      sellText:(sell.message||(sell.pending?'出售结果待确认':sell.phase==='disabled'?'出售未启用':'等待出售状态'))+(count(sell.uncertain_count)?` · ${count(sell.uncertain_count)} 件结果待核验，不重复提交`:''),
      listingCount:Number.isInteger(upload.listing_count)&&upload.listing_count>=0?upload.listing_count:null,listingLimit:count(upload.listing_limit)||8,
      pendingConfirmation:sell.pending_confirmation===true||count(sell.confirmation_count)>0,
      confirmationCount:count(sell.confirmation_count),error:typeof state.error==='string'?state.error:''};
  }
  function createStateController({api,onState=()=>{},onError=()=>{}}){
    let epoch=0,polling=false,acting=false;
    const deliver=(value,ticket)=>{if(ticket===epoch)onState(value?.state||value);};
    return {get busy(){return acting;},async refresh(){
      if(polling||acting)return;polling=true;const ticket=epoch;
      try{deliver(await api('auction/state'),ticket);}catch(error){if(ticket===epoch)onError(error);}finally{polling=false;}
    },async action(name){
      if(acting)return;acting=true;const ticket=++epoch;
      try{deliver(await api('auction/'+name,{}),ticket);}catch(error){if(ticket===epoch)onError(error);}finally{acting=false;}
    }};
  }
  const helpers={normalize,enabled,policyAllows,category,categoryNames,quality,items,updateWhitelist,validation,stateView,createStateController,priceRules};
  if(typeof module!=='undefined'&&module.exports)module.exports=helpers;
  if(!root.document)return;
  const $=id=>root.document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let options=null,current=normalize(),catalog={},disabled=false,state=null,signatures={},controller=null,initialized=false,loginWatchUntil=0;
  function renderState(){
    const view=stateView(state||{}),waiting=controller?.busy===true,openingLogin=Date.now()<loginWatchUntil&&!view.authenticated;
    $('auction-steam-status').textContent=openingLogin&&!view.loginPending?'正在打开 Steam 登录窗口':view.loginText;
    $('auction-wallet-currency').textContent=view.currency;
    $('auction-login-note').textContent=view.loginAvailable?(view.loginMessage||'登录后凭证加密保存在本机，窗口自动关闭；无需填写 Cookie 或令牌。'):view.loginReason;
    $('auction-login').hidden=view.authenticated;$('auction-login').disabled=waiting||!view.loginAvailable||view.loginPending||openingLogin;
    $('auction-logout').hidden=!view.authenticated&&!view.loginPending&&!view.bound;$('auction-logout').disabled=waiting;
    $('auction-refresh').disabled=waiting;
    // Upload must stay enabled. Preserve an authorized sale preference during
    // credential verification; a new authorization requires a live session.
    // Native disabled controls do not receive clicks, so use aria-disabled for
    // unmet prerequisites and intercept activation to explain what is missing.
    $('auction-sell-enabled').disabled=disabled||waiting;
    $('auction-sell-enabled').setAttribute('aria-disabled',String(disabled||waiting||!current.upload_enabled||!view.authenticated&&!current.sell_enabled));
    const sellNote=!current.upload_enabled?'请先启用游戏拍卖行上传，再启用 Steam 市场出售；取消上传会同时取消出售。':
      view.authenticated?'Steam 登录有效，可以启用自动出售。':current.sell_enabled?'已保存自动出售设置；等待 Steam 登录核验，期间暂停出售。':'请先在下方“Steam 登录”绑定账号，登录有效后才能启用自动出售。';
    $('auction-sell-enabled').title=sellNote;
    $('auction-sell-login-note').textContent=sellNote;
    $('auction-upload-status').textContent=view.uploadText;$('auction-sell-status').textContent=view.sellText;
    $('auction-listing-count').textContent=view.listingCount===null?'游戏上传名额待读取 · 当前数量未知':`游戏上传 ${view.listingCount} / ${view.listingLimit} 件`;
    $('auction-confirmation').hidden=!view.pendingConfirmation;
    $('auction-confirmation').textContent=`待 Steam 手机确认${view.confirmationCount?' · '+view.confirmationCount+' 件':''}，请在 Steam 手机应用中检查出售详情。确认前不重复提交。`;
    $('auction-api-error').hidden=!view.error;$('auction-api-error').textContent=view.error;
  }
  function explainSalePrerequisite(){
    const logged=stateView(state||{}).authenticated;
    const message=!current.upload_enabled?
      '请先启用“游戏拍卖行上传”。'+(logged?'开启上传后，才能勾选 Steam 市场出售。':'还需前往“托管模式 → 拍卖行与 Steam 市场 → Steam 登录”绑定账号，登录有效后再勾选。'):
      '请先前往“托管模式 → 拍卖行与 Steam 市场 → Steam 登录”绑定 Steam 账号，登录有效后再勾选。';
    options.toast?.(message);
  }
  function renderPicker({field,searchId,optionsId,countId,attribute,dataset,label}){
    const rows=items(catalog,$(searchId).value,current[field]),nextSignature=JSON.stringify([rows,current[field],current.excluded_keys,disabled]);
    $(countId).textContent=`已${label} ${current[field].length} 种 · 显示 ${rows.length} 种`;
    if(signatures[field]===nextSignature)return;signatures[field]=nextSignature;
    const selected=new Set(current[field]),focused=root.document.activeElement?.dataset?.[dataset];
    $(optionsId).innerHTML=rows.map(row=>`<label class="auction-item-choice ${selected.has(row.key)?'selected':''} auction-retained-choice" data-quality="${row.quality}" title="${esc(row.key)}"><input type="checkbox" ${attribute}="${esc(row.key)}" ${selected.has(row.key)?'checked':''} ${disabled?'disabled':''}><span class="auction-item-icon">${row.icon?`<img src="${esc(row.icon)}" alt="" loading="lazy">`:'◇'}</span><span class="auction-item-label"><strong>${esc(row.name)}</strong><small>${esc(row.quality_name)} · ${esc(row.kind_name)}</small>${row.support_reason?`<small class="auction-item-support-note">${esc(row.support_reason)}</small>`:''}</span></label>`).join('')||'<p class="auction-empty">'+esc(Object.keys(catalog.auction_items||{}).length?'没有符合搜索条件的物品':'等待读取可上传物品目录')+'</p>';
    if(focused)[...$(optionsId).querySelectorAll(`[${attribute}]`)].find(input=>input.dataset[dataset]===focused)?.focus();
  }
  function renderChoices(){
    const signature=JSON.stringify([current.categories,disabled]);
    if(signatures.categories!==signature){
      signatures.categories=signature;
      $('auction-category-options').innerHTML=Object.entries(categoryNames).map(([key,name])=>`<label class="auction-category-choice ${current.categories.includes(key)?'selected':''}"><input type="checkbox" data-auction-category="${key}" ${current.categories.includes(key)?'checked':''} ${disabled?'disabled':''}><span>${name}</span></label>`).join('');
    }
    $('auction-category-count').textContent=`已允许 ${current.categories.length} 类`;
    renderPicker({field:'excluded_keys',searchId:'auction-excluded-search',optionsId:'auction-excluded-options',countId:'auction-excluded-count',attribute:'data-auction-excluded-key',dataset:'auctionExcludedKey',label:'保留'});
  }
  function read(value=current){
    const next=normalize(value);if(!initialized)return next;
    next.upload_enabled=$('auction-upload-enabled').checked;
    next.sell_enabled=next.upload_enabled&&$('auction-sell-enabled').checked&&(current.sell_enabled||!controller.busy&&stateView(state||{}).authenticated);
    next.price_rule=$('auction-price-rule').value;
    next.categories=updateWhitelist(next.categories,[...$('auction-category-options').querySelectorAll('[data-auction-category]')].map(input=>({key:input.dataset.auctionCategory,selected:input.checked})));
    next.excluded_keys=updateWhitelist(next.excluded_keys,[...$('auction-excluded-options').querySelectorAll('[data-auction-excluded-key]')].map(input=>({key:input.dataset.auctionExcludedKey,selected:input.checked})));
    return next;
  }
  function render(value,itemCatalog,locked=false){
    if(!initialized)return;const wasEnabled=enabled(current);current=normalize(value);catalog=itemCatalog||{};disabled=!!locked;
    $('auction-upload-enabled').checked=current.upload_enabled;$('auction-sell-enabled').checked=current.sell_enabled;
    $('auction-price-rule').value=current.price_rule;
    for(const id of ['auction-upload-enabled','auction-excluded-search'])$(id).disabled=disabled;
    $('auction-price-rule').disabled=disabled||!current.sell_enabled;
    renderChoices();renderState();
    if(enabled(current)&&!wasEnabled)controller.refresh();
  }
  function init(config){
    options=config;if(initialized)return;initialized=true;
    controller=createStateController({api:options.api,onState:value=>{state=value;const view=stateView(state||{});if(view.authenticated||view.loginPending||view.bound)loginWatchUntil=0;renderState();},onError:error=>{loginWatchUntil=0;state={...(state||{}),steam:{...(state?.steam||{}),authenticated:false},error:error?.message||String(error)};renderState();}});
    $('auction-sell-enabled').addEventListener('click',event=>{
      if(disabled||controller.busy){event.preventDefault();return;}
      if(!current.upload_enabled||!current.sell_enabled&&!stateView(state||{}).authenticated){
        event.preventDefault();explainSalePrerequisite();
      }
    });
    $('managed-auction').addEventListener('change',event=>{
      if(disabled||!event.target.matches('input,select')||event.target.id==='auction-excluded-search')return;
      if(event.target.id==='auction-sell-enabled'&&event.target.checked&&(!current.upload_enabled||controller.busy||!stateView(state||{}).authenticated)){
        event.target.checked=current.sell_enabled;renderState();if(!controller.busy)explainSalePrerequisite();controller.refresh();return;
      }
      current=read();options.onChange?.(current);
    });
    $('auction-excluded-search').addEventListener('input',renderChoices);
    const action=async name=>{
      if(controller.busy)return;
      const view=stateView(state||{});
      if(name==='login'&&(!view.loginAvailable||view.authenticated||view.loginPending||Date.now()<loginWatchUntil))return;
      if(name==='logout'&&!view.authenticated&&!view.loginPending&&!view.bound)return;
      if(name==='logout'&&await options.confirmAction?.('退出助手中的 Steam 登录？','停止自动出售并清除助手保存的登录会话。已经提交的上传、出售或手机确认不会撤回。','退出 Steam',true)===false)return;
      loginWatchUntil=name==='login'?Date.now()+30000:0;
      const pending=controller.action(name);renderState();await pending;renderState();
    };
    $('auction-login').addEventListener('click',()=>action('login'));
    $('auction-logout').addEventListener('click',()=>action('logout'));
    $('auction-refresh').addEventListener('click',()=>controller.refresh());
    const fold=root.document.querySelector('[data-managed-fold="auction"]');
    fold?.addEventListener('toggle',()=>{if(fold.open)controller.refresh();});
    root.setInterval(()=>{const view=stateView(state||{});if(fold?.open||view.loginPending||view.bound&&!view.authenticated||view.pendingConfirmation||Date.now()<loginWatchUntil)controller.refresh();},5000);
    render(current,catalog);
  }
  root.AuctionUI={init,render,read,helpers};
})(typeof window!=='undefined'?window:globalThis);
