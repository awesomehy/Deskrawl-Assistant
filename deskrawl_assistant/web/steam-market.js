'use strict';
// Quotes are fetched separately from inventory reads and never delay game work.
window.SteamMarketUI=(()=>{
  const quotes=new Map();let busy=false,current=[],enabled=false,request=0,visibleKey='[]';
  let settings=null,loadingSettings=false,savingSettings=false;
  let quoteRetryAfter=0,quoteProtocolError=false;
  const protocolWarning='市场报价接口版本不兼容，请重新打开助手；10 秒后自动重试。';
  const currencySelect=document.getElementById('market-currency'),currencyError=document.getElementById('market-currency-error');
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const amount=(value,quote)=>{
    if(value===null||value===undefined||!/^\d+(\.\d+)?$/.test(String(value))||!Number.isFinite(Number(value)))return '暂无订单';
    const numeric=Number(value);
    try{return new Intl.NumberFormat('zh-CN',{style:'currency',currency:quote.currency,maximumFractionDigits:2}).format(numeric);}
    catch{return `${numeric.toFixed(2)} ${quote.currency||''}`;}
  };
  function content(quote,detail=false){
    if(!quote||quote.eligible!==true)return '';
    const converted=quote.converted===true;
    const displayAmount=value=>{
      const text=amount(value,quote);
      return escape(converted&&text!=='暂无订单'?'≈ '+text:text);
    };
    const values=`${converted?'参考价 · ':''}在售最低 ${displayAmount(quote.lowest_sell)} · 求购最高 ${displayAmount(quote.highest_buy)}`;
    const ready=quote.status==='ready'||quote.lowest_sell!=null||quote.highest_buy!=null;
    const status=quote.stale?'缓存报价 · ':'';
    const text=ready?status+values:escape(quote.message||'正在读取 Steam 市场…');
    // The server supplies the only permitted external link; validate its host too.
    let link='';try{const url=new URL(quote.market_url);if(url.protocol==='https:'&&url.hostname==='steamcommunity.com'&&url.pathname.startsWith('/market/listings/4623570/'))link=url.href;}catch{}
    if(!detail)return text;
    const hasOriginal=typeof quote.original_currency==='string'&&quote.original_currency.length>0;
    let original='';
    if(hasOriginal){
      const nativeQuote={currency:quote.original_currency};
      original=`<p>Steam 原始报价（${escape(quote.original_currency)}）：在售最低 ${escape(amount(quote.original_lowest_sell,nativeQuote))} · 求购最高 ${escape(amount(quote.original_highest_buy,nativeQuote))}</p>`;
    }
    let exchange='';
    if(converted){
      const date=/^\d{4}-\d{2}-\d{2}$/.test(String(quote.exchange_rate_date||''))?quote.exchange_rate_date:'未提供';
      exchange=`<p>参考汇率：1 ${escape(quote.original_currency||'原始币种')} = ${escape(quote.exchange_rate||'—')} ${escape(quote.currency||'所选币种')} · 汇率日期 ${escape(date)}${quote.exchange_rate_stale===true?' · 缓存汇率':''}</p>`;
    }
    const note=quote.price_note||(converted||hasOriginal?'所选币种仅用于参考换算；实际交易以 Steam 原始报价为准。':'每件价格，以 Steam 实际返回的币种显示；报价会缓存，交易以市场页面为准。');
    return `<section class="steam-market-card"><h4>Steam 社区市场${link?` <a href="${escape(link)}" target="_blank" rel="noopener noreferrer">查看 ↗</a>`:''}</h4><p>${text}</p>${original}${exchange}<small>${escape(note)}</small></section>`;
  }
  function paint(){
    document.querySelectorAll('[data-market-item]').forEach(node=>{
      const quote=quotes.get(node.dataset.marketItem);node.innerHTML=content(quote,node.dataset.marketDetail==='true');
      node.hidden=!quote||quote.eligible!==true;
    });
  }
  function slot(item,detail=false){
    const id=item?.selection_id;if(!id||item.container==='equipped')return '';
    return `<${detail?'div':'small'} class="steam-market-quote" data-market-item="${escape(id)}"${detail?' data-market-detail="true"':''} hidden></${detail?'div':'small'}>`;
  }
  function settingsError(message=''){
    if(!currencyError)return;
    if(!message&&quoteProtocolError)message=protocolWarning;
    currencyError.textContent=message;currencyError.hidden=!message;
  }
  function applySettings(value){
    const currencies=(Array.isArray(value?.currencies)?value.currencies:[]).filter(row=>Number.isInteger(row.id)&&row.id>0);
    const currencyId=value?.currency_id;
    if(!Number.isInteger(currencyId)||currencyId<0||currencyId!==0&&!currencies.some(row=>row.id===currencyId))throw new Error('市场币种设置无效，请稍后重试。');
    settings={currency_id:currencyId,currencies};
    if(currencySelect){
      currencySelect.innerHTML='<option value="0">自动（Steam 返回币种）</option>'+currencies.map(row=>`<option value="${row.id}">${escape(row.name||row.code)}${row.code?` (${escape(row.code)})`:''}</option>`).join('');
      currencySelect.value=String(currencyId);currencySelect.disabled=savingSettings;
    }
  }
  async function loadSettings(reconcile=false){
    if(settings||loadingSettings||savingSettings&&!reconcile)return;
    loadingSettings=true;if(currencySelect)currencySelect.disabled=true;
    try{applySettings(await api('market/settings'));settingsError();}
    catch(error){settingsError(error?.message||'市场币种读取失败，稍后自动重试。');}
    finally{loadingSettings=false;if(enabled&&settings)refresh();}
  }
  async function changeCurrency(){
    if(!settings||savingSettings)return;
    const currencyId=Number(currencySelect.value);
    if(currencyId===settings.currency_id)return;
    if(!Number.isInteger(currencyId)||currencyId!==0&&!settings.currencies.some(row=>row.id===currencyId)){
      currencySelect.value=String(settings.currency_id);return;
    }
    // Quotes belong to the currency they were requested in. Clear them before
    // saving, and ignore old in-flight batches rather than relabeling amounts.
    request++;quotes.clear();paint();savingSettings=true;currencySelect.disabled=true;settingsError();
    try{applySettings(await api('market/settings',{currency_id:currencyId}));}
    catch(error){
      // A lost response does not establish whether the server saved the value.
      // Read it back before accepting any price; never assume a rollback.
      settings=null;settingsError('币种保存结果未确认，正在重新读取设置…');
      await loadSettings(true);
    }
    finally{savingSettings=false;currencySelect.disabled=!settings;if(enabled&&settings)refresh();}
  }
  if(currencySelect){
    currencySelect.title='自动显示 Steam 原始币种；选择其他币种时显示汇率换算参考价，交易以 Steam 原始报价为准。';
    currencySelect.addEventListener('change',changeCurrency);
  }
  async function refresh(){
    if(!enabled||busy||savingSettings||Date.now()<quoteRetryAfter)return;
    if(!settings){loadSettings();return;}
    if(!current.length)return;
    const ids=[...new Set(current.map(item=>item.selection_id).filter(Boolean))];
    const generation=request;busy=true;
    try{
      for(let offset=0;offset<ids.length;offset+=128){
        if(!enabled||generation!==request)break;
        const result=await api('market/quotes',{items:ids.slice(offset,offset+128)});
        if(generation!==request)break;
        const metadataValid=Number.isInteger(result.currency_id)&&result.currency_id>=0;
        if(metadataValid){quoteProtocolError=false;quoteRetryAfter=0;settingsError();}
        if(!metadataValid||result.currency_id!==settings?.currency_id){
          // Another window may update the shared preference while this view is
          // open. Ignore mismatched quotes and reconcile even an empty batch.
          // Older or malformed API responses need a cooldown: successful GET
          // readback otherwise loops immediately into the incompatible API.
          if(!metadataValid){quoteProtocolError=true;quoteRetryAfter=Date.now()+10000;}
          request++;settings=null;quotes.clear();paint();
          settingsError(metadataValid?'市场币种已变化，正在重新读取设置…':protocolWarning);loadSettings();break;
        }
        for(const row of result.items||[])if(ids.includes(row.selection_id))quotes.set(row.selection_id,row.quote);
        paint();
      }
    }catch{/* A market outage must never affect inventory controls or managed mode. */}
    finally{
      busy=false;
      // An in-flight request cannot be withdrawn, but a changed filter must
      // stop its later batches and immediately start the current visible set.
      if(enabled&&generation!==request)refresh();
    }
  }
  function update(items,active){
    const next=!!active,nextItems=Array.isArray(items)?items:[];
    const nextKey=JSON.stringify([...new Set(nextItems.map(item=>item.selection_id).filter(Boolean))].sort());
    // Polling can replace or reorder rows without changing their identities;
    // preserve that generation so long visible lists finish all their batches.
    if(enabled!==next||visibleKey!==nextKey){request++;visibleKey=nextKey;}
    enabled=next;current=nextItems;
    const live=new Set(current.map(item=>item.selection_id));for(const key of quotes.keys())if(!live.has(key))quotes.delete(key);
    paint();if(enabled)refresh();
  }
  setInterval(refresh,5000);
  return {slot,update,paint,content,amount};
})();
