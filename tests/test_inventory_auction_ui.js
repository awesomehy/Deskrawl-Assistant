'use strict';
const fs=require('node:fs'),vm=require('node:vm'),{test}=require('node:test'),assert=require('node:assert/strict');
const helpers=require('../deskrawl_assistant/web/inventory-auction.js');
const source=fs.readFileSync('deskrawl_assistant/web/inventory-auction.js','utf8');
const clone=value=>JSON.parse(JSON.stringify(value)),flush=()=>new Promise(resolve=>setImmediate(resolve));
const selected=[{selection_id:'first',name:'宏伟符文',count:1,container:'storage'},{selection_id:'second',name:'金条',count:12,container:'inventory'}];
const available={connected:true,complete:true,busy:false,managed:{running:false},manual_auction_availability:{available:true,reason:''}};
const ready=()=>({token:'preview-token',can_start:true,ready_count:1,skipped_count:1,items:[{name:'宏伟符文',count:1,container:'storage',status:'ready'},{name:'金条',count:12,status:'skipped',reason:'当前不支持此类堆叠商品上传。'}]});
function page(initialBatch={}){
  const nodes=new Map(),calls=[],timers=[];let batch=initialBatch,preview=ready(),steam={authenticated:false};
  function node(id){if(nodes.has(id))return nodes.get(id);const events={};const result={id,hidden:false,disabled:false,checked:false,textContent:'',innerHTML:'',value:'lowest_sell',open:false,title:'',
    setAttribute(name,value){this[name]=value;},addEventListener:(name,fn)=>{(events[name]??=[]).push(fn);},emit:async(name,event={})=>{for(const fn of events[name]||[])await fn(event);},showModal(){this.open=true;},close(){this.open=false;}};nodes.set(id,result);return result;}
  const window={document:{getElementById:node},setInterval(fn){timers.push(fn);}};
  let handler=async(path,data)=>{
    if(path==='auction/manual/state')return clone(batch);
    if(path==='auction/manual/preview')return clone(preview);
    if(path==='auction/manual/start'){batch={mode:'sell',running:true,can_cancel:true,total:1,completed:0,items:[{name:'宏伟符文',status:'running'}],message:'正在上传所选商品'};return clone(batch);}
    if(path==='auction/manual/cancel'){batch={...batch,can_cancel:false,message:'正在核验已提交商品，然后停止后续操作。'};return clone(batch);}
    if(path==='auction/state')return {login_available:false,steam:clone(steam)};
    throw Error(path);
  };
  vm.runInContext(source,vm.createContext({window}));
  window.InventoryAuctionUI.init({api:async(path,data)=>{calls.push({path,...(data===undefined?{}:{data:clone(data)})});return handler(path,data);},onRefresh:async()=>{}});
  window.InventoryAuctionUI.update(available,selected);
  return {window,node,calls,timers,setSteam:value=>{steam=value;},setBatch:value=>{batch=value;},setPreview:value=>{preview=value;},setHandler:value=>{handler=value;},async click(id){const event={defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};await node(id).emit('click',event);await flush();return event;},async change(id,value){const element=node(id);if(typeof value==='boolean')element.checked=value;else element.value=value;await element.emit('change');await flush();},async poll(){for(const fn of timers)fn();await flush();}};
}

test('selection gating refuses disconnected, incomplete, busy and managed states without inferring eligibility',()=>{
  assert.match(helpers.availability({},selected),/连接/);
  assert.match(helpers.availability({...available,complete:false},selected),/读取完整/);
  assert.match(helpers.availability({...available,busy:true},selected),/当前操作/);
  assert.match(helpers.availability({...available,managed:{finishing:true}},selected),/停止托管/);
  assert.match(helpers.availability(available,selected,{running:true}),/进度/);
  assert.match(helpers.availability(available,[]),/勾选/);
  assert.equal(helpers.availability(available,selected),'');
  assert.match(helpers.availability({...available,manual_auction_availability:null},selected),/状态尚未确认/);
  assert.match(helpers.availability({...available,manual_auction_availability:{available:false,reason:'手动上架仅支持在主城操作，请先回城。'}},selected),/主城/);
  assert.deepEqual(helpers.cleanIds([...selected,selected[0],{},null]),['first','second']);
});

test('combat or unknown game state disables the entry and refuses even a dispatched click',async()=>{
  const p=page();await flush();
  for(const readiness of [null,{available:false,reason:'手动上架仅支持在主城操作，请先回城。'},
      {available:false,reason:'场景切换或传送中，请等待到达主城后再上架。'}]){
    p.window.InventoryAuctionUI.update({...available,manual_auction_availability:readiness},selected);
    assert.equal(p.node('inventory-auction-upload').disabled,true);
    assert.equal(p.node('inventory-auction-availability').hidden,false);
    assert.match(p.node('inventory-auction-upload').title,/主城|状态尚未确认/);
    await p.click('inventory-auction-upload');
    assert.equal(p.node('inventory-auction-dialog').open,false);
  }
  assert.equal(p.calls.some(c=>c.path==='auction/manual/preview'||c.path==='auction/manual/start'),false);
  p.window.InventoryAuctionUI.update(available,selected);
  assert.equal(p.node('inventory-auction-upload').disabled,false);
  assert.equal(p.node('inventory-auction-availability').hidden,true);
});

test('entering combat with a confirmed preview disables confirmation, price and Steam controls',async()=>{
  const p=page();p.setSteam({authenticated:true});await flush();await p.click('inventory-auction-upload');
  assert.equal(p.node('inventory-auction-submit').disabled,false);
  const before=p.calls.filter(c=>c.path==='auction/manual/preview').length;
  p.window.InventoryAuctionUI.update({...available,manual_auction_availability:{available:false,reason:'手动上架仅支持在主城操作，请先回城。'}},selected);
  for(const id of ['inventory-auction-submit','inventory-auction-recheck','inventory-auction-price','inventory-auction-with-steam'])assert.equal(p.node(id).disabled,true,id);
  assert.match(p.node('inventory-auction-error').textContent,/主城/);
  await p.click('inventory-auction-submit');await p.click('inventory-auction-recheck');
  await p.change('inventory-auction-with-steam',true);await p.change('inventory-auction-price','highest_buy');
  assert.equal(p.calls.some(c=>c.path==='auction/manual/start'),false);
  assert.equal(p.calls.filter(c=>c.path==='auction/manual/preview').length,before);
  p.window.InventoryAuctionUI.update(available,selected);
  assert.equal(p.node('inventory-auction-submit').disabled,false);
  assert.equal(p.node('inventory-auction-error').hidden,true);
});

test('a scene change while Steam login is being rechecked cannot submit the old preview',async()=>{
  const p=page();p.setSteam({authenticated:true});await flush();await p.click('inventory-auction-upload');
  await p.change('inventory-auction-with-steam',true);
  let finish;
  p.setHandler(path=>{
    if(path==='auction/state')return new Promise(resolve=>{finish=resolve;});
    if(path==='auction/manual/state')return {};
    throw Error('no write authorized');
  });
  const pending=p.click('inventory-auction-submit');await flush();
  p.window.InventoryAuctionUI.update({...available,manual_auction_availability:{available:false,reason:'手动上架仅支持在主城操作，请先回城。'}},selected);
  finish({steam:{authenticated:true}});await pending;
  assert.equal(p.calls.some(c=>c.path==='auction/manual/start'),false);
  assert.equal(p.node('inventory-auction-submit').disabled,true);
});

test('opening upload only previews exact selected identifiers and final confirmation is required',async()=>{
  const p=page();await flush();await p.click('inventory-auction-upload');
  const request=p.calls.find(call=>call.path==='auction/manual/preview');
  assert.deepEqual(request.data,{items:['first','second'],mode:'upload',price_rule:'lowest_sell'});
  assert.equal(p.node('inventory-auction-dialog').open,true);
  assert.match(p.node('inventory-auction-items').innerHTML,/金条.*数量 12.*堆叠商品/s);
  assert.match(p.node('inventory-auction-submit').textContent,/确认上传 1 组/);
  assert.equal(p.calls.some(call=>call.path==='auction/manual/start'),false);
  await p.click('inventory-auction-submit');
  assert.deepEqual(p.calls.find(call=>call.path==='auction/manual/start').data,{token:'preview-token'});
  assert.equal(p.node('inventory-auction-progress').hidden,false);
  assert.equal(p.node('inventory-auction-cancel').hidden,false);
});

test('manual Steam mode and price repreview without lock authorization or changing managed settings',async()=>{
  const p=page();p.setSteam({authenticated:true,wallet_currency:'CNY'});await flush();await p.click('inventory-auction-upload');
  assert.equal(p.node('inventory-auction-with-steam').checked,false);
  await p.change('inventory-auction-with-steam',true);
  await p.change('inventory-auction-price','highest_buy');
  const requests=p.calls.filter(call=>call.path==='auction/manual/preview');
  assert.equal(requests[0].data.mode,'upload');
  assert.deepEqual(requests.at(-1).data,{items:['first','second'],mode:'sell',price_rule:'highest_buy'});
  assert.match(p.node('inventory-auction-description').textContent,/Steam.*确认/);
  assert.equal(p.node('inventory-auction-with-steam').disabled,false);
  assert.equal(p.calls.some(call=>call.path.includes('settings')||call.path.endsWith('/start')),false);
});

test('closing a running dialog does not cancel, explicit stop sends only cancel and keeps in-flight visible',async()=>{
  const p=page();await flush();await p.click('inventory-auction-upload');await p.click('inventory-auction-submit');
  await p.click('inventory-auction-close');assert.equal(p.calls.some(call=>call.path.endsWith('/cancel')),false);
  await p.click('inventory-auction-details');await p.click('inventory-auction-cancel');
  assert.deepEqual(p.calls.find(call=>call.path==='auction/manual/cancel').data,{});
  assert.equal(p.node('inventory-auction-cancel').disabled,true);assert.match(p.node('inventory-auction-summary').textContent,/核验已提交/);
});

test('refresh restores result and pending confirmation without claiming completed sale',async()=>{
  const p=page({running:false,mode:'sell',total:1,completed:1,items:[{name:'宏伟符文',count:1,status:'pending_confirmation',message:'请在 Steam 手机应用中确认。'}],message:'已提交，等待 Steam 确认'});
  await flush();assert.equal(p.node('inventory-auction-progress').hidden,false);await p.click('inventory-auction-details');
  assert.match(p.node('inventory-auction-items').innerHTML,/待 Steam 确认/);
  assert.match(p.node('inventory-auction-footer-note').textContent,/不代表已成交/);
  assert.equal(p.calls.every(call=>call.path==='auction/manual/state'),true);
});

test('previous completed result does not replace a fresh preview during status polling',async()=>{
  const p=page({running:false,total:1,completed:1,items:[{name:'上批商品',status:'uploaded'}]});
  await flush();await p.click('inventory-auction-upload');await p.poll();
  assert.match(p.node('inventory-auction-items').innerHTML,/宏伟符文/);assert.doesNotMatch(p.node('inventory-auction-items').innerHTML,/上批商品/);
  assert.equal(p.node('inventory-auction-submit').disabled,false);
});

test('closing then reopening uses the current selection without lock authorization',async()=>{
  const p=page();await flush();await p.click('inventory-auction-upload');
  await p.click('inventory-auction-close');p.window.InventoryAuctionUI.update(available,[selected[1]]);
  await p.click('inventory-auction-upload');
  const request=p.calls.filter(call=>call.path==='auction/manual/preview').at(-1);
  assert.deepEqual(request.data,{items:['second'],mode:'upload',price_rule:'lowest_sell'});
  assert.equal(p.calls.some(call=>call.path==='auction/manual/start'),false);
});

test('unsupported rows and escaped server text stay visible but cannot be submitted when none ready',async()=>{
  const p=page();await flush();p.setPreview({token:'x',can_start:false,ready_count:0,skipped_count:1,items:[{name:'<img onerror="bad">',count:2,status:'skipped',reason:'<script>bad</script>'}]});
  await p.click('inventory-auction-upload');assert.equal(p.node('inventory-auction-submit').disabled,true);
  assert.match(p.node('inventory-auction-items').innerHTML,/&lt;img/);assert.doesNotMatch(p.node('inventory-auction-items').innerHTML,/<script>|<img/);
  await p.click('inventory-auction-submit');assert.equal(p.calls.some(call=>call.path.endsWith('/start')),false);
});

test('ambiguous start never resubmits the same consumed preview token',async()=>{
  const calls=[];const c=helpers.createController({api:async(path,data)=>{calls.push({path,data});if(path.endsWith('/preview'))return ready();if(path.endsWith('/start'))throw Error('连接断开');return {};}});
  await c.preview({items:['first']});await c.start();await c.start();
  assert.equal(calls.filter(call=>call.path.endsWith('/start')).length,1);assert.equal(c.value.uncertainStart,true);assert.equal(c.value.preview.token,null);
});

test('ambiguous new start cannot display the previous batch as its result and recheck only polls',async()=>{
  const old={batch_id:'old-batch',running:false,total:1,completed:1,items:[{name:'旧商品',status:'uploaded'}]};
  const p=page(old);await flush();await p.click('inventory-auction-upload');
  p.setHandler(async path=>{if(path.endsWith('/start'))throw Error('响应丢失');if(path.endsWith('/state'))return old;throw Error('不应重新提交');});
  await p.click('inventory-auction-submit');
  assert.match(p.node('inventory-auction-items').innerHTML,/宏伟符文/);assert.doesNotMatch(p.node('inventory-auction-items').innerHTML,/旧商品/);
  assert.equal(p.node('inventory-auction-submit').disabled,true);
  const previews=p.calls.filter(call=>call.path.endsWith('/preview')).length;
  await p.click('inventory-auction-recheck');assert.equal(p.calls.filter(call=>call.path.endsWith('/preview')).length,previews);
  p.setHandler(async()=>({batch_id:'new-batch',running:false,total:1,completed:1,items:[{name:'新商品',status:'pending_confirmation'}]}));
  await p.click('inventory-auction-recheck');assert.match(p.node('inventory-auction-items').innerHTML,/新商品/);assert.equal(p.node('inventory-auction-submit').hidden,true);
});

test('definite HTTP rejection leaves current preview for fresh verification rather than showing an old result',async()=>{
  const old={batch_id:'old',running:false,total:1,completed:1,items:[{name:'旧商品',status:'uploaded'}]};
  const p=page(old);await flush();await p.click('inventory-auction-upload');
  p.setHandler(async path=>{if(path.endsWith('/start')){const error=Error('当前场景已变化');error.status=400;throw error;}if(path.endsWith('/preview'))return ready();return old;});
  await p.click('inventory-auction-submit');assert.match(p.node('inventory-auction-items').innerHTML,/宏伟符文/);assert.doesNotMatch(p.node('inventory-auction-items').innerHTML,/旧商品/);
  await p.click('inventory-auction-recheck');assert.equal(p.calls.filter(call=>call.path.endsWith('/preview')).length,2);
});

test('late status response cannot overwrite a newer batch returned by start',async()=>{
  let resolveStatus;const c=helpers.createController({api:async path=>path.endsWith('/preview')?ready():path.endsWith('/start')?{running:true,total:1,phase:'uploading'}:new Promise(resolve=>{resolveStatus=resolve;})});
  await c.preview({items:['first']});const stale=c.refresh();const start=c.start();await flush();
  resolveStatus({running:false,total:0});await stale;await start;
  assert.equal(c.value.batch.running,true);assert.equal(c.value.batch.phase,'uploading');
});

test('production page exposes one auction action with a login-gated Steam sale checkbox',()=>{
  const html=fs.readFileSync('deskrawl_assistant/web/index.html','utf8');
  assert.ok(html.indexOf('/inventory-auction.js')<html.indexOf('/app.js'));
  assert.match(html,/id="inventory-auction-upload"[^>]*>上架交易所/);
  assert.doesNotMatch(html,/id="inventory-auction-sell"|id="inventory-auction-login"/);
  assert.doesNotMatch(html,/inventory-auction-allow-locked|本次允许上传选中的已锁定物品/);
  assert.doesNotMatch(source,/inventory-auction-allow-locked|allow_locked/);
  assert.match(html,/id="inventory-auction-with-steam"[^>]*aria-disabled="true"[^>]*>同时上架 Steam 市场/);
  assert.match(html,/id="inventory-auction-login-tip"[^>]*role="status"[^>]*hidden>请先前往“托管模式 → 拍卖行与 Steam 市场 → Steam 登录”/);
  assert.match(html,/id="inventory-auction-dialog"[^>]*aria-labelledby="inventory-auction-title"/);
  assert.match(html,/不使用托管模式的自动出售范围与保留设置/);
});

test('manual sale remains unchecked and unavailable until the shared Steam session is authenticated',async()=>{
  for(const steam of [{},{bound:true},{login_pending:true},{authenticated:'true'}]){
    const p=page();p.setSteam(steam);await flush();await p.click('inventory-auction-upload');
    assert.equal(p.node('inventory-auction-with-steam').checked,false);
    assert.equal(p.node('inventory-auction-with-steam').disabled,false);
    assert.equal(p.node('inventory-auction-with-steam')['aria-disabled'],'true');
    assert.equal(p.node('inventory-auction-login-tip').hidden,true);
    const event=await p.click('inventory-auction-with-steam');
    assert.equal(event.defaultPrevented,true);
    assert.equal(p.node('inventory-auction-login-tip').hidden,false);
    await p.change('inventory-auction-with-steam',true);
    assert.equal(p.node('inventory-auction-with-steam').checked,false);
    assert.equal(p.node('inventory-auction-price-field').hidden,true);
    assert.equal(p.calls.filter(call=>call.path==='auction/manual/preview').length,1);
    assert.equal(p.calls.some(call=>call.path==='auction/login'||call.path.includes('settings')),false);
    assert.match(p.node('inventory-auction-steam-note').textContent,/托管模式/);
  }
});

test('binding tip clears after verified login and reopening while an unverified attempt cannot request a sale',async()=>{
  const p=page();await flush();await p.click('inventory-auction-upload');
  await p.click('inventory-auction-with-steam');
  assert.equal(p.node('inventory-auction-login-tip').hidden,false);
  assert.equal(p.calls.some(call=>call.data?.mode==='sell'||call.path==='auction/manual/start'),false);
  p.setSteam({authenticated:true});await p.poll();
  assert.equal(p.node('inventory-auction-login-tip').hidden,true);
  assert.equal(p.node('inventory-auction-with-steam')['aria-disabled'],'false');
  const event=await p.click('inventory-auction-with-steam');assert.equal(event.defaultPrevented,false);
  await p.change('inventory-auction-with-steam',true);
  assert.equal(p.calls.filter(call=>call.path==='auction/manual/preview').at(-1).data.mode,'sell');
  await p.click('inventory-auction-close');p.setSteam({authenticated:false});await p.click('inventory-auction-upload');
  assert.equal(p.node('inventory-auction-login-tip').hidden,true);
});

test('live logout cancels sale selection and invalidates the old preview without silently uploading',async()=>{
  const p=page();p.setSteam({authenticated:true});await flush();await p.click('inventory-auction-upload');
  await p.change('inventory-auction-with-steam',true);
  assert.equal(p.node('inventory-auction-submit').disabled,false);
  p.setSteam({authenticated:false});await p.poll();
  assert.equal(p.node('inventory-auction-with-steam').checked,false);
  assert.equal(p.node('inventory-auction-submit').disabled,true);
  await p.click('inventory-auction-submit');assert.equal(p.calls.some(call=>call.path==='auction/manual/start'),false);
  await p.click('inventory-auction-recheck');
  assert.equal(p.calls.filter(call=>call.path==='auction/manual/preview').at(-1).data.mode,'upload');
});

test('sale submit freshly checks login and never downgrades a rejected sale into upload-only',async()=>{
  for(const failedRead of [false,true]){
    const p=page();p.setSteam({authenticated:true});await flush();await p.click('inventory-auction-upload');
    await p.change('inventory-auction-with-steam',true);
    p.setHandler(async path=>{
      if(path==='auction/state'){if(failedRead)throw Error('offline');return {steam:{authenticated:false}};}
      if(path==='auction/manual/state')return {};
      throw Error('no other operations authorized');
    });
    await p.click('inventory-auction-submit');
    assert.equal(p.calls.filter(call=>call.path==='auction/state').length,2);
    assert.equal(p.calls.some(call=>call.path==='auction/manual/start'),false);
    assert.equal(p.node('inventory-auction-submit').disabled,true);
  }
});

test('valid sale submit rechecks shared login then consumes only its preview token once',async()=>{
  const p=page();p.setSteam({authenticated:true});await flush();await p.click('inventory-auction-upload');
  await p.change('inventory-auction-with-steam',true);await p.click('inventory-auction-submit');
  const start=p.calls.findIndex(call=>call.path==='auction/manual/start');
  assert.equal(p.calls[start-1].path,'auction/state');
  assert.deepEqual(p.calls[start].data,{token:'preview-token'});
  await p.click('inventory-auction-submit');
  assert.equal(p.calls.filter(call=>call.path==='auction/manual/start').length,1);
});
