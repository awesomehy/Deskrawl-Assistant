'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict'),{test}=require('node:test');
const source=fs.readFileSync('deskrawl_assistant/web/steam-market.js','utf8');
const appSource=fs.readFileSync('deskrawl_assistant/web/app.js','utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const ready={eligible:true,status:'ready',lowest_sell:'2.30',highest_buy:'2.27',currency:'HKD',market_url:'https://steamcommunity.com/market/listings/4623570/Gold%20Bar'};
const items=count=>Array.from({length:count},(_,i)=>({selection_id:`item-${i}`}));

const currencies=[{id:1,code:'USD',name:'美元'},{id:23,code:'CNY',name:'人民币'},{id:29,code:'HKD',name:'港币'}];
function page(nodes=[],options={}){
  const calls=[],pending=[],settingsCalls=[],settingsPending=[];
  const server={currency_id:options.currencyId||0};
  const clock={now:0};
  const selector={value:'0',innerHTML:'',disabled:true,events:{},addEventListener(type,handler){this.events[type]=handler;}},error={hidden:true,textContent:''};
  const context={window:{},document:{querySelectorAll:()=>nodes,getElementById:id=>id==='market-currency'?selector:id==='market-currency-error'?error:null},setInterval:()=>{},Intl,URL,Date:{now:()=>clock.now},
    api:(path,data)=>new Promise((resolve,reject)=>{
      if(path==='market/settings'){
        settingsCalls.push({path,data:data===undefined?undefined:{...data}});
        if(data===undefined&&options.autoSettings!==false)resolve({currency_id:server.currency_id,currencies});
        else settingsPending.push({resolve(value){server.currency_id=value.currency_id;resolve(value);},reject});
        return;
      }
      const requestedCurrency=server.currency_id;
      calls.push({path,items:Array.from(data.items)});pending.push({resolve(value){resolve({currency_id:requestedCurrency,...value});},reject});
    })};
  vm.createContext(context);vm.runInContext(source,context);
  return {ui:context.window.SteamMarketUI,context,calls,pending,settingsCalls,settingsPending,selector,error,server,
    advance(milliseconds){clock.now+=milliseconds;},
    change(value){selector.value=String(value);return selector.events.change();}};
}

test('quotes display both order sides in their actual currency and escape item identity',()=>{
  const {ui}=page();
  assert.match(ui.content(ready),/2\.30/);assert.match(ui.content(ready),/2\.27/);
  assert.match(ui.content(ready),/在售最低/);assert.match(ui.content(ready),/求购最高/);
  assert.match(ui.content(ready),/HK\$/);
  assert.equal(ui.content({eligible:false}), '');
  assert.match(ui.amount(null,ready),/暂无订单/);
  assert.match(ui.content({...ready,stale:true}),/缓存报价/);
  assert.match(ui.content({...ready,market_url:'https://evil.invalid/'},true),/Steam 社区市场/);
  assert.doesNotMatch(ui.content({...ready,market_url:'https://evil.invalid/'},true),/href=/);
  assert.doesNotMatch(ui.slot({selection_id:'x',container:'equipped'}),/data-market/);
  assert.match(ui.slot({selection_id:'<x>',container:'inventory'}),/&lt;x&gt;/);
});

test('converted prices are approximate references and detail preserves original Steam amounts and exchange date',()=>{
  const {ui,selector}=page();
  const quote={...ready,converted:true,currency:'CNY',currency_id:23,lowest_sell:'2.09',highest_buy:'2.06',
    original_currency:'HKD',original_currency_id:29,original_lowest_sell:'2.30',original_highest_buy:'2.27',
    exchange_rate:'0.909',exchange_rate_date:'2026-10-17',exchange_rate_stale:false,price_note:'汇率换算参考价，交易以原始报价为准。'};
  const inline=ui.content(quote),detail=ui.content(quote,true);
  assert.match(inline,/参考价/);assert.match(inline,/≈ ¥2\.09/);assert.match(inline,/≈ ¥2\.06/);
  assert.doesNotMatch(inline,/HK\$|2\.30/,'row uses only target reference values');
  assert.match(detail,/Steam 原始报价（HKD）/);assert.match(detail,/HK\$2\.30/);assert.match(detail,/HK\$2\.27/);
  assert.match(detail,/1 HKD = 0\.909 CNY/);assert.match(detail,/汇率日期 2026-10-17/);
  assert.match(detail,/汇率换算参考价/);assert.doesNotMatch(detail,/缓存汇率/);
  assert.match(ui.content({...quote,exchange_rate_stale:true},true),/缓存汇率/);
  assert.match(selector.title,/参考价/);
  assert.doesNotMatch(ui.content(ready),/参考价|≈/,'automatic native quotes remain exact Steam values');
});

test('FX failures do not display pretend target prices but original prices remain available in detail',()=>{
  const {ui}=page();
  for(const status of ['queued','unavailable']){
    const quote={...ready,status,currency:'CNY',lowest_sell:null,highest_buy:null,converted:false,
      original_currency:'HKD',original_lowest_sell:'2.30',original_highest_buy:'2.27',
      message:'参考汇率暂不可用',price_note:'汇率获取失败，原始 Steam 报价仍可参考。'};
    assert.equal(ui.content(quote),'参考汇率暂不可用');
    const detail=ui.content(quote,true);
    assert.match(detail,/HK\$2\.30/);assert.doesNotMatch(detail,/¥|≈|参考汇率：/);
  }
});

test('converted details escape notes and metadata and do not approximate absent orders',()=>{
  const {ui}=page();
  const quote={...ready,converted:true,lowest_sell:null,highest_buy:'1.00',currency:'CNY',
    original_currency:'HKD',original_lowest_sell:null,original_highest_buy:'2.27',
    exchange_rate:'<img src=x>',exchange_rate_date:'<script>',price_note:'<script>alert(1)</script>'};
  assert.match(ui.content(quote),/在售最低 暂无订单/);assert.doesNotMatch(ui.content(quote),/≈ 暂无订单/);
  const detail=ui.content(quote,true);
  assert.match(detail,/&lt;img src=x&gt;/);assert.match(detail,/&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(detail,/汇率日期 未提供/);assert.doesNotMatch(detail,/<script>|<img/);
});

test('opening inventory requests only its filtered rows rather than all state items',async()=>{
  const p=page(),all=items(200),visible=[all[199]];
  const match=appSource.match(/function switchTab\(tab\)\{[\s\S]*?\n\}/);
  assert.ok(match,'real switchTab function is available');
  Object.assign(p.context,{state:{items:all},activeTab:'rules',closeColumnMenu(){},
    $:()=>({hidden:false}),renderInventory:()=>p.ui.update(visible,true)});
  p.context.document.querySelectorAll=()=>[];
  vm.runInContext(match[0],p.context);
  vm.runInContext("switchTab('inventory')",p.context);
  await flush();
  assert.deepEqual(p.calls.map(call=>call.items),[['item-199']]);
  p.pending.shift().resolve({items:[]});await flush();
  assert.equal(p.calls.length,1);
  vm.runInContext("switchTab('rules')",p.context);
});

test('a filter change discards late quotes and cancels the hidden remaining batches',async()=>{
  const oldNode={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true};
  const newNode={dataset:{marketItem:'item-199'},innerHTML:'',hidden:true};
  const p=page([oldNode,newNode]),all=items(200);
  p.ui.update(all,true);
  await flush();
  p.ui.update([all[199]],true);
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.deepEqual(p.calls.map(call=>call.items.length),[128,1]);
  assert.deepEqual(p.calls[1].items,['item-199']);
  assert.equal(oldNode.hidden,true,'late hidden-row response cannot restore an obsolete quote');
  p.pending.shift().resolve({items:[{selection_id:'item-199',quote:ready}]});await flush();
  assert.equal(newNode.hidden,false);assert.match(newNode.innerHTML,/2\.30/);
  assert.equal(p.calls.length,2);
});

test('state polling and sorting the same visible IDs do not interrupt long batches',async()=>{
  const node={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true};
  const p=page([node]),all=items(200);
  p.ui.update(all,true);
  await flush();
  p.ui.update([...all].reverse().map(row=>({...row,count:2})),true);
  assert.equal(p.calls.length,1,'state polling does not submit a parallel query');
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.deepEqual(p.calls.map(call=>call.items.length),[128,72]);
  assert.equal(node.hidden,false,'unchanged identities preserve the first batch response');
  assert.equal(p.calls[1].items[0],'item-128','the original generation finishes its remaining batch');
  p.pending.shift().resolve({items:[]});await flush();
  assert.equal(p.calls.length,2);
});

test('leaving inventory cancels remaining batches and ignores the in-flight result',async()=>{
  const node={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true},p=page([node]);
  p.ui.update(items(200),true);
  await flush();
  p.ui.update([],false);
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.equal(p.calls.length,1);assert.equal(node.hidden,true);
});

test('failed obsolete request releases its busy flag and starts the new visible set',async()=>{
  const p=page();
  p.ui.update([{selection_id:'old'}],true);
  await flush();
  p.ui.update([{selection_id:'new'}],true);
  p.pending.shift().reject(new Error('offline'));await flush();
  assert.deepEqual(p.calls.map(call=>call.items),[['old'],['new']]);
  p.pending.shift().resolve({items:[]});await flush();
  assert.equal(p.calls.length,2);
});

test('opening inventory loads the saved currency before querying quotes and only loads once',async()=>{
  const p=page([],{autoSettings:false});
  p.ui.update(items(1),false);assert.equal(p.settingsCalls.length,0);
  p.ui.update(items(1),true);assert.equal(p.settingsCalls.length,1);assert.equal(p.calls.length,0);
  assert.equal(p.selector.disabled,true);
  p.settingsPending.shift().resolve({currency_id:23,currencies});await flush();
  assert.equal(p.selector.value,'23');assert.equal(p.selector.disabled,false);
  assert.match(p.selector.innerHTML,/人民币 \(CNY\)/);assert.match(p.selector.innerHTML,/value="0"/);
  assert.deepEqual(p.calls[0],{path:'market/quotes',items:['item-0']});
  p.pending.shift().resolve({items:[]});await flush();
  p.ui.update([],false);p.ui.update(items(1),true);
  assert.equal(p.settingsCalls.length,1,'tab reopening retains loaded local preference');
  p.pending.shift().resolve({items:[]});await flush();
});

test('currency changes save the preference, clear old amounts and reject late old-currency results',async()=>{
  const node={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true},p=page([node]);
  p.ui.update(items(1),true);await flush();
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.match(node.innerHTML,/HK\$/);
  p.ui.update(items(1),true);assert.equal(p.pending.length,1);
  const changing=p.change(23);
  assert.equal(node.hidden,true);assert.equal(node.innerHTML,'');assert.equal(p.selector.disabled,true);
  assert.deepEqual(p.settingsCalls[1],{path:'market/settings',data:{currency_id:23}});
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.equal(node.hidden,true);assert.equal(p.calls.length,2,'no quote requests while settings save is pending');
  p.settingsPending.shift().resolve({currency_id:23,currencies});await changing;await flush();
  assert.equal(p.calls.length,3);assert.equal(p.selector.value,'23');assert.equal(p.selector.disabled,false);
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:{...ready,currency:'CNY',lowest_sell:'1.80'}}]});await flush();
  assert.match(node.innerHTML,/1\.80/);assert.doesNotMatch(node.innerHTML,/HK\$|2\.30/);
});

test('settings read failure keeps quotes unrequested and a later inventory update retries',async()=>{
  const p=page([],{autoSettings:false});p.ui.update(items(1),true);
  p.settingsPending.shift().reject(new Error('读取失败'));await flush();
  assert.equal(p.calls.length,0);assert.equal(p.selector.disabled,true);assert.equal(p.error.hidden,false);
  p.ui.update(items(1),true);assert.equal(p.settingsCalls.length,2);
  p.settingsPending.shift().resolve({currency_id:29,currencies});await flush();
  assert.equal(p.selector.value,'29');assert.equal(p.error.hidden,true);
  p.pending.shift().resolve({items:[]});await flush();
});

test('save rejection reads persisted settings instead of assuming the previous value',async()=>{
  const node={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true},p=page([node],{currencyId:29});
  p.ui.update(items(1),true);await flush();
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  const changing=p.change(23);p.settingsPending.shift().reject(new Error('保存失败'));await changing;await flush();
  assert.equal(p.selector.value,'29');assert.equal(p.selector.disabled,false);assert.equal(p.error.hidden,true);
  assert.equal(p.settingsCalls.length,3);assert.equal(p.settingsCalls[2].data,undefined,'save failure requires GET readback');
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.match(node.innerHTML,/HK\$/);assert.match(node.innerHTML,/2\.30/);
});

test('lost POST response after commit reads back the saved currency before accepting converted quotes',async()=>{
  const node={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true},p=page([node],{currencyId:29});
  p.ui.update(items(1),true);await flush();
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  const changing=p.change(23);p.server.currency_id=23;
  p.settingsPending.shift().reject(new Error('response lost after commit'));await changing;await flush();
  assert.equal(p.selector.value,'23');assert.equal(p.selector.disabled,false);assert.equal(p.settingsCalls.length,3);
  assert.equal(node.hidden,true,'old HKD quote stays hidden through reconciliation');
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:{...ready,converted:true,currency:'CNY',lowest_sell:'2.09',highest_buy:'2.06'}}]});await flush();
  assert.match(node.innerHTML,/参考价/);assert.match(node.innerHTML,/¥2\.09/);assert.doesNotMatch(node.innerHTML,/HK\$/);
});

test('failed readback after ambiguous save blocks quotes until a successful settings retry',async()=>{
  const p=page([],{autoSettings:false});p.ui.update(items(1),true);
  p.settingsPending.shift().resolve({currency_id:29,currencies});await flush();
  p.pending.shift().resolve({items:[]});await flush();
  const changing=p.change(23);p.settingsPending.shift().reject(new Error('response lost'));await flush();
  assert.equal(p.settingsCalls.length,3);assert.equal(p.calls.length,1);assert.equal(p.selector.disabled,true);
  p.settingsPending.shift().reject(new Error('readback unavailable'));await changing;await flush();
  assert.equal(p.calls.length,1);assert.equal(p.selector.disabled,true);assert.equal(p.error.hidden,false);
  p.ui.update(items(1),true);assert.equal(p.settingsCalls.length,4);
  p.settingsPending.shift().resolve({currency_id:23,currencies});await flush();
  assert.equal(p.selector.value,'23');assert.equal(p.selector.disabled,false);assert.equal(p.calls.length,2);
  p.pending.shift().resolve({items:[]});await flush();
});

test('quote metadata changed by another view clears old prices and reloads settings before new queries',async()=>{
  const node={dataset:{marketItem:'item-0'},innerHTML:'',hidden:true},p=page([node],{autoSettings:false});
  p.ui.update(items(1),true);p.settingsPending.shift().resolve({currency_id:29,currencies});await flush();
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:ready}]});await flush();
  assert.match(node.innerHTML,/HK\$/);
  p.ui.update(items(1),true);
  p.pending.shift().resolve({currency_id:23,items:[{selection_id:'item-0',quote:{...ready,currency:'CNY',lowest_sell:'2.09'}}]});await flush();
  assert.equal(node.hidden,true);assert.equal(node.innerHTML,'');assert.equal(p.selector.disabled,true);
  assert.equal(p.settingsCalls.length,2);assert.equal(p.calls.length,2,'mismatched quote cannot trigger queries before GET completes');
  p.settingsPending.shift().resolve({currency_id:23,currencies});await flush();
  assert.equal(p.selector.value,'23');assert.equal(p.calls.length,3);
  p.pending.shift().resolve({items:[{selection_id:'item-0',quote:{...ready,converted:true,currency:'CNY',lowest_sell:'2.09'}}]});await flush();
  assert.match(node.innerHTML,/参考价/);assert.match(node.innerHTML,/¥2\.09/);
});

test('missing quote preference metadata reconciles even empty batches and leaving the tab prevents follow-up queries',async()=>{
  const p=page([],{autoSettings:false});p.ui.update(items(1),true);
  p.settingsPending.shift().resolve({currency_id:0,currencies});await flush();
  p.pending.shift().resolve({currency_id:undefined,items:[]});await flush();
  assert.equal(p.settingsCalls.length,2);assert.equal(p.selector.disabled,true);
  p.ui.update([],false);p.settingsPending.shift().resolve({currency_id:23,currencies});await flush();
  assert.equal(p.calls.length,1);assert.equal(p.selector.value,'23');
});

test('repeated missing quote metadata respects a cooldown despite successful settings reloads and inventory polling',async()=>{
  const p=page();p.ui.update(items(1),true);await flush();
  assert.equal(p.calls.length,1);
  p.pending.shift().resolve({currency_id:undefined,items:[]});await flush();
  assert.equal(p.settingsCalls.length,2,'malformed response gets one settings readback');
  assert.equal(p.calls.length,1,'successful readback cannot immediately loop into quotes');
  assert.equal(p.error.hidden,false);assert.match(p.error.textContent,/接口版本不兼容/);
  for(let i=0;i<20;i++){p.ui.update(items(1),true);await flush();}
  assert.equal(p.calls.length,1);assert.equal(p.settingsCalls.length,2);
  p.advance(9999);p.ui.update(items(1),true);await flush();assert.equal(p.calls.length,1);
  p.advance(1);p.ui.update(items(1),true);await flush();assert.equal(p.calls.length,2);
  p.pending.shift().resolve({currency_id:undefined,items:[]});await flush();
  assert.equal(p.settingsCalls.length,3);assert.equal(p.calls.length,2);
  for(let i=0;i<20;i++){p.ui.update(items(1),true);await flush();}
  assert.equal(p.calls.length,2);assert.equal(p.settingsCalls.length,3);assert.match(p.error.textContent,/接口版本不兼容/);
  p.advance(10000);p.ui.update(items(1),true);await flush();
  p.pending.shift().resolve({currency_id:0,items:[]});await flush();
  assert.equal(p.calls.length,3);assert.equal(p.error.hidden,true,'a valid quote protocol clears the warning');
});

test('leaving inventory during currency save persists the choice without starting hidden queries',async()=>{
  const p=page();p.ui.update(items(1),true);await flush();
  p.pending.shift().resolve({items:[]});await flush();
  const changing=p.change(23);p.ui.update([],false);
  p.settingsPending.shift().resolve({currency_id:23,currencies});await changing;await flush();
  assert.equal(p.calls.length,1);assert.equal(p.selector.value,'23');
});
