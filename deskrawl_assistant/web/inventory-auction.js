'use strict';
(function(root){
  const statusNames={ready:'可处理',skipped:'已跳过',queued:'等待处理',running:'处理中',uploaded:'已上传交易所',listed:'已上架 Steam',pending_confirmation:'待 Steam 确认',failed:'未完成',uncertain:'结果待核验',cancelled:'已取消'};
  const priceNames={lowest_sell:'最低在售价',highest_buy:'最高求购价'};
  const cleanIds=rows=>[...new Set((Array.isArray(rows)?rows:[]).map(row=>row?.selection_id).filter(id=>typeof id==='string'&&id))];
  const unwrap=value=>value?.state||value||{};
  function availability(state={},rows=[],batch={}){
    if(batch.running)return '本次批量操作正在进行，请先查看进度。';
    if(!state.connected)return '请先连接游戏。';
    if(state.managed?.running||state.managed?.finishing)return '请先停止托管，再手动上架所选商品。';
    if(state.busy)return '当前操作正在进行，请稍后再试。';
    if(!state.complete)return '物品尚未读取完整，请先刷新物品。';
    if(state.manual_auction_availability?.available!==true)return state.manual_auction_availability?.reason||'当前游戏状态尚未确认，请等待重新读取。';
    return cleanIds(rows).length?'':'请先勾选背包或仓库中的商品。';
  }
  function createController({api,onChange=()=>{},onRefresh=()=>{}}){
    let preview=null,batch={},busy=false,error='',epoch=0,polling=false,uncertainStart=false,previousBatchId='',started=false;
    const batchIdentity=value=>String(value?.batch_id||value?.id||value?.started_at||'');
    const snapshot=()=>({preview,batch,busy,error,uncertainStart,started});
    const emit=()=>onChange(snapshot());
    const controller={get value(){return snapshot();},clearPreview(){epoch++;preview=null;error='';uncertainStart=false;started=false;emit();},
      async preview(request){
        if(busy)return;const ticket=++epoch;busy=true;preview=null;error='';uncertainStart=false;started=false;emit();
        try{const result=await api('auction/manual/preview',request);if(ticket===epoch)preview=result;}
        catch(cause){if(ticket===epoch)error=cause.message;}
        finally{busy=false;emit();}
      },
      async start(){
        if(busy||batch.running||!preview?.token||preview.can_start===false||!(preview.ready_count>0)||uncertainStart)return;
        const token=preview.token;previousBatchId=batchIdentity(batch);batch={};preview={...preview,token:null};busy=true;error='';++epoch;emit();
        try{batch=unwrap(await api('auction/manual/start',{token}));preview=null;started=true;}
        catch(cause){error=cause.message;uncertainStart=!(cause.status>=400&&cause.status<500);}
        finally{busy=false;emit();}
        await controller.refresh();await onRefresh();
      },
      async refresh(){
        if(polling||busy)return;polling=true;const ticket=epoch;
        try{const result=unwrap(await api('auction/manual/state'));if(ticket===epoch){
          const newBatch=result.running||!!batchIdentity(result)&&batchIdentity(result)!==previousBatchId;
          if(!uncertainStart||newBatch){batch=result;if(batch.running||uncertainStart){started=true;uncertainStart=false;preview=null;}}
          emit();}}
        catch(cause){if(ticket===epoch&&(batch.running||uncertainStart)){error='进度暂时读取失败，请稍后重新核验；不要重复提交。'+cause.message;emit();}}
        finally{polling=false;}
      },
      async cancel(){
        if(busy||!batch.running||batch.can_cancel===false)return;busy=true;error='';++epoch;emit();
        try{batch=unwrap(await api('auction/manual/cancel',{}));}
        catch(cause){error=cause.message;}
        finally{busy=false;emit();}
        await controller.refresh();
      }};
    return controller;
  }
  const helpers={statusNames,priceNames,cleanIds,availability,createController};
  if(typeof module!=='undefined'&&module.exports)module.exports=helpers;
  if(!root.document)return;
  const $=id=>root.document.getElementById(id),esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let options,controller,game={},selection=[],chosen=[],mode='upload',editing=false,active=false,initialized=false,steam={},steamRequest=null,submitting=false,committing=false,steamNotice='',loginTipRequested=false;
  function updateButtons(){
    const reason=availability(game,selection,controller?.value.batch),busy=controller?.value.busy;
    $('inventory-auction-upload').disabled=!!reason||!!busy||submitting;$('inventory-auction-upload').title=reason||'核验并确认本次勾选的商品。';
    $('inventory-auction-availability').hidden=!reason;$('inventory-auction-availability').textContent=reason||'';
  }
  function rowMarkup(row){
    const status=String(row.status||'queued'),name=row.name||row.item_key||'未识别物品',quantity=Number.isInteger(row.count)&&row.count>0?row.count:1;
    const position=row.container==='storage'?'仓库':row.container==='inventory'?'背包':'';
    const reason=row.reason||row.message||'';
    return `<div class="inventory-auction-row" data-status="${esc(status)}"><div><strong>${esc(name)}</strong><small>${esc(position)}${position?' · ':''}数量 ${quantity}${reason?' · '+esc(reason):''}</small></div><span class="inventory-auction-row-status">${esc(statusNames[status]||'等待核验')}</span></div>`;
  }
  function renderSteam(){
    const value=steam.steam||{},logged=value.authenticated===true;
    $('inventory-auction-steam-status').textContent=logged?'Steam 已登录'+(value.wallet_currency?' · '+value.wallet_currency:''):value.login_pending?'等待 Steam 登录完成':value.bound?'Steam 登录凭证待核验':'尚未登录 Steam';
    $('inventory-auction-steam-note').textContent=steamNotice||(logged?'使用托管模式中的同一 Steam 登录会话；勾选后先上传交易所，再出售。':'请前往“托管模式 → 拍卖行与 Steam 市场”登录；登录有效后才可勾选同时出售。');
    $('inventory-auction-with-steam').checked=mode==='sell';
    // Keep unauthenticated attempts focusable/clickable so the binding tip can appear.
    const blocked=!!availability(game,chosen,{});
    $('inventory-auction-with-steam').disabled=controller.value.busy||submitting||blocked;
    $('inventory-auction-with-steam').setAttribute('aria-disabled',String(controller.value.busy||submitting||blocked||!logged));
    $('inventory-auction-with-steam').title=logged?'':'Steam 登录尚未核验或已失效。';
    if(logged)loginTipRequested=false;
    $('inventory-auction-login-tip').hidden=!editing||logged||!loginTipRequested;
  }
  function render(){
    if(!controller)return;
    const {preview,batch,busy,error,uncertainStart,started}=controller.value;
    if(started)editing=false;
    const showPreview=editing&&!batch.running;
    const blocked=showPreview?availability(game,chosen,{}):'';
    const rows=showPreview?(preview?.items||[]):(batch.items||[]),showResult=batch.running||batch.total>0;
    $('inventory-auction-title').textContent='上架交易所';
    $('inventory-auction-description').textContent=mode==='sell'?'先将所选商品上传游戏交易所，再上架 Steam 市场。出售可能需要在 Steam 手机应用或邮件中确认。':'将所选商品从游戏背包上传到交易所 / Steam 库存。此操作不会在 Steam 市场出售。';
    $('inventory-auction-options').hidden=!showPreview;
    $('inventory-auction-price-field').hidden=mode!=='sell';$('inventory-auction-price-note').hidden=mode!=='sell';
    $('inventory-auction-steam').hidden=!showPreview;
    $('inventory-auction-price').disabled=busy||submitting||!!blocked;
    $('inventory-auction-summary').textContent=showPreview?(busy?'正在核验所选商品…':preview?`本次选择 ${preview.items?.length||chosen.length} 组 · 可处理 ${preview.ready_count||0} 组 · 跳过 ${preview.skipped_count||0} 组${preview.message?'。'+preview.message:''}`:'请重新核验本次商品。'):(batch.message||'等待批量操作进度。');
    $('inventory-auction-error').hidden=!(error||blocked);$('inventory-auction-error').textContent=error||blocked;
    const markup=rows.length?rows.map(rowMarkup).join(''):'<div class="empty-state">'+(busy?'正在读取商品…':'暂无商品清单')+'</div>';
    if($('inventory-auction-items').innerHTML!==markup)$('inventory-auction-items').innerHTML=markup;
    $('inventory-auction-scope').hidden=!showPreview;
    $('inventory-auction-submit').hidden=!showPreview;
    $('inventory-auction-submit').disabled=busy||submitting||uncertainStart||!preview?.token||preview.can_start===false||!(preview?.ready_count>0)||mode==='sell'&&steam.steam?.authenticated!==true||!!blocked;
    $('inventory-auction-submit').title=blocked;
    $('inventory-auction-submit').textContent=mode==='sell'?`确认上传并出售 ${preview?.ready_count||0} 组`:`确认上传 ${preview?.ready_count||0} 组`;
    $('inventory-auction-recheck').hidden=!showPreview&&!uncertainStart&&!error;
    $('inventory-auction-recheck').disabled=busy||submitting||!!batch.running||!!blocked;
    $('inventory-auction-cancel').hidden=!batch.running;
    $('inventory-auction-cancel').disabled=busy||batch.can_cancel===false;
    $('inventory-auction-footer-note').textContent=batch.running?'关闭窗口不停止任务。停止后续操作后，仍会核验已经提交的商品。':showPreview?'仓库商品会先取回背包；已提交的上传或卖单无法在此撤回。':'“已上架”表示已挂单，不代表已成交；待确认的卖单请前往 Steam 完成确认。';
    $('inventory-auction-progress').hidden=!showResult;
    $('inventory-auction-progress-title').textContent=batch.running?`批量上架进行中 · ${batch.completed||0} / ${batch.total||0}`:'上次批量上架结果';
    $('inventory-auction-progress-message').textContent=batch.message||'';
    $('inventory-auction-details').textContent=batch.running?'查看进度':'查看结果';
    updateButtons();renderSteam();
  }
  async function refreshSteam(fresh=false){
    if(steamRequest){await steamRequest;if(!fresh)return;}
    steamRequest=(async()=>{
      try{steam=unwrap(await options.api('auction/state'));steamNotice='';}
      catch(error){steam={steam:{authenticated:false}};steamNotice='Steam 登录状态核验失败，请稍后重试。';}
      if(editing&&!committing&&!controller.value.uncertainStart&&mode==='sell'&&steam.steam?.authenticated!==true){
        mode='upload';controller.clearPreview();
        steamNotice='Steam 登录已失效或尚未核验，已取消同时出售；请重新登录并勾选，或重新核验后仅上传交易所。';
      }
      render();
    })();
    try{await steamRequest;}finally{steamRequest=null;}
  }
  async function preview(){
    if(!chosen.length||availability(game,chosen,{})){render();return;}
    await controller.preview({items:cleanIds(chosen),mode,price_rule:$('inventory-auction-price').value});
  }
  async function open(){
    if(availability(game,selection,controller.value.batch)||controller.value.busy||submitting)return;
    mode='upload';steam={};steamNotice='';loginTipRequested=false;chosen=selection.map(row=>({...row}));editing=true;controller.clearPreview();
    $('inventory-auction-price').value='lowest_sell';
    render();$('inventory-auction-dialog').showModal();
    await Promise.all([refreshSteam(),preview()]);
  }
  function showResults(){editing=false;mode=controller.value.batch.mode||mode;render();if(!$('inventory-auction-dialog').open)$('inventory-auction-dialog').showModal();controller.refresh();}
  root.InventoryAuctionUI={helpers,init(config){
    if(initialized)return;initialized=true;options=config;
    controller=createController({api:config.api,onChange:render,onRefresh:config.onRefresh});
    $('inventory-auction-upload').addEventListener('click',open);
    $('inventory-auction-close').addEventListener('click',()=>$('inventory-auction-dialog').close());
    $('inventory-auction-price').addEventListener('change',preview);
    $('inventory-auction-with-steam').addEventListener('click',event=>{
      if(controller.value.busy||submitting||!editing||availability(game,chosen,{})){event.preventDefault();render();return;}
      if(steam.steam?.authenticated!==true){event.preventDefault();loginTipRequested=true;render();}
    });
    $('inventory-auction-with-steam').addEventListener('change',async()=>{
      if(controller.value.busy||submitting||!editing||availability(game,chosen,{})){render();return;}
      if(steam.steam?.authenticated!==true){loginTipRequested=true;render();return;}
      loginTipRequested=false;
      mode=$('inventory-auction-with-steam').checked?'sell':'upload';steamNotice='';await preview();
    });
    $('inventory-auction-recheck').addEventListener('click',()=>editing&&!controller.value.uncertainStart?preview():controller.refresh());
    $('inventory-auction-submit').addEventListener('click',async()=>{
      if($('inventory-auction-submit').disabled||submitting)return;
      const requestedMode=mode;submitting=true;render();
      try{
        if(requestedMode==='sell'){
          await refreshSteam(true);
          if(mode!==requestedMode||steam.steam?.authenticated!==true)return;
        }
        if(availability(game,chosen,{}))return;
        committing=true;await controller.start();
      }finally{committing=false;submitting=false;render();}
    });
    $('inventory-auction-cancel').addEventListener('click',()=>controller.cancel());
    $('inventory-auction-details').addEventListener('click',showResults);
    root.setInterval(()=>{if(active||controller.value.batch.running||$('inventory-auction-dialog').open)controller.refresh();if($('inventory-auction-dialog').open&&editing)refreshSteam();},2000);
    render();controller.refresh();
  },update(value,rows){game=value||{};selection=rows||[];if(controller)render();},setActive(value){active=!!value;if(active)controller?.refresh();}};
})(typeof window!=='undefined'?window:globalThis);
