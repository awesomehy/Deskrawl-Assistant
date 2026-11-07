/* Local Steam identity display. Market authentication remains independent. */
(() => {
  'use strict';
  const root=document.getElementById('steam-profile');
  const name=document.getElementById('steam-profile-name');
  const detail=document.getElementById('steam-profile-detail');
  const image=document.getElementById('steam-profile-avatar');
  const fallback=document.getElementById('steam-profile-fallback');
  const labels={
    loading:['正在识别 Steam…','正在读取昵称和头像…'],
    online:['Steam 已登录','Steam · 已登录'],
    logged_out:['未登录 Steam','请在 Steam 客户端登录'],
    not_running:['Steam 未运行','启动后自动识别'],
    profile_unavailable:['Steam 已登录','账号信息暂不可用'],
    unknown:['Steam 状态未确认','稍后自动重试']
  };
  let source='',failedAt=null;
  image.addEventListener('error',()=>{if(!source)return;failedAt=Date.now();image.hidden=true;fallback.hidden=false;});
  image.addEventListener('load',()=>{if(!source)return;failedAt=null;image.hidden=false;fallback.hidden=true;});
  function render(packet){
    const status=labels[packet?.status]?packet.status:'unknown';
    const online=status==='online'||status==='profile_unavailable';
    const nickname=online&&typeof packet?.nickname==='string'?packet.nickname:'';
    const text=nickname||labels[status][0];
    if(name.textContent!==text)name.textContent=text;
    name.title=text;
    if(detail.textContent!==labels[status][1])detail.textContent=labels[status][1];
    root.dataset.status=status;
    root.title=`本机 Steam 客户端：${text}\n${labels[status][1]}\n与 Steam 市场登录状态独立`;
    const next=online&&/^\/api\/steam-client\/avatar\?v=[0-9a-f]{64}$/.test(packet?.avatar_url||'')?packet.avatar_url:'';
    if(next!==source||next&&failedAt!==null&&Date.now()-failedAt>=30000){
      source=next;failedAt=null;image.hidden=true;fallback.hidden=false;
      image.removeAttribute('src');
      if(source)image.src=source;
    }
  }
  window.SteamClientUI={render};
})();
