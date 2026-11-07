'use strict';
const fs=require('node:fs'),vm=require('node:vm'),{test}=require('node:test'),assert=require('node:assert/strict');
const helpers=require('../deskrawl_assistant/web/auction-tracking.js');
const source=fs.readFileSync('deskrawl_assistant/web/auction-tracking.js','utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const rows=[{name:'金条',status:'waiting_town',count:1,container:'inventory'},
  {name:'南瓜王',status:'uploaded'}, {name:'宏伟符文',status:'pending_confirmation'},
  {name:'在售商品',status:'listed',my_price:'CNY 0.23',lowest_price:'CNY 0.21',can_reprice:true,tracking_id:'a'.repeat(32)},
  {name:'已售出商品',status:'sold',proceeds:'CNY 1.07'}];
function page(){
  const nodes=new Map(),calls=[],timers=[],timeouts=new Map();
  let state={items:rows,total:5},failure=null,start=null,nativeRefresh=null,previewResponse=null,timeoutId=0;
  function node(id){if(nodes.has(id))return nodes.get(id);const events={};const element={id,dataset:{},value:'',innerHTML:'',textContent:'',disabled:false,open:false,
    addEventListener(name,fn){(events[name]??=[]).push(fn);},async emit(name,event={}){for(const fn of events[name]||[])await fn(event);},
    setAttribute(name,value){this[name]=value;},showModal(){this.open=true;},close(){this.open=false;this.emit('close');}};nodes.set(id,element);return element;}
  const window={document:{getElementById:node},setInterval(fn){timers.push(fn);},
    setTimeout(fn,delay){timeouts.set(++timeoutId,{fn,delay});return timeoutId;},clearTimeout(id){timeouts.delete(id);}};
  const context=vm.createContext({window});vm.runInContext(source,context);
  const api=async(path,data)=>{calls.push({path,data});
    if(path==='auction/managed/state'){if(failure)throw failure;return state;}
    if(path==='auction/reprice/preview')return previewResponse?previewResponse():{token:'t'.repeat(43),currency:'CNY',my_price:'CNY 0.23',lowest_price:'CNY 0.21',default_price:'0.21'};
    if(path==='auction/reprice/start')return start?start():{running:true};
    if(path==='auction/sale/preview')return previewResponse?previewResponse():{token:'s'.repeat(43),currency:'CNY',lowest_price:'CNY 13.11',highest_price:'CNY 12.90',default_price:'13.11'};
    if(path==='auction/sale/start')return start?start():{running:true};
    if(path==='auction/tracking/refresh')return nativeRefresh?nativeRefresh():{message:'正在刷新拍卖行'};
    if(path==='auction/manual/state')return {running:false,phase:'completed',total:5,items:rows};
    if(path==='auction/state')return {};
    throw Error(path);};
  window.AuctionTrackingUI.init({api});
  return {node,calls,timers,window,context,api,setFailure:value=>{failure=value;},setStart:value=>{start=value;},setPreview:value=>{previewResponse=value;},setNativeRefresh:value=>{nativeRefresh=value;},setState:value=>{state=value;},
    async expireReads(){for(const {fn,delay} of [...timeouts.values()]){assert.equal(delay,8000);fn();}await flush();},
    async click(id){await node(id).emit('click');await flush();},
    async reprice(id='a'.repeat(32)){await node('managed-auction-tracking-items').emit('click',{target:{closest:()=>({dataset:{repriceId:id},disabled:false})}});await flush();},
    async poll(){for(const fn of timers)fn();await flush();}};
}

test('only an exact uploaded inventory asset offers manual pricing; stale and other stages do not',()=>{
  const row={id:'inventory-123',status:'uploaded',sale_available:true};
  assert.match(helpers.rowMarkup(row),/data-sale-id="inventory-123"/);
  for(const changes of [{status:'waiting_town'},{status:'listed'},{status:'pending_confirmation'},
    {status:'uncertain',previous_status:'uploaded'},{needs_verification:true},{id:'auction-abc'},{sale_available:false}])
    assert.equal(helpers.rowMarkup({...row,...changes}).includes('data-sale-id='),false);
  assert.equal(helpers.rowMarkup(helpers.invalidateLive([row])[0]).includes('data-sale-id='),false);
  assert.match(helpers.rowMarkup({...row,status:'uncertain',previous_status:'uploaded',sale_preview_available:true}),/data-sale-id="inventory-123"/);
});

const fees={minimum:7,increment:1,maximum:10000000,steam:'0.05',publisher:'0.10'};
test('fee guidance exposes the 12.63 gap and currency bounds without silently changing the price',()=>{
  assert.deepEqual(helpers.priceGuidance('12.63',fees).options,['12.62','12.65']);
  for(const price of ['12.62','12.65','13.11'])assert.equal(helpers.priceGuidance(price,fees).valid,true);
  assert.equal(helpers.priceGuidance('100000.01',fees).valid,false);
  assert.deepEqual(helpers.priceGuidance('0.01',fees).options,['0.21']);
  assert.equal(helpers.priceGuidance('1.23',null),null);
  assert.equal(helpers.priceGuidance('1.23',{...fees,steam:'<script>'}),null);
  assert.equal(helpers.priceGuidance('12.30',{...fees,minimum:100,increment:100}).valid,false);
  assert.equal(helpers.priceGuidance('12.00',{...fees,minimum:100,increment:100}).valid,true);
  assert.equal(helpers.priceGuidance('12.63',{...fees,steam:'0',publisher:'0'}).valid,true);
});

test('manual sale and repricing share price alternatives and cannot submit a fee gap',async()=>{
  for(const mode of ['sale','reprice']){
    const p=page();p.setPreview(()=>({token:'s'.repeat(43),currency:'CNY',fee_parameters:fees,default_price:'13.11'}));
    await p.window.AuctionTrackingUI[mode==='sale'?'openSale':'openReprice'](mode==='sale'?'inventory-123':'a'.repeat(32));
    p.node('auction-reprice-price').value='12.63';await p.node('auction-reprice-price').emit('input');
    assert.equal(p.node('auction-reprice-submit').disabled,true);
    assert.match(p.node('auction-reprice-price-note').innerHTML,/12.62.*12.65/);
    await p.click('auction-reprice-submit');assert.equal(p.calls.some(c=>c.path.endsWith('/start')),false);
    await p.node('auction-reprice-price-note').emit('click',{target:{closest:()=>({dataset:{priceOption:'12.65'}})}});
    assert.equal(p.node('auction-reprice-price').value,'12.65');
    assert.equal(p.node('auction-reprice-submit').disabled,false);
    assert.equal(p.node('auction-reprice-price-note').innerHTML,'');
    await p.click('auction-reprice-submit');
    assert.equal(p.calls.find(c=>c.path===`auction/${mode}/start`).data.price,'12.65');
  }
});

test('editor distinguishes accepted work from a submitted sale and polls its own validation failure',async()=>{
  const p=page();p.setStart(()=>({running:true,job_id:'a'.repeat(32),message:'正在核验所选物品'}));
  await p.window.AuctionTrackingUI.openSale('inventory-123');await p.click('auction-reprice-submit');
  assert.match(p.node('auction-reprice-note').textContent,/正在核验/);
  assert.doesNotMatch(p.node('auction-reprice-note').textContent,/已提交/);
  p.setState({items:[],reprice:{running:false,error:true,job_id:'b'.repeat(32),message:'另一个任务'}});
  await p.poll();assert.doesNotMatch(p.node('auction-reprice-note').textContent,/另一个任务/);
  p.setState({items:[],reprice:{running:false,error:true,job_id:'a'.repeat(32),message:'附近可用总价：12.62 或 12.65'}});
  await p.poll();assert.match(p.node('auction-reprice-error').textContent,/12.62.*12.65/);
  assert.equal(p.node('auction-reprice-price').disabled,false);
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/start').length,1);
  const reads=p.calls.length;await p.poll();assert.equal(p.calls.length,reads);
});

test('an immediately failed validation is never labelled submitted',async()=>{
  const p=page();p.setStart(()=>({running:false,error:true,message:'手续费已变化，未出售'}));
  await p.window.AuctionTrackingUI.openSale('inventory-123');await p.click('auction-reprice-submit');
  assert.match(p.node('auction-reprice-error').textContent,/未出售/);
  assert.doesNotMatch(p.node('auction-reprice-note').textContent,/已提交/);
});

test('manual sale shares the editor, shows ask and bid and defaults to ask with no cancellation',async()=>{
  const p=page();await p.window.AuctionTrackingUI.openSale('inventory-123');
  assert.equal(p.node('auction-reprice-dialog').open,true);
  assert.equal(p.node('auction-reprice-title').textContent,'定价上架 Steam 市场');
  assert.equal(p.node('auction-reprice-price').value,'13.11');
  assert.match(p.node('auction-reprice-note').textContent,/在售最低价 CNY 13.11.*求购最高价 CNY 12.90/);
  assert.equal(p.calls.some(c=>c.path.endsWith('/start')),false);
  p.node('auction-reprice-price').value='13.20';await p.click('auction-reprice-submit');
  assert.deepEqual(JSON.parse(JSON.stringify(p.calls.find(c=>c.path==='auction/sale/start').data)),{token:'s'.repeat(43),price:'13.20'});
  await p.click('auction-reprice-submit');assert.equal(p.calls.filter(c=>c.path==='auction/sale/start').length,1);
  assert.equal(p.calls.some(c=>c.path.includes('reprice')),false);
  await p.click('auction-reprice-close');await p.reprice();
  assert.equal(p.node('auction-reprice-title').textContent,'下架并修改价格重新上架');
});

test('manual sale retains edited price through retry and handles empty books without stale prices',async()=>{
  const p=page();let finish;
  p.setPreview(()=>new Promise(resolve=>{finish=resolve;}));
  const loading=p.window.AuctionTrackingUI.openSale('inventory-123');await flush();
  p.node('auction-reprice-price').value='13.25';
  finish({token:'s'.repeat(43),currency:'CNY',lowest_price:'CNY 13.11',highest_price:'CNY 12.90',default_price:'13.11'});
  await loading;assert.equal(p.node('auction-reprice-price').value,'13.25');
  p.setPreview(()=>({token:'e'.repeat(43),currency:'CNY',lowest_price:null,highest_price:null,default_price:''}));
  await p.click('auction-reprice-retry');
  assert.equal(p.node('auction-reprice-price').value,'13.25');
  assert.match(p.node('auction-reprice-note').textContent,/暂无在售.*暂无求购/);
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/preview').length,2);
});

test('a missing manual quote never blocks typing; a bounded local poll fills the later ask and bid',async()=>{
  const p=page();p.setPreview(()=>({token:'s'.repeat(43),currency:'CNY',quote_pending:true,quote_known:false,default_price:''}));
  await p.window.AuctionTrackingUI.openSale('inventory-123');
  assert.equal(p.node('auction-reprice-submit').disabled,false);
  assert.match(p.node('auction-reprice-note').textContent,/后台读取中，可先手填/);
  assert.equal(p.calls.some(c=>c.path.endsWith('/start')),false);
  p.setPreview(()=>({token:'n'.repeat(43),currency:'CNY',quote_pending:false,quote_known:true,
    lowest_price:'CNY 13.11',highest_price:'CNY 12.90',default_price:'13.11'}));
  await p.poll();
  assert.equal(p.node('auction-reprice-price').value,'13.11');
  assert.match(p.node('auction-reprice-note').textContent,/13.11.*12.90/);
  await p.poll();assert.equal(p.calls.filter(c=>c.path==='auction/sale/preview').length,2);
});

test('background quote refresh never overwrites typed or intentionally cleared prices',async()=>{
  for(const typed of ['14.25','']){
    const p=page();p.setPreview(()=>({token:'s'.repeat(43),currency:'CNY',quote_pending:true,quote_known:false,default_price:''}));
    await p.window.AuctionTrackingUI.openSale('inventory-123');
    p.node('auction-reprice-price').value=typed;await p.node('auction-reprice-price').emit('input');
    p.setPreview(()=>({token:'n'.repeat(43),currency:'CNY',default_price:'13.11',quote_known:true}));
    await p.poll();assert.equal(p.node('auction-reprice-price').value,typed);
  }
});

test('unavailable quotes stop polling and show a finished state while allowing explicit manual pricing',async()=>{
  const p=page();p.setPreview(()=>({token:'s'.repeat(43),currency:'CNY',quote_pending:true,quote_known:false,default_price:''}));
  await p.window.AuctionTrackingUI.openSale('inventory-123');
  for(let i=0;i<15;i++)await p.poll();
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/preview').length,11);
  assert.match(p.node('auction-reprice-note').textContent,/暂不可读，可手填/);
  assert.match(p.node('auction-reprice-error').textContent,/行情暂未读到/);
  assert.equal(p.node('auction-reprice-submit').disabled,false);
  p.node('auction-reprice-price').value='13.25';await p.click('auction-reprice-submit');
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/start').length,1);
});

test('preview failure replaces the loading message and closing or submitting stops quote polling',async()=>{
  const p=page();p.setPreview(()=>{throw Error('正在处理上一笔 Steam 操作');});
  await p.window.AuctionTrackingUI.openSale('inventory-123');
  assert.match(p.node('auction-reprice-note').textContent,/读取未完成/);
  assert.doesNotMatch(p.node('auction-reprice-note').textContent,/正在/);
  assert.equal(p.node('auction-reprice-retry').disabled,false);
  p.setPreview(()=>({token:'s'.repeat(43),currency:'CNY',default_price:'13.11',quote_pending:true}));
  await p.click('auction-reprice-retry');await p.click('auction-reprice-close');await p.poll();
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/preview').length,2);
  await p.window.AuctionTrackingUI.openSale('inventory-123');
  await p.click('auction-reprice-submit');await p.poll();
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/preview').length,3);
  assert.equal(p.calls.filter(c=>c.path==='auction/sale/start').length,1);
});

test('shared renderer covers five stages with wallet price and actual proceeds and escapes all text',()=>{
  const markup=rows.map(helpers.rowMarkup).join('');
  for(const text of ['待上架','已上架拍卖行','Steam 市场待确认','Steam 市场已上架','已售出','CNY 0.23','CNY 0.21','CNY 1.07'])assert.ok(markup.includes(text),text);
  assert.equal((markup.match(/data-reprice-id=/g)||[]).length,1);
  const unsafe=helpers.rowMarkup({...rows[3],name:'<img onerror=alert(1)>',message:'<script>',my_price:'<script>',tracking_id:'"><img'});
  assert.equal(unsafe.includes('<img'),false);assert.equal(unsafe.includes('<script>'),false);assert.equal(unsafe.includes('data-reprice-id='),false);
});

test('nonautomatic items are last in a closed details group, and the expanded choice can be retained',()=>{
  const items=[{name:'保留符文',status:'waiting_town',auto_eligible:false},...rows.slice(0,4)];
  const markup=helpers.listMarkup(items);
  assert.ok(markup.indexOf('Steam 市场已上架')<markup.indexOf('<details'));
  assert.match(markup,/<details[^>]*data-tracking-excluded>/);
  assert.match(markup,/未满足自动上架条件（1 件）/);
  assert.ok(markup.indexOf('保留符文')>markup.indexOf('<summary>'));
  assert.match(helpers.listMarkup(items,true),/data-tracking-excluded open>/);
  assert.equal(helpers.listMarkup(rows).includes('<details'),false);
});

test('opening and polling managed tracking only read local state and never enable automation',async()=>{
  const p=page();assert.equal(p.calls.length,0);await p.click('managed-auction-tracking-open');await p.poll();
  assert.equal(p.node('managed-auction-tracking-dialog').open,true);
  assert.match(p.node('managed-auction-tracking-items').innerHTML,/CNY 1.07/);
  assert.equal(p.calls.every(call=>call.path==='auction/managed/state'&&call.data===undefined),true);
});

test('native refresh is explicit and duplicate clicks are blocked while the request is pending',async()=>{
  const p=page();await p.click('managed-auction-tracking-open');
  let finish;p.setNativeRefresh(()=>new Promise(resolve=>{finish=resolve;}));
  const first=p.click('managed-auction-native-refresh');await flush();
  assert.equal(p.node('managed-auction-native-refresh').disabled,true);
  await p.click('managed-auction-native-refresh');await p.poll();
  assert.equal(p.calls.filter(c=>c.path==='auction/tracking/refresh').length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(p.calls.find(c=>c.path==='auction/tracking/refresh').data)),{});
  finish({message:'刷新请求已提交'});await first;
  assert.equal(p.node('managed-auction-native-refresh').disabled,false);
  p.setNativeRefresh(()=>{throw Error('请先暂停托管，再刷新拍卖行。');});
  await p.click('managed-auction-native-refresh');
  assert.match(p.node('managed-auction-tracking-error').textContent,/先暂停托管/);
  await p.poll();assert.match(p.node('managed-auction-tracking-error').textContent,/先暂停托管/);
});

test('excluded group remains expanded through polling and is separate in both entry points',async()=>{
  const p=page();p.setState({items:[{name:'保留符文',status:'waiting_town',auto_eligible:false},rows[3]]});
  await p.click('inventory-trade-tracking-open');
  const element=p.node('managed-auction-tracking-items');
  assert.match(element.innerHTML,/data-tracking-excluded>/);
  await element.emit('toggle',{target:{parentNode:element,open:true,matches:()=>true}});
  await p.poll();assert.match(element.innerHTML,/data-tracking-excluded open>/);
});

test('failed refresh invalidates live pending and listed statuses while retaining proven sales',async()=>{
  const p=page();await p.click('managed-auction-tracking-open');p.setFailure(Error('unavailable'));await p.poll();
  const markup=p.node('managed-auction-tracking-items').innerHTML;
  assert.equal(markup.includes('data-reprice-id='),false);assert.match(markup,/Steam 市场待确认 · 待核验/);
  assert.match(markup,/Steam 市场已上架 · 待核验/);assert.match(markup,/CNY 1.07/);assert.match(p.node('managed-auction-tracking-error').textContent,/读取失败/);
});

test('repricing opens a read-only exact-listing preview, defaults to current ask and waits for explicit submit',async()=>{
  const p=page();await p.reprice();
  assert.equal(p.node('auction-reprice-dialog').open,true);assert.equal(p.node('auction-reprice-price').value,'0.21');
  assert.deepEqual(p.calls.filter(c=>c.path==='auction/reprice/preview').map(c=>JSON.parse(JSON.stringify(c.data))),[{tracking_id:'a'.repeat(32)}]);
  assert.equal(p.calls.some(c=>c.path==='auction/reprice/start'),false);
  assert.match(p.node('auction-reprice-note').textContent,/钱包币种 CNY/);
  p.node('auction-reprice-price').value='0.24';await p.click('auction-reprice-submit');
  assert.deepEqual(JSON.parse(JSON.stringify(p.calls.find(c=>c.path==='auction/reprice/start').data)),{token:'t'.repeat(43),price:'0.24'});
  await p.click('auction-reprice-submit');assert.equal(p.calls.filter(c=>c.path==='auction/reprice/start').length,1);
});

test('invalid price and duplicate clicks while submission is in flight never produce extra writes',async()=>{
  const p=page();await p.reprice();p.node('auction-reprice-price').value='0';await p.click('auction-reprice-submit');
  assert.equal(p.calls.some(c=>c.path==='auction/reprice/start'),false);
  let finish;p.setStart(()=>new Promise(resolve=>{finish=resolve;}));p.node('auction-reprice-price').value='0.23';
  const first=p.click('auction-reprice-submit');await flush();await p.click('auction-reprice-submit');
  assert.equal(p.calls.filter(c=>c.path==='auction/reprice/start').length,1);
  finish({running:true});await first;
});

test('manual inventory dialog reuses identical shared item rendering and repricing controls',async()=>{
  const p=page();vm.runInContext(fs.readFileSync('deskrawl_assistant/web/inventory-auction.js','utf8'),p.context);
  p.window.InventoryAuctionUI.init({api:p.api,onRefresh:async()=>{}});await flush();await p.click('inventory-auction-details');
  assert.equal(p.node('inventory-auction-items').innerHTML,rows.map(helpers.rowMarkup).join(''));
  assert.match(p.node('inventory-auction-items').innerHTML,/下架并修改价格重新上架/);
});

test('production loads tracking before app init and exposes managed list and a separate explicit-price confirmation',()=>{
  const html=fs.readFileSync('deskrawl_assistant/web/index.html','utf8');
  assert.ok(html.indexOf('/auction-tracking.js')<html.indexOf('/app.js'));
  for(const id of ['managed-auction-tracking-open','managed-auction-tracking-dialog','auction-reprice-dialog','auction-reprice-price','auction-reprice-submit'])assert.match(html,new RegExp(`id="${id}"`));
  assert.match(fs.readFileSync('deskrawl_assistant/web/auction-ui.js','utf8'),/AuctionTrackingUI\?\.init\(config\)/);
});


test('inventory pending entry opens the same account-wide dialog independently of a selected batch',async()=>{
  const p=page();await p.click('inventory-trade-tracking-open');
  assert.equal(p.node('managed-auction-tracking-dialog').open,true);
  assert.equal(p.calls[0].path,'auction/managed/state');
});

test('a backend uncertain result preserves the previous stage and price but disables repricing',async()=>{
  const p=page();p.setState({items:[{...rows[3],id:'old'}]});await p.click('managed-auction-tracking-open');
  p.setState({items:[{id:'old',name:'在售商品',status:'uncertain'}]});await p.poll();
  const markup=p.node('managed-auction-tracking-items').innerHTML;
  assert.match(markup,/Steam 市场已上架 · 待核验/);assert.match(markup,/CNY 0.23/);
  assert.equal(markup.includes('data-reprice-id='),false);
});

test('a stale listed order can open a fresh readonly preview without granting a sale',async()=>{
  const p=page();p.setState({items:[{...rows[3],status:'uncertain',previous_status:'listed',
    needs_verification:true,can_reprice:false,reprice_available:true,lowest_price_cached:true}]});
  await p.click('managed-auction-tracking-open');
  const markup=p.node('managed-auction-tracking-items').innerHTML;
  assert.match(markup,/Steam 市场已上架 · 待核验/);assert.match(markup,/CNY 0.21（缓存）/);
  assert.match(markup,/data-reprice-id=/);
  await p.window.AuctionTrackingUI.openReprice('a'.repeat(32));
  assert.equal(p.node('auction-reprice-dialog').open,true);
  assert.equal(p.calls.filter(c=>c.path==='auction/reprice/preview').length,1);
  assert.equal(p.calls.some(c=>c.path==='auction/reprice/start'),false);
});

test('tray uses the same price editor and rejects invalid identifiers before any request',async()=>{
  const p=page();
  for(const value of ['',null,'bad-id','a'.repeat(33)])await p.window.AuctionTrackingUI.openReprice(value);
  assert.equal(p.calls.length,0);
  await p.window.AuctionTrackingUI.openReprice('a'.repeat(32));
  assert.equal(p.node('auction-reprice-price').value,'0.21');
  assert.equal(p.node('auction-reprice-submit').disabled,false);
  assert.equal(p.calls.some(c=>c.path==='auction/reprice/start'),false);
  p.node('auction-reprice-price').value='0.25';await p.click('auction-reprice-submit');
  assert.equal(p.calls.filter(c=>c.path==='auction/reprice/start').length,1);
});

test('a failed local poll retains only a bounded cached quote and a readonly entry',()=>{
  const item={...rows[3],reprice_available:true,lowest_price_expires_at:Date.now()/1000+120};
  const fresh=helpers.invalidateLive([item])[0];
  assert.equal(fresh.lowest_price,'CNY 0.21');assert.equal(fresh.lowest_price_cached,true);
  assert.equal(fresh.can_reprice,false);assert.equal(fresh.reprice_available,true);
  const expired=helpers.invalidateLive([{...item,lowest_price_expires_at:Date.now()/1000-1}])[0];
  assert.equal(expired.lowest_price,null);
});

test('cached price is labelled and an unavailable quote does not prevent manual price entry',async()=>{
  const p=page();p.setPreview(()=>({token:'c'.repeat(43),currency:'CNY',my_price:'CNY 0.23',
    lowest_price:'CNY 0.21',lowest_price_cached:true,default_price:'0.21'}));
  await p.reprice();assert.match(p.node('auction-reprice-note').textContent,/CNY 0.21（缓存）/);
  await p.click('auction-reprice-close');
  p.setPreview(()=>({token:'m'.repeat(43),currency:'CNY',default_price:'',lowest_price:null}));
  await p.reprice();assert.match(p.node('auction-reprice-note').textContent,/可手填/);
  assert.equal(p.node('auction-reprice-submit').disabled,false);
  p.node('auction-reprice-price').value='0.25';await p.click('auction-reprice-submit');
  assert.equal(p.calls.find(c=>c.path==='auction/reprice/start').data.price,'0.25');
});

test('preview timeout offers retry without writes, retaining typed price and ignoring a late response',async()=>{
  const p=page();let finish;
  p.setPreview(()=>new Promise(resolve=>{finish=resolve;}));
  const first=p.window.AuctionTrackingUI.openReprice('a'.repeat(32));await flush();
  p.node('auction-reprice-price').value='0.27';
  await p.expireReads();await first;
  assert.match(p.node('auction-reprice-error').textContent,/读取超时/);
  assert.equal(p.node('auction-reprice-submit').disabled,true);
  assert.equal(p.node('auction-reprice-retry').disabled,false);
  p.setPreview(()=>({token:'n'.repeat(43),currency:'CNY',default_price:'0.21'}));
  await p.click('auction-reprice-retry');
  assert.equal(p.node('auction-reprice-price').value,'0.27');
  assert.equal(p.node('auction-reprice-submit').disabled,false);
  finish({token:'old'.repeat(14),currency:'USD',default_price:'9.99'});await flush();
  assert.equal(p.node('auction-reprice-price').value,'0.27');
  assert.match(p.node('auction-reprice-note').textContent,/钱包币种 CNY/);
  assert.equal(p.calls.some(c=>c.path==='auction/reprice/start'),false);
});

test('closing a loading editor permits reopening and discards the old response',async()=>{
  const p=page();let finish;
  p.setPreview(()=>new Promise(resolve=>{finish=resolve;}));
  const old=p.window.AuctionTrackingUI.openReprice('a'.repeat(32));await flush();
  await p.click('auction-reprice-close');
  p.setPreview(()=>({token:'n'.repeat(43),currency:'CNY',default_price:'0.22'}));
  await p.window.AuctionTrackingUI.openReprice('b'.repeat(32));
  finish({token:'o'.repeat(43),currency:'USD',default_price:'8.88'});await old;
  assert.equal(p.node('auction-reprice-price').value,'0.22');
  p.node('auction-reprice-price').value='0.26';await p.click('auction-reprice-submit');
  assert.equal(p.calls.find(c=>c.path==='auction/reprice/start').data.token,'n'.repeat(43));
});

test('finishing a local read does not replace a price edited while it loaded',async()=>{
  const p=page();let finish;
  p.setPreview(()=>new Promise(resolve=>{finish=resolve;}));
  const pending=p.window.AuctionTrackingUI.openReprice('a'.repeat(32));await flush();
  p.node('auction-reprice-price').value='0.29';
  finish({token:'n'.repeat(43),currency:'CNY',default_price:'0.21'});await pending;
  assert.equal(p.node('auction-reprice-price').value,'0.29');
  assert.equal(p.calls.some(c=>c.path==='auction/reprice/start'),false);
});
