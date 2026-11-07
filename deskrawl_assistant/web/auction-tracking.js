'use strict';
(function(root){
  const names={waiting_town:'待上架',ready:'可处理',skipped:'已跳过',queued:'等待处理',running:'处理中',uploaded:'已上架拍卖行 · 未上架 Steam',pending_confirmation:'Steam 市场待确认',listed:'Steam 市场已上架',sold:'已售出',uncertain:'结果待核验',failed:'未完成',cancelled:'已取消'};
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // Local guidance only. The server rechecks live wallet/item fees before
  // any cancellation or sale. Integer ratios avoid floating-point cent errors.
  function priceGuidance(value,p){
    if(!p||!/^\d{1,10}(?:\.\d{1,2})?$/.test(value)||!(Number(value)>0))return null;
    if(!['minimum','increment','maximum'].every(k=>Number.isSafeInteger(p[k])&&p[k]>0&&p[k]<=1e12)||p.maximum<p.minimum)return null;
    const rates=[p.steam,p.publisher].map(rate=>{
      if(typeof rate!=='string'||! /^(?:0(?:\.\d{1,28})?|1(?:\.0{1,28})?)$/.test(rate))return null;
      const [whole,fraction='']=rate.split('.');return [BigInt(whole+fraction),10n**BigInt(fraction.length)];
    });
    if(rates.some(r=>!r))return null;
    const [whole,fraction='']=value.split('.'),total=Number(whole)*100+Number(fraction.padEnd(2,'0'));
    if(total>p.maximum)return {valid:false,options:[],message:'价格超过 Steam 钱包交易上限。'};
    const round=n=>n<=p.minimum?p.minimum:n<=p.increment?p.increment:Math.floor((n*2+p.increment)/(p.increment*2))*p.increment;
    const buyer=n=>round(n)+rates.reduce((sum,[rate,den])=>sum+(rate?round(Number(BigInt(n)*rate/den)):0),0);
    let low=0,high=Math.floor(total/p.increment)+1;
    while(low<high){const mid=Math.floor((low+high)/2);if(buyer(Math.max(p.minimum,mid*p.increment))<total)low=mid+1;else high=mid;}
    const candidates=[...new Set([p.minimum,low*p.increment,Math.max(p.minimum,(low-1)*p.increment)])]
      .filter(n=>n>0&&round(n)===n).map(buyer).filter(n=>n<=p.maximum).sort((a,b)=>a-b);
    if(candidates.includes(total))return {valid:true,options:[]};
    const below=candidates.filter(n=>n<total).pop(),above=candidates.find(n=>n>total);
    return {valid:false,options:[below,above].filter(n=>n!==undefined).map(n=>(n/100).toFixed(2)),
      message:'此总价不符合 Steam 手续费取整规则，请选择附近可用总价或修改输入：'};
  }
  function rowMarkup(row){
    const status=row.status||'queued',previous=names[row.previous_status]?row.previous_status:null;
    const display=status==='uncertain'&&previous?previous:status,position={inventory:'背包',storage:'仓库'}[row.container]||'';
    const unverified=status==='uncertain'||row.needs_verification===true;
    const quantity=Number.isInteger(row.count)&&row.count>0?row.count:1;
    const prices=display==='listed'?`<small class="auction-tracking-prices">我的价格：${esc(row.my_price||'待核验')}　当前在售底价：${esc(row.lowest_price||'暂不可读')}${row.lowest_price&&row.lowest_price_cached?'（缓存）':''}</small>`:'';
    const proceeds=status==='sold'?`<small class="auction-tracking-proceeds">收获 Steam 余额：${esc(row.proceeds||'实际到账金额待核验')}</small>`:'';
    const button=display==='listed'&&(row.reprice_available||status==='listed'&&!unverified&&row.can_reprice)&&/^[a-f0-9]{32}$/.test(row.tracking_id||'')?`<button class="button ghost auction-reprice-button" type="button" data-reprice-id="${esc(row.tracking_id)}">下架并修改价格重新上架</button>`:display==='uploaded'&&(row.sale_preview_available||status==='uploaded'&&!unverified&&row.sale_available)&&/^(?:[a-f0-9]{32}|inventory-[1-9]\d{0,24})$/.test(row.id||'')?`<button class="button ghost auction-reprice-button" type="button" data-sale-id="${esc(row.id)}">定价上架 Steam</button>`:'';
    return `<div class="inventory-auction-row" data-status="${esc(status)}"><div><strong>${esc(row.name||row.item_key||'未识别物品')}</strong><small>${esc(position)}${position?' · ':''}数量 ${quantity}${row.message||row.reason?' · '+esc(row.message||row.reason):''}</small>${prices}${proceeds}${button}</div><span class="inventory-auction-row-status">${esc(names[display]||'等待核验')}${unverified&&display!=='uncertain'?' · 待核验':''}</span></div>`;
  }
  function invalidateLive(rows){return (rows||[]).map(row=>!['sold','failed','cancelled','skipped'].includes(row.status)?{...row,previous_status:row.status==='uncertain'?row.previous_status:row.status,status:'uncertain',needs_verification:true,can_reprice:false,lowest_price:Date.now()/1000<row.lowest_price_expires_at?row.lowest_price:null,lowest_price_cached:true,message:'当前状态查询失败；保留上次状态，待核验，不会重复提交。'}:row);}
  function listMarkup(rows,expanded=false){
    const active=rows.filter(row=>row.auto_eligible!==false),other=rows.filter(row=>row.auto_eligible===false);
    return active.map(rowMarkup).join('')+(other.length?`<details class="auction-tracking-excluded" data-tracking-excluded${expanded?' open':''}><summary>未满足自动上架条件（${other.length} 件）<small>点击展开查看，不会因此启用自动上架</small></summary>${other.map(rowMarkup).join('')}</details>`:'');
  }
  const helpers={rowMarkup,listMarkup,names,invalidateLive,priceGuidance};
  if(typeof module!=='undefined'&&module.exports)module.exports=helpers;
  if(!root.document)return;
  const $=id=>root.document.getElementById(id);
  let options,initialized=false,loading=false,polling=false,preview=null,submitting=false,latest=[],excludedOpen=false,nativeError='',editorId=null,editorVersion=0,editorMode='reprice',quotePolls=0,priceEdited=false,watchedJob=null;
  function showJob(job){
    $('auction-reprice-note').textContent=job.message||(job.running?'任务已受理，正在核验；尚未确认提交。':'操作已结束，请核验物品列表。');
    if(job.error){$('auction-reprice-error').textContent=job.message;$('auction-reprice-price').disabled=false;}
    if(!job.running&&!job.waiting)watchedJob=null;
  }
  function updatePriceGuidance(){
    const guidance=priceGuidance($('auction-reprice-price').value.trim(),preview?.fee_parameters);
    $('auction-reprice-price-note').innerHTML=guidance&&!guidance.valid?esc(guidance.message)+guidance.options.map(price=>` <button class="text-button" type="button" data-price-option="${price}">${esc(preview.currency||'')} ${price}</button>`).join(''):'';
    $('auction-reprice-submit').disabled=loading||submitting||!preview?.token||guidance?.valid===false;
  }
  async function refresh(){
    if(polling)return;polling=true;
    try{
      const value=await options.api('auction/managed/state');
      if(watchedJob&&value.reprice?.job_id===watchedJob&&$('auction-reprice-dialog').open)showJob(value.reprice);
      const previous=new Map(latest.map(row=>[row.id,row]));
      latest=(value.items||[]).map(row=>{const old=previous.get(row.id);return row.status==='uncertain'&&!row.previous_status&&old?{...row,previous_status:old.status==='uncertain'?old.previous_status:old.status,my_price:row.my_price||old.my_price,can_reprice:false}:row;});
      $('managed-auction-tracking-summary').textContent=(value.message||'')+(value.reprice?.message?' '+value.reprice.message:'');
      const markup=latest.length?listMarkup(latest,excludedOpen):'<div class="empty-state">当前暂无待交易物品。</div>';
      if($('managed-auction-tracking-items').innerHTML!==markup)$('managed-auction-tracking-items').innerHTML=markup;
      $('managed-auction-tracking-error').textContent=nativeError;
    }catch(error){latest=invalidateLive(latest);$('managed-auction-tracking-items').innerHTML=listMarkup(latest,excludedOpen);$('managed-auction-tracking-error').textContent='状态读取失败，请稍后刷新；不要重复提交。'+error.message;}
    finally{polling=false;}
  }
  function openReprice(id,keepPrice=false){return openPrice('reprice',id,keepPrice);}
  function openSale(id,keepPrice=false){return openPrice('sale',id,keepPrice);}
  async function openPrice(mode,id,keepPrice=false,background=false){
    const sale=mode==='sale',valid=sale?/^(?:[a-f0-9]{32}|inventory-[1-9]\d{0,24})$/:/^[a-f0-9]{32}$/;
    if(!initialized||!valid.test(id||'')||loading||submitting)return;
    const previousPreview=preview;
    if(!background){watchedJob=null;quotePolls=0;if(!keepPrice)priceEdited=false;}
    loading=true;preview=null;
    editorId=id;editorMode=mode;const version=++editorVersion;
    $('auction-reprice-eyebrow').textContent=sale?'库存商品 · 确认上架':'在售卖单 · 确认改价';
    $('auction-reprice-title').textContent=sale?'定价上架 Steam 市场':'下架并修改价格重新上架';
    $('auction-reprice-submit').textContent=sale?'确认按此价格上架':'确认下架并按此价格重挂';
    $('auction-reprice-help').textContent=sale?'确认后核验所选物品、登录和钱包，再按填写价格上架。可能需要在 Steam 手机应用确认；不会替换成其他同名商品。':'确认后实时核验原卖单、登录和钱包，核验通过才撤单；核验物品退回后按新价格重新挂牌。重新挂牌可能需要再次在 Steam 手机应用确认；不会自动降低价格。';
    $('auction-reprice-scope').textContent=sale?'仅上架当前选中的 1 件商品。':'仅处理当前选中的这一笔卖单。';
    $('auction-reprice-price').disabled=false;
    if(!keepPrice)$('auction-reprice-price').value='';
    const initialPrice=$('auction-reprice-price').value;
    $('auction-reprice-error').textContent='';
    $('auction-reprice-price-note').innerHTML='';
    if(!background)$('auction-reprice-note').textContent=sale?'正在读取商品与价格记录；确认后才实时核验并上架。':'正在读取最近的卖单价格；确认后才实时核验并下架。';
    $('auction-reprice-submit').disabled=true;
    $('auction-reprice-retry').disabled=true;
    if(!$('auction-reprice-dialog').open)$('auction-reprice-dialog').showModal();
    let timeout;
    try{
      const result=await Promise.race([options.api(`auction/${mode}/preview`,sale?{item_id:id}:{tracking_id:id}),
        new Promise((_,reject)=>{timeout=root.setTimeout(()=>reject(Error('价格读取超时，请重新读取；尚未下架或出售。')),8000);})]);
      if(version!==editorVersion)return;
      preview=result;
      // Loading/retrying must never overwrite a price the player is editing.
      if((!keepPrice||background&&!initialPrice)&&!priceEdited&&$('auction-reprice-price').value===initialPrice)$('auction-reprice-price').value=preview.default_price||'';
      const cached=preview.lowest_price&&preview.lowest_price_cached?'（缓存）':'';
      if(!background)quotePolls=preview.quote_pending?10:0;
      const waiting=preview.quote_pending&&quotePolls>0;
      const missing=waiting?'后台读取中，可先手填':preview.quote_known===false?'暂不可读，可手填':'暂无在售，可手填';
      $('auction-reprice-note').textContent=sale?`钱包币种 ${preview.currency||''} · 在售最低价 ${preview.lowest_price||missing}${cached} · 求购最高价 ${preview.highest_price||(waiting?'后台读取中':preview.quote_known===false?'暂不可读':'暂无求购')}。以下价格为买方含手续费总价。${preview.message||''}`:`钱包币种 ${preview.currency||''} · 我的价格 ${preview.my_price||'待核验'} · 在售底价 ${preview.lowest_price||missing}${cached}。以下价格为买方含手续费总价。${preview.message||''}`;
      $('auction-reprice-error').textContent=preview.quote_error||(preview.quote_pending&&!waiting?'行情暂未读到，可先手填价格，或稍后重新读取。':'');
      if(!preview.quote_pending)quotePolls=0;
      $('auction-reprice-submit').disabled=false;
    }catch(error){if(version===editorVersion){
      quotePolls=0;
      if(background&&previousPreview?.token){preview=previousPreview;$('auction-reprice-submit').disabled=false;}
      $('auction-reprice-note').textContent='本次价格读取未完成，可稍后重新读取；尚未下架或出售。';
      $('auction-reprice-error').textContent=error.message;
    }}
    finally{root.clearTimeout(timeout);if(version===editorVersion){loading=false;$('auction-reprice-retry').disabled=false;updatePriceGuidance();}}
  }
  function bindList(id){
    const element=$(id);if(!element||element.dataset.trackingBound)return;
    element.dataset.trackingBound='true';
    element.addEventListener('click',event=>{const button=event.target.closest('[data-reprice-id],[data-sale-id]');if(button&&!button.disabled){if(button.dataset.saleId)openSale(button.dataset.saleId);else openReprice(button.dataset.repriceId);}});
    if(id==='managed-auction-tracking-items')element.addEventListener('toggle',event=>{if(event.target.parentNode===element&&event.target.matches?.('[data-tracking-excluded]'))excludedOpen=event.target.open;},true);
  }
  root.AuctionTrackingUI={helpers,bindList,openReprice,openSale,init(config){
    if(initialized)return;initialized=true;options=config;
    for(const id of ['managed-auction-tracking-open','inventory-trade-tracking-open'])$(id)?.addEventListener('click',()=>{if(!$('managed-auction-tracking-dialog').open)$('managed-auction-tracking-dialog').showModal();refresh();});
    $('managed-auction-tracking-close').addEventListener('click',()=>$('managed-auction-tracking-dialog').close());
    $('managed-auction-tracking-refresh').addEventListener('click',refresh);
    $('managed-auction-native-refresh')?.addEventListener('click',async()=>{
      const button=$('managed-auction-native-refresh');if(button.disabled)return;button.disabled=true;nativeError='';
      try{const value=await options.api('auction/tracking/refresh',{});$('managed-auction-tracking-summary').textContent=value.message||'正在刷新拍卖行当前列表…';}
      catch(error){nativeError=error.message;$('managed-auction-tracking-error').textContent=nativeError;}
      finally{button.disabled=false;}
    });
    $('auction-reprice-close').addEventListener('click',()=>{if(!submitting)$('auction-reprice-dialog').close();});
    $('auction-reprice-retry').addEventListener('click',()=>openPrice(editorMode,editorId,Boolean($('auction-reprice-price').value.trim())));
    $('auction-reprice-price').addEventListener('input',()=>{priceEdited=true;updatePriceGuidance();});
    $('auction-reprice-price-note').addEventListener('click',event=>{
      const button=event.target.closest('[data-price-option]');
      if(!button||loading||submitting||!preview?.token)return;
      const guidance=priceGuidance($('auction-reprice-price').value.trim(),preview.fee_parameters);
      if(!guidance?.options.includes(button.dataset.priceOption))return;
      $('auction-reprice-price').value=button.dataset.priceOption;priceEdited=true;updatePriceGuidance();
    });
    $('auction-reprice-submit').addEventListener('click',async()=>{
      if(!preview?.token||submitting||loading)return;
      const price=$('auction-reprice-price').value.trim();
      if(!/^\d{1,10}(?:\.\d{1,2})?$/.test(price)||!(Number(price)>0)){$('auction-reprice-error').textContent='请输入正数价格，最多两位小数。';return;}
      if(priceGuidance(price,preview.fee_parameters)?.valid===false){updatePriceGuidance();return;}
      submitting=true;quotePolls=0;$('auction-reprice-submit').disabled=true;$('auction-reprice-retry').disabled=true;
      $('auction-reprice-error').textContent='';
      const sale=editorMode==='sale';
      $('auction-reprice-note').textContent=sale?'正在核验商品、登录和钱包；通过后按填写价格上架。':'正在实时核验原卖单、登录和钱包；核验通过后才下架并按填写价格重挂。';
      try{
        const job=await options.api(`auction/${editorMode}/start`,{token:preview.token,price});preview=null;
        watchedJob=job.job_id||null;$('auction-reprice-price').disabled=true;showJob(job);refresh();
      }catch(error){$('auction-reprice-error').textContent=error.message+' 如请求结果不明，请先核验市场记录，不要重复提交。';preview=null;}
      finally{submitting=false;$('auction-reprice-retry').disabled=false;}
    });
    $('auction-reprice-dialog').addEventListener('cancel',event=>{if(submitting)event.preventDefault();});
    $('auction-reprice-dialog').addEventListener('close',()=>{++editorVersion;editorId=null;loading=false;preview=null;quotePolls=0;watchedJob=null;$('auction-reprice-price').disabled=false;});
    bindList('managed-auction-tracking-items');bindList('inventory-auction-items');
    root.setInterval(()=>{
      if($('managed-auction-tracking-dialog').open||watchedJob)refresh();
      if($('auction-reprice-dialog').open&&quotePolls>0&&!loading&&!submitting){--quotePolls;openPrice(editorMode,editorId,true,true);}
    },2000);
  }};
})(typeof window!=='undefined'?window:globalThis);
