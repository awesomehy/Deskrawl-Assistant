'use strict';
(()=>{
  const get=id=>document.getElementById(id),dialog=get('settings-dialog');
  if(!dialog)return;
  const open=get('settings-open'),steam=get('settings-steam'),hotkey=get('settings-hotkey'),save=get('settings-save');
  let snapshot=null,draft=null,busy=false,submitting=false,captureTimer=null,captureOriginal='',capturing=false,generation=0,captureVersion=0,captureRequests=Promise.resolve();
  const resetButtons=['settings-reset-rules','settings-reset-automation','settings-reset-cache'].map(get);
  function error(message=''){get('settings-error').textContent=message;get('settings-error').hidden=!message;}
  function paint(){
    steam.setAttribute('aria-checked',String(!!draft?.steam_acceleration_enabled));
    get('settings-steam-state').textContent=draft?.steam_acceleration_enabled?'开启':'关闭';
    hotkey.value=draft?.boss_hotkey||'';
    get('settings-hotkey-status').textContent=draft?.boss_hotkey?'已设置':'未设置';
    steam.disabled=!draft||busy;save.disabled=!draft||busy;
    hotkey.disabled=!draft||busy||!snapshot?.desktop_supported;
    get('settings-hotkey-clear').disabled=hotkey.disabled||!draft?.boss_hotkey;
    get('settings-open-folder').disabled=!snapshot||busy;
    get('settings-close').disabled=submitting;get('settings-cancel').disabled=submitting;
    resetButtons.forEach((button,index)=>{if(button)button.disabled=!snapshot||busy||(index===2&&!snapshot.desktop_supported);});
  }
  function apply(value){
    if(typeof value.steam_acceleration_enabled!=='boolean'||typeof value.boss_hotkey!=='string'||typeof value.data_directory!=='string')throw new Error('设置读取失败，请重新打开设置。');
    snapshot=value;draft={steam_acceleration_enabled:value.steam_acceleration_enabled,boss_hotkey:value.boss_hotkey};
    get('settings-data-path').textContent=value.data_directory;
    error(value.load_error||value.hotkey?.error||value.tray_error||'');paint();
  }
  function setCapture(value){captureRequests=captureRequests.catch(()=>{}).then(()=>api('window/hotkey-capture',{capturing:value}));return captureRequests;}
  function endCapture(){
    captureVersion++;
    clearTimeout(captureTimer);captureTimer=null;hotkey.classList.remove('recording');
    hotkey.placeholder='点击这里，再按组合键';
    if(capturing){capturing=false;return setCapture(false).catch(()=>{});}
    return captureRequests.catch(()=>{});
  }
  function close(force=false){if(submitting&&force!==true)return;generation++;endCapture();dialog.close();open.setAttribute('aria-expanded','false');open.focus();}
  open.addEventListener('click',async()=>{
    if(dialog.open)return;
    const version=++generation;
    snapshot=null;draft=null;busy=true;error();paint();get('settings-loading').hidden=false;
    if(get('settings-reset-result'))get('settings-reset-result').hidden=true;
    dialog.showModal();open.setAttribute('aria-expanded','true');
    try{const value=await api('settings');if(version===generation)apply(value);}catch(e){if(version===generation)error(e.message);}
    finally{if(version===generation){busy=false;get('settings-loading').hidden=true;paint();}}
  });
  get('settings-close').addEventListener('click',close);get('settings-cancel').addEventListener('click',close);
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  steam.addEventListener('click',()=>{if(!draft||busy)return;draft.steam_acceleration_enabled=!draft.steam_acceleration_enabled;paint();});
  hotkey.addEventListener('focus',async()=>{
    if(!draft||busy||!snapshot?.desktop_supported)return;
    captureOriginal=draft.boss_hotkey;error();
    const version=++captureVersion;capturing=true;
    try{
      await setCapture(true);
      if(version!==captureVersion)return;
      if(document.activeElement!==hotkey){endCapture();return;}
      hotkey.classList.add('recording');hotkey.placeholder='请按下组合键…';
      captureTimer=setTimeout(()=>{hotkey.blur();},29000);
    }catch(e){error(e.message);hotkey.blur();}
  });
  hotkey.addEventListener('blur',endCapture);
  hotkey.addEventListener('keydown',event=>{
    if(event.key==='Tab')return;
    event.preventDefault();event.stopPropagation();
    if(event.key==='Escape'){draft.boss_hotkey=captureOriginal;paint();hotkey.blur();return;}
    if(['Control','Alt','Shift','Meta'].includes(event.key))return;
    const key=event.code?.startsWith('Key')?event.code.slice(3):event.code?.startsWith('Digit')?event.code.slice(5):event.key.toUpperCase();
    if(event.metaKey||(!event.ctrlKey&&!event.altKey)||!/^([A-Z0-9]|F(?:[1-9]|1[01]))$/.test(key)){
      error('请使用 Ctrl 或 Alt 搭配字母、数字或 F1–F11。');return;
    }
    const parts=[];if(event.ctrlKey)parts.push('Ctrl');if(event.altKey)parts.push('Alt');if(event.shiftKey)parts.push('Shift');parts.push(key);
    if(parts.join('+')==='Alt+F4'){error('此组合键由系统使用，请换一个组合。');return;}
    draft.boss_hotkey=parts.join('+');error();paint();hotkey.blur();
  });
  get('settings-hotkey-clear').addEventListener('click',()=>{if(!draft||busy)return;draft.boss_hotkey='';error();paint();});
  get('settings-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!draft||busy)return;
    busy=true;submitting=true;error();paint();
    try{
      await endCapture();
      const previous=snapshot.steam_acceleration_enabled;
      apply(await api('settings',{...draft}));
      if(previous!==snapshot.steam_acceleration_enabled)window.SteamMarketUI?.retry?.();
      toast('助手设置已保存');close(true);
    }catch(e){
      // An interrupted response can have committed. Read back the truth rather
      // than showing a local rollback that may not match the saved preference.
      try{apply(await api('settings'));}catch{/* Keep controls available for retry. */}
      error(e.message);
    }finally{busy=false;submitting=false;paint();}
  });
  get('settings-open-folder').addEventListener('click',async()=>{
    if(busy||!snapshot)return;const button=get('settings-open-folder');button.disabled=true;error();
    try{await api('settings/open-folder',{});}catch(e){error(e.message);}finally{button.disabled=false;}
  });
  for(const [index,section,title,description,label] of [
    [0,'equipment_rules','重置装备筛选规则？','将清空所有已保存的筛选组合，并丢弃尚未保存的规则改动。\n\n保留游戏内装备锁定状态、托管与背包仓库设置、助手设置和运行记录。原配置文件会先自动备份。\n\n确认后立即生效，无需再点击“保存设置”。','清空筛选规则'],
    [1,'automation','重置托管与背包仓库设置？','将把全部托管设置、背包仓库自动整理及共用存放页恢复为默认值，并丢弃这部分尚未保存的改动；之后需要重新配置相关功能。\n\n保留装备筛选规则、老板键、Steam 加速与登录凭证、历史日志和掉落留档。原配置文件会先自动备份。\n\n确认后立即生效，无需再点击“保存设置”。','恢复默认设置'],
    [2,'browser_cache','清理内置浏览器缓存？','将清理助手内置浏览器的网页资源缓存，后续打开页面可能需要重新加载资源。\n\n保留登录凭证、全部助手配置、历史日志和掉落留档。\n\n确认后立即清理，无需再点击“保存设置”。','确认清理缓存']]){
    resetButtons[index]?.addEventListener('click',async()=>{
      if(busy||!snapshot)return;
      busy=true;submitting=true;error();paint();
      try{
        await endCapture();
        if(await confirmAction(title,description,label)!==true)return;
        const value=await api('settings/reset',{section});
        const result=get('settings-reset-result');
        result.textContent=value.backup_directory?'已重置，原配置备份于：'+value.backup_directory:'已完成。';result.hidden=false;
        try{await window.AssistantConfigurationUI?.refresh(section);}
        catch{throw new Error('重置已完成，界面刷新失败。请重新打开助手查看已保存的配置。');}
        toast(index===2?'浏览器缓存已清理':'所选配置已重置');
      }catch(e){error(e.message);}finally{busy=false;submitting=false;paint();}
    });
  }
})();
