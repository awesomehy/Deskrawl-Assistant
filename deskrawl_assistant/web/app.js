'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const desktopMode=new URLSearchParams(location.search).get('desktop')==='1';
let token = document.querySelector('meta[name="assistant-token"]').content;
let catalog, state, gearKey, draft, dirty=false, selectedClass=0, containerFilter='', detailId=null;
let selectedItems=new Set(), itemSignature='', catalogSignature='', polling=false, toastTimer, exiting=false;
const gearMap=new Map(), statMap=new Map();
let lastRules='';

async function api(path,data,retry=true){
  const response=await fetch('/api/'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Assistant-Token':token},body:JSON.stringify(data)});
  const result=await response.json();
  if(response.status===403&&result.session_expired&&retry){
    const session=await fetch('/api/session').then(r=>r.json());
    token=session.token;
    return api(path,data,false);
  }
  if(!response.ok)throw new Error(result.error||'操作未完成，请重试。');
  return result;
}
function toast(message,error=false){
  clearTimeout(toastTimer);$('toast').textContent=message;$('toast').className=error?'error':'';$('toast').hidden=false;
  toastTimer=setTimeout(()=>$('toast').hidden=true,error?6500:3500);
}
function confirmAction(title,body,label='确认',danger=true){
  $('confirm-title').textContent=title;$('confirm-body').textContent=body;
  $('confirm-ok').textContent=label;$('confirm-ok').className='button '+(danger?'danger':'primary');
  const dialog=$('confirm-dialog');dialog.showModal();
  return new Promise(resolve=>{
    const done=(value)=>{dialog.close();$('confirm-ok').onclick=null;$('confirm-cancel').onclick=null;dialog.oncancel=null;resolve(value);};
    $('confirm-ok').onclick=()=>done(true);$('confirm-cancel').onclick=()=>done(false);dialog.oncancel=e=>{e.preventDefault();done(false);};
  });
}
function classesLabel(mask){
  if(mask===15)return '全职业';
  return catalog.classes.filter(c=>mask&c.value).map(c=>c.name).join(' / ')||'无职业限制记录';
}
function gearIcon(gear,cls='gear-icon'){
  return `<div class="${cls}">${gear?.icon?`<img src="${esc(gear.icon)}" alt="${esc(gear.name)}">`:''}</div>`;
}
function rulesFor(key){return (state?.rules||[]).filter(r=>r.equipment_keys.includes(key));}
function switchTab(tab){
  document.querySelectorAll('.tab').forEach(button=>{let active=button.dataset.tab===tab;button.classList.toggle('active',active);button.setAttribute('aria-selected',active);});
  $('rules-page').hidden=tab!=='rules';$('inventory-page').hidden=tab!=='inventory';
  if(tab==='inventory')renderInventory();
}
function buildCatalog(){
  catalog.equipment.forEach(g=>gearMap.set(g.key,g));catalog.stats.forEach(s=>statMap.set(s.key,s));
  $('class-filters').innerHTML=`<button class="selected" data-class="0">全部职业</button>`+catalog.classes.map(c=>`<button data-class="${c.value}">${esc(c.name)}</button>`).join('');
  $('slot-filter').innerHTML='<option value="">全部部位</option>'+catalog.slots.map(s=>`<option value="${esc(s.key)}">${esc(s.name)}</option>`).join('');
}
function renderCatalog(){
  let query=$('equipment-search').value.trim().toLowerCase(),slot=$('slot-filter').value,configured=$('configured-filter').value;
  let gears=catalog.equipment.filter(g=>g.legendary&&(!selectedClass||(g.class_mask&selectedClass))&&(!slot||g.slot===slot)&&
    (!query||(g.name+' '+g.en+' '+g.key).toLowerCase().includes(query))&&(!configured||(rulesFor(g.key).length>0)===(configured==='yes')));
  const slotOrder=new Map(catalog.slots.map((s,i)=>[s.key,i]));
  gears.sort((a,b)=>slotOrder.get(a.slot)-slotOrder.get(b.slot)||a.name.localeCompare(b.name,'zh-CN'));
  $('equipment-count').textContent=`${gears.length} 件传说装备`;
  $('equipment-grid').innerHTML=gears.length?gears.map(g=>{
    const rules=rulesFor(g.key),enabled=rules.some(r=>r.enabled);
    return `<button class="gear-card ${g.key===gearKey?'active':''}" data-gear="${esc(g.key)}" aria-pressed="${g.key===gearKey}">
      ${rules.length?`<span class="configured-dot ${enabled?'':'off'}"></span>`:''}${gearIcon(g)}<strong title="${esc(g.name)}">${esc(g.name)}</strong>
      <small>${esc(g.slot_label)} · ${esc(classesLabel(g.class_mask))}</small><small class="card-status ${enabled?'':'off'}">${rules.length?(enabled?'规则已启用':'规则已停用'):'未配置规则'}</small></button>`;
  }).join(''):'<div class="empty-state">没有找到符合条件的装备<br>试试其他职业、部位或搜索词</div>';
}
function makeDraft(rule){
  const groups=rule?.groups||{};
  return {id:rule?.id||'',name:rule?.name||gearMap.get(gearKey).name,enabled:rule?.enabled??true,
    primary:[...(groups.primary?.selected_stats||[])],primaryCount:groups.primary?.count||1,
    secondary:[...(groups.secondary?.selected_stats||[])],secondaryCount:groups.secondary?.count||1,
    secondaryEnabled:!!groups.secondary,legacy:!!rule&&!rule.groups};
}
async function chooseGear(key,ruleId){
  if(dirty&&!await confirmAction('切换装备配置？','当前配置还没有保存。切换后将放弃这些改动。','放弃改动并切换',false))return;
  gearKey=key;const candidates=rulesFor(key);const rule=candidates.find(r=>r.id===ruleId)||candidates[0];
  draft=makeDraft(rule);setDirty(false);renderCatalog();renderEditor();
  const grid=$('equipment-grid'),active=grid.querySelector('.gear-card.active');
  if(active){const area=grid.getBoundingClientRect(),card=active.getBoundingClientRect();if(card.bottom>area.bottom)grid.scrollTop+=card.bottom-area.bottom+10;else if(card.top<area.top)grid.scrollTop+=card.top-area.top-10;}
}
function groupMarkup(group,title){
  const gear=gearMap.get(gearKey),optional=group==='secondary',enabled=!optional||draft.secondaryEnabled;
  const pool=catalog.pools[gear.slot]?.[group]||[],selected=draft[group];
  const options=[...pool,...selected.filter(key=>!pool.includes(key))];
  return `<div class="affix-group ${enabled?'':'disabled'}" data-group="${group}"><div class="group-heading"><div class="group-title"><span class="step">${optional?'2':'1'}</span><h3>${title}</h3></div>
    ${optional?`<label class="group-toggle"><input type="checkbox" id="secondary-enabled" ${enabled?'checked':''}>启用副词条筛选</label>`:'<small>先判断这一组</small>'}</div>
    <div class="count-line">选中的词条，至少命中 <input type="number" min="1" max="${Math.max(1,selected.length)}" value="${draft[group+'Count']}" data-count="${group}" aria-label="${title}至少命中数量" ${enabled?'':'disabled'}> 类<span class="selected-total" data-total="${group}">已选 ${selected.length} 类</span></div>
    <input class="affix-search" data-affix-search="${group}" placeholder="搜索${title}…" aria-label="搜索${title}" ${enabled?'':'disabled'}>
    <div class="affix-options">${options.length?options.map(key=>`<label class="affix-chip ${selected.includes(key)?'checked':''}" data-stat-name="${esc((statMap.get(key)?.name+' '+statMap.get(key)?.en).toLowerCase())}"><input type="checkbox" data-affix="${esc(key)}" data-group="${group}" ${selected.includes(key)?'checked':''} ${enabled?'':'disabled'}>${esc(statMap.get(key)?.name||key)}${!pool.includes(key)?' · 不参与筛选，请取消勾选':''}</label>`).join(''):'<div class="note">此部位的本地随机词条池为空，目前无法配置该组条件。</div>'}</div></div>`;
}
function renderEditor(){
  if(!gearKey||!draft)return;
  const gear=gearMap.get(gearKey),existing=rulesFor(gearKey);
  $('rule-editor').innerHTML=`<div class="editor-head">${gearIcon(gear)}<div><div class="eyebrow">传说装备 · ${esc(gear.slot_label)} · ${esc(classesLabel(gear.class_mask))}</div><h2>${esc(gear.name)}</h2><small>${esc(gear.en)}</small></div></div>
    <div class="editor-content">${gear.description?`<div class="legendary-description">${esc(gear.description)}</div>`:''}
    ${existing.length>1?`<div class="editor-rule-select"><select id="existing-rule" aria-label="选择这件装备的规则">${existing.map(r=>`<option value="${esc(r.id)}" ${draft.id===r.id?'selected':''}>${esc(r.name)}${r.enabled?' · 已启用':' · 已停用'}</option>`).join('')}</select><button id="new-rule" class="button ghost">新增组合</button></div>`:''}
    <div class="rule-state"><label><input type="checkbox" id="rule-enabled" ${draft.enabled?'checked':''}>启用这件装备的筛选规则</label><span id="draft-status">${draft.id?'已保存':'尚未配置'}</span></div>
    ${draft.legacy?'<div class="inline-error">这是旧版数值规则；保存将改为当前主、副词条数量规则。</div>':''}
    ${groupMarkup('primary','主词条')}<div class="flow-label">↓ 主词条达标后，再检查副词条</div>${groupMarkup('secondary','副词条')}
    <div id="rule-summary" class="rule-summary"></div><div id="rule-error" class="inline-error" hidden></div>
    <p class="note">勾选的是可接受词条池，只需其中任意 n 类达标，并非每个勾选项都必需。同类型只算一次；基础属性不参与计数。保存并启用后，一键锁定和持续监控都使用这条规则。</p></div>
    <div class="editor-actions"><button id="delete-rule" class="text-button" ${draft.id?'':'hidden'}>删除规则</button><span class="spacer"></span><button id="reset-rule" class="button ghost">重置改动</button><button id="save-rule" class="button primary">${draft.id?'保存修改':'保存并应用'}</button></div>`;
  updateSummary();
}
function setDirty(value){dirty=value;if(desktopMode)api('window/draft',{dirty:value}).catch(()=>{});}
function markDirty(){setDirty(true);$('draft-status').textContent='有未保存的改动';updateSummary();}
function validation(){
  const gear=gearMap.get(gearKey),pool=catalog.pools[gear.slot];
  if(!pool?.primary?.length)return '此部位没有可用的随机词条，暂不支持词条筛选配置。';
  for(const group of ['primary',...(draft.secondaryEnabled?['secondary']:[])]){
    const label=group==='primary'?'主词条':'副词条',number=draft[group+'Count'];
    if(!draft[group].length)return `请至少选择一种${label}。`;
    if(!Number.isInteger(number)||number<1)return `${label}命中数量必须是大于 0 的整数。`;
    if(number>draft[group].length)return `${label}要求至少 ${number} 类，但只选了 ${draft[group].length} 类。`;
    const invalid=draft[group].filter(s=>!pool?.[group]?.includes(s));
    if(invalid.length)return `${invalid.map(s=>statMap.get(s)?.name||s).join('、')}不参与此部位的随机${label}筛选，请取消勾选后保存。`;
  }
  return state?.config_error||'';
}
function updateSummary(){
  if(!$('rule-summary'))return;
  const second=draft.secondaryEnabled?`，并且副词条至少命中 ${draft.secondaryCount} / ${draft.secondary.length} 类`:'，不检查副词条';
  $('rule-summary').textContent=`主词条至少命中 ${draft.primaryCount} / ${draft.primary.length} 类${second}，则锁定。`;
  document.querySelectorAll('[data-total]').forEach(e=>e.textContent=`已选 ${draft[e.dataset.total].length} 类`);
  document.querySelectorAll('[data-count]').forEach(e=>e.max=Math.max(1,draft[e.dataset.count].length));
  const error=validation();$('rule-error').textContent=error;$('rule-error').hidden=!error;
  $('save-rule').disabled=!!error;
}
async function saveRule(){
  const error=validation();if(error){toast(error,true);return;}
  const groups={primary:{selected_stats:draft.primary,operator:'>=',count:draft.primaryCount}};
  if(draft.secondaryEnabled)groups.secondary={selected_stats:draft.secondary,operator:'>=',count:draft.secondaryCount};
  $('save-rule').disabled=true;
  try{
    const result=await api('rule/save',{id:draft.id,name:draft.name,equipment_keys:[gearKey],enabled:draft.enabled,groups,group_mode:'all'});
    setDirty(false);draft.id=result.rule.id;await pollState();renderEditor();toast(draft.enabled?'规则已保存并启用':'规则已保存，当前停用');
  }catch(error){toast(error.message,true);updateSummary();}
}
function scopedItems(){const scope=$('action-scope').value;return state.items.filter(i=>scope==='all'||i.container===scope);}
function visibleItems(){
  const query=$('item-search').value.trim().toLowerCase(),lock=$('lock-filter').value;
  return (state?.items||[]).filter(i=>(!containerFilter||i.container===containerFilter)&&
    (!query||(i.name+' '+(gearMap.get(i.name_key)?.en||'')).toLowerCase().includes(query))&&
    (!lock||(lock==='locked'?i.locked===true:lock==='unlocked'?i.locked===false:i.matches)));
}
function resultBadge(item){
  return item.matches?'<span class="badge match">符合规则</span>':item.review?'<span class="badge review">待核验</span>':'<span class="badge miss">未命中</span>';
}
function renderInventory(){
  if(!state)return;
  const view=visibleItems();
  selectedItems=new Set([...selectedItems].filter(id=>state.items.some(i=>i.instance_id===id&&i.locked===false)));
  $('visible-count').textContent=`${view.length} 件装备`;
  const signature=JSON.stringify([view,detailId,[...selectedItems]]);
  if(signature!==itemSignature){
    itemSignature=signature;
    $('item-rows').innerHTML=view.map(i=>`<tr data-item="${esc(i.instance_id)}" class="${i.instance_id===detailId?'active':''}" tabindex="0">
      <td><input type="checkbox" data-item-select="${esc(i.instance_id)}" aria-label="选择${esc(i.name)}" ${selectedItems.has(i.instance_id)?'checked':''} ${i.locked!==false?'disabled':''}></td>
      <td><div class="item-name">${i.icon?`<img src="${esc(i.icon)}" alt="">`:''}<div><strong>${esc(i.name)}</strong><small>${esc(i.slot_label)}</small></div></div></td>
      <td>${i.container==='inventory'?'背包':'仓库'} · ${Number(i.slot_index)+1}</td><td>${esc(i.level)}${i.upgrade?` <small>+${i.upgrade}</small>`:''}</td>
      <td><span class="badge ${i.locked?'locked':'unlocked'}">${i.locked===true?'已锁定':i.locked===false?'未锁定':'未确认'}</span></td><td>${resultBadge(i)}</td></tr>`).join('');
    $('inventory-empty').hidden=view.length>0;
    $('inventory-empty').textContent=state.connected?'当前筛选条件下没有装备':'连接游戏，查看背包和仓库装备';
    renderDetail();
  }
  const available=view.filter(i=>i.locked===false);
  $('select-visible').checked=available.length>0&&available.every(i=>selectedItems.has(i.instance_id));
  $('select-visible').indeterminate=available.some(i=>selectedItems.has(i.instance_id))&&!$('select-visible').checked;
  $('select-visible').disabled=!available.length;
  $('selected-count').textContent=selectedItems.size;
  updateControls();
}
function renderDetail(){
  const item=state.items.find(i=>i.instance_id===detailId);
  if(!item){$('item-detail').innerHTML='<div class="empty-state">点击装备查看词条与筛选结果</div>';return;}
  $('item-detail').innerHTML=`<div class="detail-title">${item.icon?`<img src="${esc(item.icon)}" alt="">`:''}<div><h3>${esc(item.name)}</h3><small>${esc(item.slot_label)} · ${esc(classesLabel(item.class_mask))}</small></div></div>
    <div class="detail-meta"><span>物品等级 ${esc(item.level)}</span><span>强化 +${esc(item.upgrade??0)}</span></div>
    ${['base','primary','secondary','unknown'].map(g=>{const values=item.groups[g]||[];return values.length?`<div class="detail-group"><h4>${{base:'基础属性 · 不参与筛选',primary:'主词条',secondary:'副词条',unknown:'待核验词条'}[g]}</h4><div class="type-list">${values.map(v=>`<div class="affix-row ${v.matched?'matched':''}" data-affix-key="${esc(v.key)}" title="${esc(v.value_note||(v.matched?'命中规则：'+v.matched_rules.join('、'):''))}"><span class="affix-name">${esc(v.name)}</span><strong class="affix-value">${esc(v.display_value??'未读取')}</strong>${v.matched?'<small class="affix-hit" aria-label="命中规则">✓</small>':''}</div>`).join('')}</div></div>`:'';}).join('')}
    <p class="detail-hint">绿色为命中已启用规则的词条；整件装备是否达标见下方结果。数值包含已确认的装备强化。</p>
    <div class="detail-rule">${resultBadge(item)}${item.reasons.map(r=>`<p>${esc(r)}</p>`).join('')}<button class="text-button" data-configure-item="${esc(item.name_key)}">配置这件装备的规则 →</button></div>`;
}
function updateControls(){
  if(!state)return;
  const available=state.connected&&!state.busy&&state.complete,enabled=state.rules.some(r=>r.enabled);
  $('connect').hidden=state.connected;$('connect').disabled=state.busy;
  $('disconnect').hidden=!state.connected;$('disconnect').disabled=state.busy;
  $('refresh').disabled=!state.connected||state.busy;
  $('unlock-all').disabled=!available||!scopedItems().some(i=>i.locked===true);
  $('lock-selected').disabled=!available||!selectedItems.size;
  $('lock-rules').disabled=!available||!enabled;
  $('lock-rules').title=enabled?'按已启用规则处理批量范围内的装备':'请先在规则页保存并启用至少一条规则';
  $('export-items').disabled=!state.items.length;
  $('monitor-switch').disabled=!state.connected||(!state.monitoring&&(state.busy||!state.complete||!enabled));
  $('monitor-switch').title=!state.connected?'请先连接游戏':!enabled?'请先保存并启用至少一条规则':'监控背包和仓库里的新增装备';
  $('monitor-switch').classList.toggle('on',state.monitoring);$('monitor-switch').setAttribute('aria-checked',state.monitoring);
  $('stop').hidden=!state.busy&&!state.monitoring;
  $('monitor-badge').hidden=!state.monitoring;
}
function renderState(){
  const connection=$('connection-state');connection.className='status-pill'+(state.connected?' online':'')+(state.busy?' busy':'');
  connection.innerHTML='<i></i>'+ (state.connected?`已连接${state.busy?' · 处理中':''}`:state.busy?'正在连接…':'未连接游戏');
  connection.title=state.pid?`游戏进程 ${state.pid}`:'';
  $('rule-count').textContent=state.rules.filter(r=>r.enabled).length;
  $('operation-message').textContent=state.message+(state.connected&&!state.complete&&!state.busy?' 本次读取不完整，请稍后刷新。':'');
  document.querySelector('.operation-notice').classList.toggle('error',!!state.error);
  $('footer-state').textContent=state.error?state.message:state.connected?'已连接游戏 · 无需切回游戏前台':'本机运行 · 无需切回游戏前台';
  $('last-read').textContent=state.captured_at?`上次读取 ${new Date(state.captured_at).toLocaleTimeString('zh-CN',{hour12:false})}`:'自动监控默认关闭';
  for(const c of ['inventory','storage']){
    const counts=state.counts[c];$(c+'-count').textContent=counts?counts.equipment:'—';
    $(c+'-capacity').textContent=counts?`物品占用 ${counts.occupied} / ${counts.capacity} 格`:'连接后读取';
  }
  $('locked-count').textContent=state.items.length?state.items.filter(i=>i.locked===true).length:'—';
  $('match-count').textContent=state.items.length?state.items.filter(i=>i.matches&&i.locked===false).length:'—';
  $('progress-wrap').hidden=!state.busy||!state.progress;
  if(state.progress){$('operation-progress').max=Math.max(1,state.progress.total);$('operation-progress').value=state.progress.done;$('progress-label').textContent=`${state.progress.done} / ${state.progress.total}`;}
  $('history-list').innerHTML=state.history.length?state.history.map(h=>`<div><time>${esc(h.time)}</time><span>${esc(h.text)}</span></div>`).join(''):'<div class="note">尚无操作记录</div>';
  const rulesSignature=JSON.stringify(state.rules);
  if(rulesSignature!==lastRules){lastRules=rulesSignature;renderCatalog();if(gearKey&&!dirty){const r=rulesFor(gearKey).find(r=>r.id===draft?.id);if(r){draft=makeDraft(r);renderEditor();}}}
  renderInventory();updateControls();
}
async function pollState(){
  if(polling||exiting)return;
  polling=true;
  try{state=await api('state');renderState();}
  catch(error){$('footer-state').textContent='助手服务已断开，请重新双击启动文件。';if(!state)$('operation-message').textContent=error.message;}
  finally{polling=false;}
}
async function action(kind,data={}){
  try{await api('action',{kind,scope:$('action-scope').value,...data});await pollState();}
  catch(error){toast(error.message,true);}
}
async function download(path,name){
  try{const data=await api(path),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
    const anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){toast(error.message,true);}
}

document.querySelectorAll('.tab').forEach(button=>button.addEventListener('click',()=>switchTab(button.dataset.tab)));
$('connect').addEventListener('click',()=>action('connect'));
$('disconnect').addEventListener('click',()=>action('disconnect'));
$('refresh').addEventListener('click',()=>action('read'));
$('equipment-search').addEventListener('input',renderCatalog);$('slot-filter').addEventListener('change',renderCatalog);$('configured-filter').addEventListener('change',renderCatalog);
$('class-filters').addEventListener('click',e=>{const button=e.target.closest('[data-class]');if(!button)return;selectedClass=Number(button.dataset.class);document.querySelectorAll('[data-class]').forEach(b=>b.classList.toggle('selected',b===button));renderCatalog();});
$('equipment-grid').addEventListener('click',e=>{const card=e.target.closest('[data-gear]');if(card)chooseGear(card.dataset.gear);});
$('rule-editor').addEventListener('change',async e=>{
  const input=e.target;
  if(input.dataset.affix){const g=input.dataset.group,key=input.dataset.affix;draft[g]=input.checked?[...new Set([...draft[g],key])]:draft[g].filter(s=>s!==key);input.closest('label').classList.toggle('checked',input.checked);markDirty();}
  if(input.id==='rule-enabled'){draft.enabled=input.checked;markDirty();}
  if(input.id==='secondary-enabled'){draft.secondaryEnabled=input.checked;markDirty();renderEditor();$('draft-status').textContent='有未保存的改动';}
  if(input.id==='existing-rule')chooseGear(gearKey,input.value);
});
$('rule-editor').addEventListener('input',e=>{
  const input=e.target;
  if(input.dataset.count){draft[input.dataset.count+'Count']=Number(input.value);markDirty();}
  if(input.dataset.affixSearch){let q=input.value.trim().toLowerCase();input.closest('.affix-group').querySelectorAll('[data-stat-name]').forEach(c=>c.hidden=!!q&&!c.dataset.statName.includes(q));}
});
$('rule-editor').addEventListener('click',async e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.id==='save-rule')saveRule();
  if(button.id==='reset-rule'){setDirty(false);const r=rulesFor(gearKey).find(r=>r.id===draft.id);draft=makeDraft(r);renderEditor();}
  if(button.id==='new-rule'){if(!dirty||await confirmAction('新增词条组合？','当前未保存的改动将被放弃。','新增',false)){draft=makeDraft(null);setDirty(false);renderEditor();}}
  if(button.id==='delete-rule'&&await confirmAction('删除这条规则？','删除规则不会解锁游戏里的装备。','删除规则')){
    try{await api('rule/delete',{id:draft.id});setDirty(false);draft=makeDraft(null);await pollState();await chooseGear(gearKey);toast('规则已删除');}catch(error){toast(error.message,true);}
  }
});
$('import-rules').addEventListener('click',()=>$('import-file').click());
$('import-file').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file)return;
  try{const payload=JSON.parse((await file.text()).replace(/^\uFEFF/,'')),result=await api('rule/import',{payload});await pollState();toast(`已导入 ${result.imported} 条规则，默认停用`);}catch(error){toast(error.message,true);}finally{e.target.value='';}
});
$('export-rules').addEventListener('click',()=>download('export/rules','Deskrawl筛选规则.json'));
$('export-items').addEventListener('click',()=>download('export/items','Deskrawl装备清单.json'));
$('container-filter').addEventListener('click',e=>{const button=e.target.closest('[data-container]');if(!button)return;containerFilter=button.dataset.container;document.querySelectorAll('[data-container]').forEach(b=>b.classList.toggle('selected',b===button));renderInventory();});
$('item-search').addEventListener('input',renderInventory);$('lock-filter').addEventListener('change',renderInventory);$('action-scope').addEventListener('change',updateControls);
$('item-rows').addEventListener('change',e=>{const id=e.target.dataset.itemSelect;if(!id)return;if(e.target.checked)selectedItems.add(id);else selectedItems.delete(id);renderInventory();});
$('item-rows').addEventListener('click',e=>{if(e.target.matches('input'))return;const row=e.target.closest('[data-item]');if(row){detailId=row.dataset.item;renderInventory();}});
$('item-rows').addEventListener('keydown',e=>{if(e.target.matches('input')||!['Enter',' '].includes(e.key))return;const row=e.target.closest('[data-item]');if(row){e.preventDefault();detailId=row.dataset.item;renderInventory();}});
$('select-visible').addEventListener('change',e=>{visibleItems().filter(i=>i.locked===false).forEach(i=>e.target.checked?selectedItems.add(i.instance_id):selectedItems.delete(i.instance_id));renderInventory();});
$('item-detail').addEventListener('click',async e=>{const button=e.target.closest('[data-configure-item]');if(button&&gearMap.has(button.dataset.configureItem)){await chooseGear(button.dataset.configureItem);switchTab('rules');}});
$('lock-selected').addEventListener('click',()=>action('lock_selected',{items:state.items.filter(i=>selectedItems.has(i.instance_id)).map(i=>({item_uid:i.item_uid,instance_id:i.instance_id,name_key:i.name_key}))}));
$('lock-rules').addEventListener('click',()=>action('lock_rules'));
$('unlock-all').addEventListener('click',async()=>{
  const count=scopedItems().filter(i=>i.locked===true).length,scope=$('action-scope').selectedOptions[0].text;
  if(await confirmAction(`全部解锁 · ${scope}`,`将解锁此范围内的 ${count} 件已锁装备。解锁后，它们可被游戏出售或分解。\n持续监控将自动关闭。`,'解锁这 '+count+' 件装备'))action('unlock_all');
});
$('monitor-switch').addEventListener('click',async()=>{try{await api('monitor',{enabled:!state.monitoring});await pollState();}catch(error){toast(error.message,true);}});
$('stop').addEventListener('click',async()=>{try{await api('stop',{});await pollState();}catch(error){toast(error.message,true);}});
$('history-toggle').addEventListener('click',()=>$('history-list').hidden=!$('history-list').hidden);
$('exit').addEventListener('click',async()=>{
  if(dirty&&!await confirmAction('退出助手？','当前配置还未保存。退出将放弃改动，并停止持续监控。','退出',false))return;
  try{await api('shutdown',{});exiting=true;document.body.innerHTML='<div class="empty-state"><h2>助手已退出</h2><p class="note">持续监控已停止。可关闭此页面，或双击启动文件重新打开。</p></div>';}catch(error){toast(error.message,true);}
});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
async function boot(){
  if(desktopMode)$('usage-note').textContent='监控只自动处理新增装备；已有装备请用“一键按规则锁定”。最小化窗口可继续监控，关闭窗口会退出助手并停止监控。';
  try{catalog=await api('catalog');buildCatalog();await pollState();const first=state.rules[0]?.equipment_keys[0]||'LegendaryBelt3';if(first)await chooseGear(first);setInterval(pollState,1000);}catch(error){toast(error.message,true);}
}
boot();
