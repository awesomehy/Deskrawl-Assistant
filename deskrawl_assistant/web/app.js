'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const desktopMode=new URLSearchParams(location.search).get('desktop')==='1';
let token = document.querySelector('meta[name="assistant-token"]').content;
let catalog, state, gearKey, draft, dirty=false, selectedClass=0, containerFilter='', detailId=null;
let selectedItems=new Set(), itemSignature='', catalogSignature='', polling=false, toastTimer, exiting=false;
const gearMap=new Map(), statMap=new Map();
let lastRules='';
let automationDraft=null, automationDirty=false, collectionSignature='';
let activeTab='rules', logBefore=null, logNext=null, logRequest=0, logTimer;
let experienceData=null, experienceRequest=0, experienceLoading=false, experienceBusy=false, experienceReceivedAt=0, experienceInputTimer;
let recommendationData=null, recommendationRequest=0, recommendationLoading=false, recommendationBusy=false, recommendationDirty=false;

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
  activeTab=tab;
  document.querySelectorAll('.tab').forEach(button=>{let active=button.dataset.tab===tab;button.classList.toggle('active',active);button.setAttribute('aria-selected',active);});
  $('rules-page').hidden=tab!=='rules';$('inventory-page').hidden=tab!=='inventory';$('logs-page').hidden=tab!=='logs';$('experience-page').hidden=tab!=='experience';$('recommendations-page').hidden=tab!=='recommendations';
  if(tab==='inventory')renderInventory();
  if(tab==='logs')loadLogs();
  if(tab==='experience')loadExperience();
  if(tab==='recommendations')loadRecommendations();
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
      <small>${esc(g.slot_label)} · ${esc(classesLabel(g.class_mask))}</small><small class="card-status ${enabled?'':'off'}">${rules.length?`${rules.filter(r=>r.enabled).length} / ${rules.length} 个组合启用`:'未配置规则'}</small></button>`;
  }).join(''):'<div class="empty-state">没有找到符合条件的装备<br>试试其他职业、部位或搜索词</div>';
}
function makeDraft(rule){
  const groups=rule?.groups||{};
  let number=1;while(rulesFor(gearKey).some(r=>r.name===`组合 ${number}`))number++;
  return {id:rule?.id||'',name:rule?.name||`组合 ${number}`,enabled:rule?.enabled??true,
    primary:[...(groups.primary?.selected_stats||[])],primaryCount:groups.primary?.count||1,
    secondary:[...(groups.secondary?.selected_stats||[])],secondaryCount:groups.secondary?.count||1,
    secondaryEnabled:!!groups.secondary,legacy:!!rule&&!rule.groups};
}
async function chooseGear(key,ruleId){
  if(dirty&&!await confirmAction('切换装备配置？','当前配置还没有保存。切换后将放弃这些改动。','放弃改动并切换',false)){renderEditor();return;}
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
    <div class="editor-rule-select"><select id="existing-rule" aria-label="选择词条组合">${!draft.id?'<option value="" selected>新组合 · 尚未保存</option>':''}${existing.map((r,index)=>`<option value="${esc(r.id)}" ${draft.id===r.id?'selected':''}>${index+1}. ${esc(r.name)}${r.enabled?' · 已启用':' · 已停用'}</option>`).join('')}</select><button id="new-rule" class="button ghost">新增组合</button><button id="copy-rule" class="button ghost" ${draft.id?'':'hidden'}>复制组合</button></div>
    <label class="combination-name">组合名称<input id="rule-name" value="${esc(draft.name)}" maxlength="80" placeholder="例如：暴击流、冰伤流" aria-label="组合名称"></label>
    <p class="combination-note">这件装备有 ${existing.length} 个已保存组合，${existing.filter(r=>r.enabled).length} 个启用。满足任意一个启用组合，即符合规则。</p>
    <div class="rule-state"><label><input type="checkbox" id="rule-enabled" ${draft.enabled?'checked':''}>启用当前词条组合</label><span id="draft-status">${draft.id?'已保存':'尚未配置'}</span></div>
    ${draft.legacy?'<div class="inline-error">这是旧版数值规则；保存将改为当前主、副词条数量规则。</div>':''}
    ${groupMarkup('primary','主词条')}<div class="flow-label">↓ 主词条达标后，再检查副词条</div>${groupMarkup('secondary','副词条')}
    <div id="rule-summary" class="rule-summary"></div><div id="rule-error" class="inline-error" hidden></div>
    <p class="note">每个组合独立判断主、副词条。只需所选池中任意 n 类达标；同类型只算一次，基础属性不参与计数。一键锁定和持续监控都会检查所有启用组合。</p></div>
    <div class="editor-actions"><button id="delete-rule" class="text-button" ${draft.id?'':'hidden'}>删除组合</button><span class="spacer"></span><button id="reset-rule" class="button ghost">重置改动</button><button id="save-rule" class="button primary">${draft.id?'保存修改':'保存并应用'}</button></div>`;
  updateSummary();
}
function setDirty(value){dirty=value;if(desktopMode)api('window/draft',{dirty:dirty||automationDirty}).catch(()=>{});}
function markDirty(){setDirty(true);$('draft-status').textContent='有未保存的改动';updateSummary();}
function validation(){
  if(!draft.name.trim())return '请填写组合名称。';
  if(draft.name.trim().length>80)return '组合名称最多 80 个字符。';
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
function scopedItems(){const scope=$('action-scope').value;return state.items.filter(i=>i.is_equipment&&(scope==='all'||i.container===scope));}
function visibleItems(){
  const query=$('item-search').value.trim().toLowerCase(),lock=$('lock-filter').value,kind=$('kind-filter').value;
  return (state?.items||[]).filter(i=>(!containerFilter||i.container===containerFilter)&&
    (!kind||i.kind===kind)&&(!query||(i.name+' '+(gearMap.get(i.name_key)?.en||'')).toLowerCase().includes(query))&&
    (!lock||(i.is_equipment&&(lock==='locked'?i.locked===true:lock==='unlocked'?i.locked===false:i.matches))));
}
function resultBadge(item){
  if(!item.is_equipment)return '<span class="badge miss">—</span>';
  return item.matches?'<span class="badge match">符合规则</span>':item.review?'<span class="badge review">待核验</span>':'<span class="badge miss">未命中</span>';
}
function renderInventory(){
  if(!state)return;
  const view=visibleItems();
  selectedItems=new Set([...selectedItems].filter(id=>state.items.some(i=>i.selection_id===id)));
  $('visible-count').textContent=`${view.length} 组物品`;
  const signature=JSON.stringify([view,detailId,[...selectedItems]]);
  if(signature!==itemSignature){
    itemSignature=signature;
    $('item-rows').innerHTML=view.map(i=>`<tr data-item="${esc(i.selection_id)}" class="${i.selection_id===detailId?'active':''}" tabindex="0">
      <td><input type="checkbox" data-item-select="${esc(i.selection_id)}" aria-label="选择${esc(i.name)}" ${selectedItems.has(i.selection_id)?'checked':''} ${!i.selection_id?'disabled':''}></td>
      <td><div class="item-name">${i.icon?`<img src="${esc(i.icon)}" alt="">`:''}<div><strong>${esc(i.name)}</strong><small>${esc(i.slot_label)}</small></div></div></td>
      <td>${i.container==='inventory'?'背包':'仓库'} · ${Number(i.slot_index)+1}</td><td>${esc(i.count)}</td><td>${esc(i.level??'—')}${i.upgrade?` <small>+${i.upgrade}</small>`:''}</td>
      <td><span class="badge ${i.locked?'locked':'unlocked'}">${i.locked===true?'已锁定':i.locked===false?'未锁定':'未确认'}</span></td><td>${resultBadge(i)}</td></tr>`).join('');
    $('inventory-empty').hidden=view.length>0;
    $('inventory-empty').textContent=state.connected?'当前筛选条件下没有物品':'连接游戏，查看背包和仓库物品';
    renderDetail();
  }
  const available=view.filter(i=>i.selection_id);
  $('select-visible').checked=available.length>0&&available.every(i=>selectedItems.has(i.selection_id));
  $('select-visible').indeterminate=available.some(i=>selectedItems.has(i.selection_id))&&!$('select-visible').checked;
  $('select-visible').disabled=!available.length;
  $('selected-count').textContent=state.items.filter(i=>selectedItems.has(i.selection_id)&&i.is_equipment&&i.locked===false).length;
  $('transfer-selection').textContent=selectedItems.size?`已选 ${selectedItems.size} 组物品`:'勾选物品后移动';
  updateControls();
}
function renderDetail(){
  const item=state.items.find(i=>i.selection_id===detailId);
  if(!item){$('item-detail').innerHTML='<div class="empty-state">点击物品查看详情</div>';return;}
  const moveButton=`<button class="button detail-move" data-move-item="${esc(item.selection_id)}" data-target="${item.container==='inventory'?'storage':'inventory'}" ${!item.movable||state.busy?'disabled':''}>${item.container==='inventory'?'移入仓库':'取回背包'}</button>`;
  if(!item.is_equipment){$('item-detail').innerHTML=`<div class="detail-title">${item.icon?`<img src="${esc(item.icon)}" alt="">`:`<div class="item-placeholder">${esc(item.kind_label[0])}</div>`}<div><h3>${esc(item.name)}</h3><small>${esc(item.kind_label)}</small></div></div><div class="detail-meta">数量 ${esc(item.count)} · ${item.container==='inventory'?'背包':'仓库'}第 ${Number(item.slot_index)+1} 格</div>${renderEffects(item)}<p class="detail-hint">${esc(item.effect_note||'')}</p><p class="detail-hint">${item.kind==='gem'?`同种同等级宝石优先堆叠${item.max_stack>1?`，每组最多 ${esc(item.max_stack)} 颗`:''}，剩余数量放入空格。`:'整组移动到目标空格，保留原有数量。'}不参与装备词条筛选。</p>${moveButton}`;return;}
  $('item-detail').innerHTML=`<div class="detail-title">${item.icon?`<img src="${esc(item.icon)}" alt="">`:''}<div><h3>${esc(item.name)}</h3><small>${esc(item.slot_label)} · ${esc(classesLabel(item.class_mask))}</small></div></div>
    <div class="detail-meta"><span>物品等级 ${esc(item.level)}</span><span>强化 +${esc(item.upgrade??0)}</span></div>
    ${['base','primary','secondary','unknown'].map(g=>{const values=item.groups[g]||[];return values.length?`<div class="detail-group"><h4>${{base:'基础属性 · 不参与筛选',primary:'主词条',secondary:'副词条',unknown:'待核验词条'}[g]}</h4><div class="type-list">${values.map(v=>`<div class="affix-row ${v.matched?'matched':''}" data-affix-key="${esc(v.key)}" title="${esc(v.value_note||(v.matched?'命中规则：'+v.matched_rules.join('、'):''))}"><span class="affix-name">${esc(v.name)}</span><strong class="affix-value">${esc(v.display_value??'未读取')}</strong>${v.matched?'<small class="affix-hit" aria-label="命中规则">✓</small>':''}</div>`).join('')}</div></div>`:'';}).join('')}
    <p class="detail-hint">绿色为命中已启用规则的词条；整件装备是否达标见下方结果。数值包含已确认的装备强化。</p>
    <div class="detail-rule">${resultBadge(item)}${item.matched_combinations?.length?`<p class="matched-combinations">命中组合：${item.matched_combinations.map(esc).join('、')}</p>`:''}${item.reasons.map(r=>`<p>${esc(r)}</p>`).join('')}<button class="text-button" data-configure-item="${esc(item.name_key)}">配置这件装备的规则 →</button></div>${moveButton}`;
}
function renderEffects(item){
  const plain=s=>String(s??'').replace(/<[^>]+>/g,'');
  return (item.effect_groups||[]).map(g=>`<div class="detail-group item-effect"><h4>${esc(g.title)}</h4>${(g.values||[]).map(v=>`<div class="effect-row"><div><span>${esc(v.name)}</span><strong>${esc(v.display_value)}</strong></div>${v.growth?`<small>${esc(v.growth)}</small>`:''}</div>`).join('')}${(g.texts||[]).map(t=>`<p>${esc(plain(t))}</p>`).join('')}</div>`).join('');
}
function readAutomationForm(){
  return {carriage:{enabled:$('auto-carriage').checked,items:[...automationDraft.carriage.items]},gems:{enabled:$('auto-gems').checked},pressure:{enabled:$('auto-pressure').checked,threshold:Number($('pressure-threshold').value),target_free:Number($('pressure-target').value),equipment_filter:$('pressure-filter').value}};
}
function markAutomationDirty(){
  automationDraft=readAutomationForm();automationDirty=JSON.stringify(automationDraft)!==JSON.stringify(state.automation.settings);
  setDirty(dirty);
  $('automation-draft-state').textContent=automationDirty?'有未保存的设置':'设置已保存';
  $('run-automation').disabled=!state.automation.running&&(!state.connected||state.busy||automationDirty||!Object.values(automationDraft).some(v=>v.enabled));
}
function filteredCollectionChoices(){
  const query=$('carriage-search').value.trim().toLowerCase(),kind=$('carriage-kind').value;
  return catalog.collectibles.filter(i=>(!kind||i.kind===kind)&&(!query||(i.name+' '+i.key).toLowerCase().includes(query)));
}
function setFilteredCollectionSelection(checked){
  if(!automationDraft)return;
  const choices=filteredCollectionChoices();if(!choices.length)return;
  const selected=new Set(automationDraft.carriage.items);
  choices.forEach(i=>checked?selected.add(i.key):selected.delete(i.key));
  automationDraft.carriage.items=[...selected];
  markAutomationDirty();renderCollectionChoices();
}
function renderCollectionChoices(){
  if(!automationDraft)return;
  const selected=new Set(automationDraft.carriage.items);
  const choices=filteredCollectionChoices().sort((a,b)=>Number(selected.has(b.key))-Number(selected.has(a.key))||a.name.localeCompare(b.name,'zh-CN'));
  const signature=JSON.stringify([choices,automationDraft.carriage.items]);
  if(signature!==collectionSignature){collectionSignature=signature;$('carriage-choices').innerHTML=choices.map(i=>`<label><input type="checkbox" data-collect-key="${esc(i.key)}" ${selected.has(i.key)?'checked':''}><span>${esc(i.name)}</span><small>${{equipment:'装备',gem:'宝石',rune:'符文',chest:'宝箱',material:'其他'}[i.kind]}</small></label>`).join('')||'<p class="note">没有找到物品</p>';}
  $('carriage-selected').textContent=`已选 ${selected.size} 种物品 · 显示 ${choices.length} 种`;
  $('carriage-select-all').disabled=!choices.some(i=>!selected.has(i.key));
  $('carriage-select-none').disabled=!choices.some(i=>selected.has(i.key));
}
function renderAutomation(){
  const a=state.automation;if(!a)return;
  if(!automationDraft||(!automationDirty&&JSON.stringify(automationDraft)!==JSON.stringify(a.settings))){
    automationDraft=JSON.parse(JSON.stringify(a.settings));$('auto-carriage').checked=automationDraft.carriage.enabled;$('auto-gems').checked=automationDraft.gems.enabled;$('auto-pressure').checked=automationDraft.pressure.enabled;$('pressure-threshold').value=automationDraft.pressure.threshold;$('pressure-target').value=automationDraft.pressure.target_free;$('pressure-filter').value=automationDraft.pressure.equipment_filter;renderCollectionChoices();
  }
  $('automation-summary').textContent=(a.running?'运行中':'已暂停')+' · 点击配置';
  $('automation-summary').classList.toggle('running',a.running);
  $('automation-status').textContent=a.status;
  const carriage=a.carriage||{};
  $('carriage-state').textContent=!state.connected?'连接后读取马车':carriage.available?`当前马车 ${carriage.count} 组：${carriage.items.map(i=>i.name+' ×'+i.count).join('、')||'暂无物品'}`:carriage.reason||'当前场景没有马车';
  $('run-automation').textContent=a.running?'暂停自动整理':'启动自动整理';
  $('run-automation').disabled=!a.running&&(!state.connected||state.busy||automationDirty||!Object.values(automationDraft).some(v=>v.enabled));
  $('save-automation').disabled=!automationDirty;
  $('automation-draft-state').textContent=automationDirty?'有未保存的设置':'设置已保存';
}
function updateControls(){
  if(!state)return;
  const available=state.connected&&!state.busy&&state.complete,enabled=state.rules.some(r=>r.enabled);
  $('connect').hidden=state.connected;$('connect').disabled=state.busy;
  $('disconnect').hidden=!state.connected;$('disconnect').disabled=state.busy;
  $('refresh').disabled=!state.connected||state.busy;
  $('unlock-all').disabled=!available||!scopedItems().some(i=>i.locked===true);
  $('lock-selected').disabled=!available||!state.items.some(i=>selectedItems.has(i.selection_id)&&i.is_equipment&&i.locked===false);
  for(const target of ['storage','inventory']){const items=state.items.filter(i=>selectedItems.has(i.selection_id)&&i.container!==target);$('move-'+target+'-count').textContent=items.length;$('move-'+target).disabled=!available||!state.transfer?.available||!items.length||items.some(i=>!i.movable);}
  $('transfer-hint').textContent=state.connected&&!state.transfer?.available?(state.transfer?.reason||'移动条件尚未核验，请刷新物品。'):'优先堆叠同种同等级宝石；满堆后使用已开放的空格。';
  $('lock-rules').disabled=!available||!enabled;
  $('lock-rules').title=enabled?'按已启用规则处理批量范围内的装备':'请先在规则页保存并启用至少一条规则';
  $('export-items').disabled=!state.items.length;
  $('monitor-switch').disabled=!state.connected||(!state.monitoring&&(state.busy||!state.complete||!enabled));
  $('monitor-switch').title=!state.connected?'请先连接游戏':!enabled?'请先保存并启用至少一条规则':'监控背包和仓库里的新增装备';
  $('monitor-switch').classList.toggle('on',state.monitoring);$('monitor-switch').setAttribute('aria-checked',state.monitoring);
  $('stop').hidden=!state.busy&&!state.monitoring&&!state.automation?.running;
  $('monitor-badge').hidden=!state.monitoring&&!state.automation?.running;
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
  $('locked-count').textContent=state.items.length?state.items.filter(i=>i.is_equipment&&i.locked===true).length:'—';
  $('match-count').textContent=state.items.length?state.items.filter(i=>i.matches&&i.locked===false).length:'—';
  $('progress-wrap').hidden=!state.busy||!state.progress;
  if(state.progress){$('operation-progress').max=Math.max(1,state.progress.total);$('operation-progress').value=state.progress.done;$('progress-label').textContent=`${state.progress.done} / ${state.progress.total}`;}
  $('history-list').innerHTML=state.history.length?state.history.map(h=>`<div><time>${esc(h.time)}</time><span>${esc(h.text)}</span></div>`).join(''):'<div class="note">尚无操作记录</div>';
  const rulesSignature=JSON.stringify(state.rules);
  if(rulesSignature!==lastRules){lastRules=rulesSignature;renderCatalog();if(gearKey&&!dirty){const r=rulesFor(gearKey).find(r=>r.id===draft?.id);if(r){draft=makeDraft(r);renderEditor();}}}
  renderInventory();updateControls();renderAutomation();
  $('logging-state').textContent=state.connected?'已连接 · 每 2 秒记录物品变化':'连接游戏后开始记录物品变化';
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
  if(input.id==='rule-name'){draft.name=input.value;markDirty();}
  if(input.dataset.count){draft[input.dataset.count+'Count']=Number(input.value);markDirty();}
  if(input.dataset.affixSearch){let q=input.value.trim().toLowerCase();input.closest('.affix-group').querySelectorAll('[data-stat-name]').forEach(c=>c.hidden=!!q&&!c.dataset.statName.includes(q));}
});
$('rule-editor').addEventListener('click',async e=>{
  const button=e.target.closest('button');if(!button)return;
  if(button.id==='save-rule')saveRule();
  if(button.id==='reset-rule'){setDirty(false);const r=rulesFor(gearKey).find(r=>r.id===draft.id);draft=makeDraft(r);renderEditor();}
  if(['new-rule','copy-rule'].includes(button.id)){
    if(!dirty||await confirmAction('新增词条组合？','当前未保存的改动将被放弃。','继续',false)){
      if(button.id==='copy-rule'){const source=rulesFor(gearKey).find(r=>r.id===draft.id);draft=makeDraft(source);draft.id='';draft.name=(draft.name+' · 副本').slice(0,80);}
      else draft=makeDraft(null);
      setDirty(true);renderEditor();$('draft-status').textContent='有未保存的改动';
    }
  }
  if(button.id==='delete-rule'&&await confirmAction('删除这个词条组合？','只删除当前组合；这件装备的其他组合保留。','删除组合')){
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
$('item-search').addEventListener('input',renderInventory);$('lock-filter').addEventListener('change',renderInventory);$('kind-filter').addEventListener('change',renderInventory);$('action-scope').addEventListener('change',updateControls);
$('item-rows').addEventListener('change',e=>{const id=e.target.dataset.itemSelect;if(!id)return;if(e.target.checked)selectedItems.add(id);else selectedItems.delete(id);renderInventory();});
$('item-rows').addEventListener('click',e=>{if(e.target.matches('input'))return;const row=e.target.closest('[data-item]');if(row){detailId=row.dataset.item;renderInventory();}});
$('item-rows').addEventListener('keydown',e=>{if(e.target.matches('input')||!['Enter',' '].includes(e.key))return;const row=e.target.closest('[data-item]');if(row){e.preventDefault();detailId=row.dataset.item;renderInventory();}});
$('select-visible').addEventListener('change',e=>{visibleItems().filter(i=>i.selection_id).forEach(i=>e.target.checked?selectedItems.add(i.selection_id):selectedItems.delete(i.selection_id));renderInventory();});
for(const target of ['storage','inventory'])$('move-'+target).addEventListener('click',()=>action('move_items',{target,items:state.items.filter(i=>selectedItems.has(i.selection_id)&&i.container!==target).map(i=>i.selection_id)}));
$('item-detail').addEventListener('click',async e=>{const move=e.target.closest('[data-move-item]');if(move){await action('move_items',{target:move.dataset.target,items:[move.dataset.moveItem]});return;}const button=e.target.closest('[data-configure-item]');if(button&&gearMap.has(button.dataset.configureItem)){await chooseGear(button.dataset.configureItem);switchTab('rules');}});
$('lock-selected').addEventListener('click',()=>action('lock_selected',{items:state.items.filter(i=>selectedItems.has(i.selection_id)&&i.is_equipment&&i.locked===false).map(i=>({item_uid:i.item_uid,instance_id:i.instance_id,name_key:i.name_key}))}));
$('lock-rules').addEventListener('click',()=>action('lock_rules'));
$('unlock-all').addEventListener('click',async()=>{
  const count=scopedItems().filter(i=>i.locked===true).length,scope=$('action-scope').selectedOptions[0].text;
  if(await confirmAction(`全部解锁 · ${scope}`,`将解锁此范围内的 ${count} 件已锁装备。解锁后，它们可被游戏出售或分解。\n持续监控将自动关闭。`,'解锁这 '+count+' 件装备'))action('unlock_all');
});
$('monitor-switch').addEventListener('click',async()=>{try{await api('monitor',{enabled:!state.monitoring});await pollState();}catch(error){toast(error.message,true);}});
$('automation-panel').addEventListener('change',e=>{const key=e.target.dataset.collectKey;if(key){const selected=new Set(automationDraft.carriage.items);e.target.checked?selected.add(key):selected.delete(key);automationDraft.carriage.items=[...selected];}if(e.target.id==='carriage-kind'){renderCollectionChoices();return;}markAutomationDirty();renderCollectionChoices();});
$('carriage-search').addEventListener('input',renderCollectionChoices);
$('carriage-select-all').addEventListener('click',()=>setFilteredCollectionSelection(true));
$('carriage-select-none').addEventListener('click',()=>setFilteredCollectionSelection(false));
for(const id of ['pressure-threshold','pressure-target'])$(id).addEventListener('input',markAutomationDirty);
$('save-automation').addEventListener('click',async()=>{try{const result=await api('automation/settings',{settings:readAutomationForm()});automationDraft=result.settings;automationDirty=false;setDirty(dirty);await pollState();toast('自动整理设置已保存');}catch(error){toast(error.message,true);}});
$('run-automation').addEventListener('click',async()=>{try{await api('automation/run',{enabled:!state.automation.running});await pollState();}catch(error){toast(error.message,true);}});
$('stop').addEventListener('click',async()=>{try{await api('stop',{});await pollState();}catch(error){toast(error.message,true);}});
$('history-toggle').addEventListener('click',()=>switchTab('logs'));
$('exit').addEventListener('click',async()=>{
  if((dirty||automationDirty)&&!await confirmAction('退出助手？','当前配置还未保存。退出将放弃改动，并停止持续监控和自动整理。','退出',false))return;
  try{await api('shutdown',{});exiting=true;document.body.innerHTML='<div class="empty-state"><h2>助手已退出</h2><p class="note">持续监控已停止。可关闭此页面，或双击启动文件重新打开。</p></div>';}catch(error){toast(error.message,true);}
});
window.addEventListener('beforeunload',e=>{if(dirty||automationDirty){e.preventDefault();e.returnValue='';}});
async function boot(){
  if(desktopMode)$('usage-note').textContent='监控只自动处理新增装备；已有装备请用“一键按规则锁定”。最小化窗口可继续监控，关闭窗口会退出助手并停止监控。';
  try{catalog=await api('catalog');buildCatalog();await pollState();const first=state.rules[0]?.equipment_keys[0]||'LegendaryBelt3';if(first)await chooseGear(first);setInterval(pollState,1000);setInterval(()=>{if(activeTab==='logs'&&!logBefore)loadLogs(true);},2000);}catch(error){toast(error.message,true);}
}
const logCategories={loot:'物品获取',lock:'锁定 / 解锁',transfer:'物品转移',settings:'配置操作',system:'运行操作',error:'失败'};
function logPosition(position){
  if(!position)return '—';
  const label={inventory:'背包',storage:'仓库',carriage:'马车'}[position.container]||position.container||'未确认';
  const slot=position.slot_index==null?'':`第 ${Number(position.slot_index)+1} ${position.container==='carriage'?'组':'格'}`;
  const counts=position.count_before==null?'':`<small>数量 ${esc(position.count_before)}${position.count_after==null?' · 结果未确认':` → ${esc(position.count_after)}`}</small>`;
  return `<span>${esc(label)} ${slot}</span>${counts}`;
}
async function loadLogs(quiet=false){
  const request=++logRequest,params=new URLSearchParams({category:$('log-category').value,query:$('log-search').value.trim(),limit:'50'});
  if(logBefore)params.set('before',logBefore);
  try{
    const result=await api('logs?'+params);if(request!==logRequest)return;
    logNext=result.next_before;$('log-total').textContent=`共 ${result.total} 条记录`;
    $('log-older').disabled=!logNext;$('log-latest').disabled=!logBefore;
    $('log-empty').hidden=result.entries.length>0;
    $('log-rows').innerHTML=result.entries.map(row=>{
      const date=new Date(row.time),context=row.context||{},method=context.automatic?'自动':context.automatic===false?'手动':'';
      const matched=context.matched_combinations?.length?`命中：${context.matched_combinations.join('、')}`:'';
      return `<tr class="log-${esc(row.category)}"><td><time>${date.toLocaleTimeString('zh-CN',{hour12:false})}<small>${date.toLocaleDateString('zh-CN')}</small></time></td><td><span class="badge ${row.category==='error'?'review':row.category==='loot'?'match':'miss'}">${esc(logCategories[row.category])}</span>${method?`<small>${method}</small>`:''}</td><td><strong>${esc(row.item_name||'—')}</strong></td><td class="log-quantity">${row.quantity==null?'—':(context.quantity_requested?'请求 ':'')+esc(row.quantity)}</td><td>${logPosition(row.source)}</td><td>${logPosition(row.destination)}</td><td>${esc(row.message)}${matched?`<small>${esc(matched)}</small>`:''}</td></tr>`;
    }).join('');
    $('log-error').hidden=true;
  }catch(error){if(request!==logRequest)return;$('log-error').textContent=error.message;$('log-error').hidden=false;if(!quiet)toast(error.message,true);}
}
function resetLogs(){logBefore=null;loadLogs();}
$('log-category').addEventListener('change',resetLogs);
$('log-search').addEventListener('input',()=>{clearTimeout(logTimer);logTimer=setTimeout(resetLogs,300);});
$('log-refresh').addEventListener('click',()=>loadLogs());
$('log-latest').addEventListener('click',resetLogs);
$('log-older').addEventListener('click',()=>{if(logNext){logBefore=logNext;loadLogs();}});
const experienceNumber=new Intl.NumberFormat('zh-CN',{maximumFractionDigits:2});
function formatExperience(value){return Number.isFinite(Number(value))?experienceNumber.format(Number(value)):'—';}
function formatExperienceTime(value){
  const seconds=Math.max(0,Number(value)||0),whole=Math.floor(seconds);
  if(seconds<60)return `${formatExperience(seconds)} 秒`;
  const hours=Math.floor(whole/3600),minutes=Math.floor(whole%3600/60),rest=whole%60;
  return `${hours?hours+' 小时 ':''}${minutes?minutes+' 分 ':''}${rest?rest+' 秒':''}`.trim();
}
function experienceMinutes(){
  const field=$('experience-minutes'),minutes=Number(field.value);
  return field.value!==''&&Number.isInteger(minutes)&&minutes>=1&&minutes<=10080?minutes:null;
}
function experienceElapsed(){
  if(!experienceData?.active)return 0;
  return Math.max(0,Number(experienceData.active.elapsed_seconds)||0)+(experienceData.active.stopped?0:(Date.now()-experienceReceivedAt)/1000);
}
function updateExperienceTimer(){
  if(exiting||!$('experience-elapsed'))return;
  const seconds=Math.floor(experienceElapsed()),hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60),rest=seconds%60;
  $('experience-elapsed').textContent=[...(hours?[String(hours).padStart(2,'0')]:[]),String(minutes).padStart(2,'0'),String(rest).padStart(2,'0')].join(':');
}
function renderExperience(){
  if(!experienceData||exiting)return;
  const data=experienceData,minutes=data.minutes||experienceMinutes()||60,rows=data.rows||[],best=rows[0],active=data.active,live=data.live||{},completed=(data.sort||$('experience-sort').value)==='completed';
  const filter=$('experience-filter'),selected=filter.value;
  const profiles=[...new Set(data.profiles||[])].filter(Boolean);
  if(selected&&!profiles.includes(selected))profiles.push(selected);
  filter.innerHTML='<option value="">全部条件</option>'+profiles.map(profile=>`<option value="${esc(profile)}" ${profile===selected?'selected':''}>${esc(profile)}</option>`).join('');
  $('experience-best').textContent=best?.stage||'—';
  $('experience-best-profile').textContent=best?`${best.profile||'未标注条件'} · ${formatExperience(best.samples)} 次采样`:'等待采样';
  $('experience-hourly').textContent=best?formatExperience(best.xp_per_hour):'—';
  $('experience-projection').textContent=best?formatExperience(completed?best.completed_runs_xp:best.projected_xp):'—';
  $('experience-projection-label').textContent=`${minutes} 分钟${completed?'完整通关':'预计'}经验`;
  $('experience-projection-note').textContent=completed?'仅计时限内完整通关的经验':'按实测平均速度估算';
  $('experience-samples').textContent=formatExperience(rows.reduce((sum,row)=>sum+(Number(row.samples)||0),0));
  $('experience-coverage').textContent=rows.length?`${new Set(rows.map(row=>row.stage)).size} 个关卡 · ${rows.length} 个关卡 / 条件`:'尚未记录关卡';
  $('experience-count').textContent=`${rows.length} 个关卡 / 条件`;
  $('experience-ranking-note').textContent=`按 ${minutes} 分钟${completed?'完整通关':'预计'}经验从高到低排列${selected?' · '+selected:''}。`;
  $('experience-projected-heading').textContent=`${minutes} 分钟预计`;
  $('experience-completed-heading').textContent=`${minutes} 分钟完整通关`;
  $('experience-rows').innerHTML=rows.map((row,index)=>`<tr class="${index===0?'experience-leading':''}"><td><div class="experience-stage-cell"><span class="experience-rank">${index+1}</span><div><strong>${esc(row.stage)}</strong><small>${esc(row.profile||'未标注条件')}</small></div></div></td><td>${formatExperience(row.samples)}${Number(row.samples)===1?'<small>单次样本</small>':''}</td><td>${formatExperience(row.average_xp)}</td><td>${esc(formatExperienceTime(row.average_seconds))}</td><td>${formatExperience(row.xp_per_hour)}</td><td class="${completed?'':'experience-value'}">${formatExperience(row.projected_xp)}</td><td class="${completed?'experience-value':''}">${formatExperience(row.completed_runs_xp)}<small>${formatExperience(row.completed_runs)} 次完整通关</small></td></tr>`).join('');
  $('experience-empty').hidden=rows.length>0;
  $('experience-empty').innerHTML=selected?'这个条件下还没有经验样本<br>尝试其他条件，或录入这个条件的关卡数据。':'还没有经验样本<br>完成一次计时采样，或手动录入关卡经验与用时，开始比较。';
  $('experience-live-dot').classList.toggle('available',!!live.available);
  $('experience-live-state').textContent=live.available?'游戏经验已连接':'暂时无法自动读取经验';
  $('experience-live-detail').textContent=live.available?[live.character,live.stage?`关卡：${live.stage}`:'',live.total_xp!=null?`累计经验 ${formatExperience(live.total_xp)}`:''].filter(Boolean).join(' · ')||'开始与结束采样时自动读取经验。':(live.error||'未连接时仍可手动录入经验。');
  $('experience-use-current').disabled=!live.stage||!!active||experienceBusy;
  if(active){$('experience-stage').value=active.stage;$('experience-profile').value=active.profile||'';}
  $('experience-stage').disabled=!!active||experienceBusy;
  $('experience-profile').disabled=!!active||experienceBusy;
  $('experience-start').hidden=!!active;
  $('experience-finish').hidden=!active;
  $('experience-cancel').hidden=!active;
  $('experience-finish-field').hidden=!active;
  $('experience-finish-help').textContent=active?.auto_xp===false?'开始时没有经验基线，请手动填写本次获得经验。':'自动读取时可留空；也可手动填写覆盖自动结果。';
  $('experience-finish-xp').placeholder=active?.auto_xp===false?'填写实际获得经验':'自动读取，或填写实际经验';
  $('experience-timer-label').textContent=active?(active.stopped?'已停止，待补录':'正在采样'):'准备开始';
  $('experience-finish').textContent=active?.stopped?'补录并保存':'结束并保存';
  $('experience-timer-form').classList.toggle('sampling',!!active);
  $('experience-active-label').textContent=active?`${active.stage} · ${active.profile||'未标注条件'}`:'每次记录一个关卡的实际收益';
  $('experience-timer-note').textContent=active?(active.stopped?'计时已停止。请填写本次获得经验并保存，这段补录时间不会计入用时。':live.available&&active.auto_xp!==false?'结算并准备重开后结束。经验可自动读取；手动填写时以填写值为准。':'需要手动填写本次获得经验。也可先点击结束冻结计时，再补录经验。'):'连接游戏可自动记录经验差值。未连接时，结束采样需要填写本次获得经验。';
  for(const id of ['experience-start','experience-finish','experience-cancel','experience-add','experience-refresh'])$(id).disabled=experienceBusy;
  const recent=data.recent||[];
  $('experience-history-count').textContent=recent.length?`显示最近 ${recent.length} 次${selected?' · 当前条件':''}`:'';
  $('experience-history-rows').innerHTML=recent.map(row=>{
    const date=new Date(row.recorded_at),time=Number.isNaN(date.getTime())?esc(row.recorded_at||'—'):date.toLocaleString('zh-CN',{hour12:false});
    const hourly=Number(row.seconds)>0?Number(row.xp)*3600/Number(row.seconds):null;
    return `<tr><td><time>${time}</time></td><td><strong>${esc(row.stage)}</strong><small>${esc(row.profile||'未标注条件')}</small></td><td>${formatExperience(row.xp)}</td><td>${esc(formatExperienceTime(row.seconds))}</td><td>${hourly==null?'—':formatExperience(hourly)}</td><td><button class="text-button experience-delete" data-experience-delete="${esc(row.id)}" type="button" ${experienceBusy?'disabled':''} aria-label="删除${esc(row.stage)}这次采样">删除</button></td></tr>`;
  }).join('');
  $('experience-history-empty').hidden=recent.length>0;
  updateExperienceTimer();
}
async function loadExperience(quiet=false){
  if(exiting||quiet&&experienceLoading)return;
  const minutes=experienceMinutes();
  if(minutes===null){if(!quiet){$('experience-error').textContent='计划刷图时间请输入 1 至 10080 的整数分钟。';$('experience-error').hidden=false;}return;}
  const request=++experienceRequest,params=new URLSearchParams({minutes:String(minutes),sort:$('experience-sort').value}),profile=$('experience-filter').value;
  if(profile)params.set('profile',profile);
  experienceLoading=true;
  try{
    const result=await api('experience?'+params);if(request!==experienceRequest||exiting)return;
    experienceData=result;experienceReceivedAt=Date.now();renderExperience();$('experience-error').hidden=true;
  }catch(error){if(request!==experienceRequest||exiting)return;$('experience-error').textContent=error.message;$('experience-error').hidden=false;if(!quiet)toast(error.message,true);}
  finally{if(request===experienceRequest)experienceLoading=false;}
}
async function mutateExperience(path,payload,message){
  if(experienceBusy)return;
  experienceBusy=true;renderExperience();
  try{await api('experience/'+path,payload);await loadExperience();toast(message);return true;}
  catch(error){if(path==='finish')await loadExperience();toast(error.message,true);return false;}
  finally{experienceBusy=false;renderExperience();}
}
$('experience-refresh').addEventListener('click',()=>loadExperience());
$('experience-filter').addEventListener('change',()=>loadExperience());
$('experience-sort').addEventListener('change',()=>loadExperience());
$('experience-use-current').addEventListener('click',()=>{
  const live=experienceData?.live;if(!live?.stage||experienceData.active||experienceBusy)return;
  $('experience-stage').value=live.stage;$('experience-profile').value=live.profile||live.character||'';
  toast('已填入当前关卡与角色，可补充配装或经验加成条件。');
});
$('experience-minutes').addEventListener('input',()=>{clearTimeout(experienceInputTimer);experienceInputTimer=setTimeout(()=>loadExperience(),350);});
$('experience-timer-form').addEventListener('submit',async e=>{
  e.preventDefault();if(experienceData?.active||experienceBusy)return;
  const stage=$('experience-stage').value.trim();if(!stage){toast('请填写这次采样的关卡。',true);return;}
  if(await mutateExperience('start',{stage,profile:$('experience-profile').value.trim()},'计时已开始；结算并准备重开后结束采样。'))$('experience-finish-xp').value='';
});
$('experience-finish').addEventListener('click',async()=>{
  if(!experienceData?.active||experienceBusy)return;
  const field=$('experience-finish-xp'),payload={};
  if(field.value!==''){
    const xp=Number(field.value);if(!Number.isFinite(xp)||!Number.isInteger(xp)||xp<0){toast('获得经验请输入大于或等于 0 的整数。',true);field.focus();return;}
    payload.xp=xp;
  }
  if(await mutateExperience('finish',payload,'采样已保存，效率排行已更新。'))field.value='';
});
$('experience-cancel').addEventListener('click',async()=>{
  if(!experienceData?.active||experienceBusy)return;
  if(await confirmAction('取消这次采样？','这次计时和经验收益不会保存。之前的样本保留。','取消采样',false))mutateExperience('cancel',{},'本次采样已取消。');
});
$('experience-manual-form').addEventListener('submit',async e=>{
  e.preventDefault();if(experienceBusy)return;
  const stage=$('experience-manual-stage').value.trim(),profile=$('experience-manual-profile').value.trim(),xp=Number($('experience-manual-xp').value),seconds=Number($('experience-manual-seconds').value);
  if(!stage||!Number.isInteger(xp)||xp<0||!Number.isFinite(seconds)||seconds<0.1){toast('请填写关卡、非负整数经验和至少 0.1 秒的用时。',true);return;}
  if(await mutateExperience('sample',{stage,profile,xp,seconds},'经验样本已保存，效率排行已更新。')){$('experience-manual-xp').value='';$('experience-manual-seconds').value='';}
});
$('experience-history-rows').addEventListener('click',async e=>{
  const button=e.target.closest('[data-experience-delete]');if(!button||experienceBusy)return;
  if(await confirmAction('删除这次经验采样？','删除后会重新计算该关卡的经验效率。','删除样本'))mutateExperience('delete',{id:Number(button.dataset.experienceDelete)},'样本已删除，效率排行已更新。');
});
setInterval(()=>{if(!exiting&&activeTab==='experience'&&!experienceBusy)loadExperience(true);},2000);
setInterval(updateExperienceTimer,250);
function recommendationValue(value){return value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value))?Number(value):null;}
function recommendationNumber(value){const number=recommendationValue(value);return number===null?'—':formatExperience(number);}
function recommendationTime(value){const number=recommendationValue(value);return number===null?'—':formatExperienceTime(number);}
function recommendationRunTime(row){
  const seconds=recommendationValue(row?.seconds_per_run);if(seconds===null)return '—';
  if(row.calibrated===true)return recommendationTime(seconds);
  const step=seconds>=300?30:15;return `约 ${recommendationTime(Math.max(step,Math.round(seconds/step)*step))}`;
}
function recommendationPlanXp(row){return recommendationNumber(row.effective_budget_xp??row.completed_runs_xp);}
function recommendationConfidence(value){
  const labels={high:'较高',medium:'中等',low:'较低',unknown:'待确认',calibrated:'实战校准',estimated:'理论估计'};
  if(value==null||value==='')return '待确认';
  if(typeof value==='number')return `${formatExperience(value<=1?value*100:value)}%`;
  return labels[value]||String(value);
}
function recommendationRisk(value){return {low:'较低风险',medium:'中等风险',high:'较高风险',unknown:'风险待确认',observed_failure:'有失败记录',unverified:'生存待校准'}[value]||String(value||'风险待确认');}
function recommendationRiskClass(value){return value==='high'||value==='较高'||value==='observed_failure'?'review':value==='low'||value==='较低'?'match':'unlocked';}
function recommendationDifficulty(value){return (recommendationData?.difficulty_options||[]).find(option=>String(option.value)===String(value))?.label||String(value??'');}
function recommendationBadges(row){
  const available=row.accessible===true,known=typeof row.accessible==='boolean';
  return `<span class="badge ${available?'match':known?'unlocked':'review'}">${available?'已开放':known?'未开放':'开放状态待确认'}</span>${row.boss?'<span class="badge recommendation-boss">Boss 关卡</span>':''}<span class="badge ${row.calibrated===true?'match':'unlocked'}">${row.calibrated===true?'已校准':'预估'}</span>`;
}
function recommendationDate(value){if(!value)return '';const date=new Date(value);return Number.isNaN(date.getTime())?'':date.toLocaleString('zh-CN',{hour12:false});}
function recommendationSettings(){return {minutes:Number($('recommendation-minutes').value),difficulty:$('recommendation-difficulty').value,sort:$('recommendation-sort').value,overhead_seconds:Number($('recommendation-overhead').value),include_locked:$('recommendation-include-locked').checked};}
function markRecommendationDirty(){recommendationDirty=true;$('recommendation-settings-state').textContent='设置有改动，点击“更新推荐”应用。';}
function renderRecommendations(){
  if(!recommendationData||exiting)return;
  const data=recommendationData,settings=data.settings||{},minutes=data.minutes||settings.minutes||60,character=data.character||{},rows=data.rows||[],best=data.available===true&&data.best?.accessible===true?data.best:null,calibration=data.calibration||{},current=data.current||{};
  const options=data.difficulty_options?.length?data.difficulty_options:[{value:'current',label:'跟随当前地图难度'}];
  if(!recommendationDirty){
    $('recommendation-minutes').value=settings.minutes??minutes;
    $('recommendation-overhead').value=settings.overhead_seconds??8;
    $('recommendation-include-locked').checked=settings.include_locked!==false;
    $('recommendation-sort').value=settings.sort||'completed';
    $('recommendation-difficulty').innerHTML=options.map(option=>`<option value="${esc(option.value)}" ${String(option.value)===String(settings.difficulty??data.difficulty??'current')?'selected':''}>${esc(option.label)}</option>`).join('');
    $('recommendation-settings-state').textContent='设置已应用';
  }
  const sort=settings.sort||'completed',eligible=rows.filter(row=>row.accessible===true&&recommendationValue(row.seconds_per_run)>0),minimumRow=eligible.reduce((first,row)=>!first||Number(row.seconds_per_run)<Number(first.seconds_per_run)?row:first,null),minimum=minimumRow?Number(minimumRow.seconds_per_run):null,tooShort=data.available===true&&!best&&minimum!==null&&minimum>minutes*60;
  $('recommendation-read-dot').classList.toggle('available',data.available===true);
  const identity=[character.name,character.class,character.level!=null?`等级 ${recommendationNumber(character.level)}`:''].filter(Boolean);
  $('recommendation-character-name').textContent=identity.join(' · ')||'等待读取当前角色';
  $('recommendation-read-state').textContent=data.available===true?`角色与地图已读取 · ${recommendationDifficulty(data.difficulty||settings.difficulty)} · ${sort==='rate'?'单位时间效率优先':'计划可获经验优先'}`:(data.error||'连接游戏后自动读取角色和地图。');
  const updated=recommendationDate(data.updated_at);$('recommendation-updated').textContent=updated?`更新于 ${updated}`:'';
  $('recommendation-error').hidden=!data.error;$('recommendation-error').textContent=data.error||'';
  const warnings=Array.isArray(data.warnings)?data.warnings:[];
  $('recommendation-warnings').hidden=warnings.length===0;$('recommendation-warnings').innerHTML=warnings.map(warning=>`<p>${esc(warning)}</p>`).join('');
  $('recommendation-best-stage').textContent=best?best.stage:tooShort?'计划时间暂不足':data.available===true?'暂无可推荐地图':'等待读取地图';
  $('recommendation-best-subtitle').textContent=best?`${recommendationDifficulty(best.difficulty||data.difficulty)} · ${minutes} 分钟刷图计划 · ${sort==='rate'?'单位时间效率优先':'计划可获经验优先'}`:tooShort?`最短一轮预计需 ${recommendationRunTime(minimumRow)}，请延长计划时长。`:data.available===true?'没有已确认开放且可计算收益的地图。':(data.error||'无需逐图录入，连接游戏即可开始比较。');
  $('recommendation-best-badges').innerHTML=best?recommendationBadges(best):'';
  $('recommendation-plan-label').textContent=`${minutes} 分钟计划可获经验`;
  $('recommendation-plan-note').textContent=best?.cycle_estimate?'计划可获经验（含失败保留经验）':'按完整通关估算';
  $('recommendation-best-xp').textContent=best?recommendationPlanXp(best):tooShort?'0':'—';
  $('recommendation-best-time').textContent=best?recommendationRunTime(best):tooShort?recommendationRunTime(minimumRow):'—';
  $('recommendation-best-hourly').textContent=best?recommendationNumber(best.xp_per_hour):'—';
  $('recommendation-best-runs').textContent=best?`预计完整通关 ${recommendationNumber(best.completed_runs)} 次`:tooShort?'当前时限内可完成 0 次':'等待计算完成次数';
  $('recommendation-best-overhead').textContent=best?.fixed_seconds!=null?`含每次 ${recommendationNumber(best.fixed_seconds)} 秒固定开销`:'含载入、结算与重开开销';
  $('recommendation-best-reason').textContent=best?[best.calibrated===true?'耗时采用实战校准均值':'按平均伤害粗估，正常刷图自动校准',`${recommendationRisk(best.risk)} · 可信度${recommendationConfidence(best.confidence)}`,...(best.cycle_estimate?['计划可获经验（含失败保留经验）']:[]),...(Array.isArray(best.reasons)?best.reasons:[])].join('；'):tooShort?'时限内尚不能完整通关，计划经验为 0；延长计划后会重新比较。':'推荐结果会标明风险和可信度；没有完整读取数据时不会猜测收益。';
  $('recommendation-current-stage').textContent=current.stage||'等待读取当前地图';
  const phases={combat:'战斗中',normal:'清理怪物',boss:'Boss 战',fighting:'战斗中',cleared:'已通关',completed:'已通关',finished:'已结束',failed:'本次失败',idle:'等待刷图',loading:'载入中',unknown:'状态待确认'};
  $('recommendation-current-phase').textContent=phases[current.phase]||current.phase||'未读取';
  $('recommendation-current-plan-label').textContent='本轮计划总经验（单次全清）';
  $('recommendation-current-xp').textContent=recommendationNumber(current.planned_xp);
  const currentRow=rows.find(row=>String(row.map_id)===String(current.map_id)&&(current.difficulty==null||String(row.difficulty)===String(current.difficulty)));
  $('recommendation-current-note').textContent=currentRow?`地图常规预估：每轮 ${recommendationNumber(currentRow.xp_per_run)} · ${recommendationRunTime(currentRow)} · ${currentRow.calibrated===true?'已校准':'预估'}`:current.stage?'本轮经验以当前生成计划为准；地图常规预估随选择的难度比较。':'读取后可与推荐地图对照。';
  $('recommendation-ranking-note').textContent=`${sort==='rate'?'按单位时间经验效率':'按时限内计划可获经验'}比较，未开放地图不参与当前推荐。`;
  const coverage=data.coverage,supported=recommendationValue(coverage?.supported),total=recommendationValue(coverage?.total);
  $('recommendation-map-count').textContent=`${supported!==null&&total!==null?`支持 ${recommendationNumber(supported)}/${recommendationNumber(total)} 个地图 · `:''}当前 ${rows.length} 个 · 已开放 ${rows.filter(row=>row.accessible===true).length} 个`;
  $('recommendation-table-plan').textContent=`${minutes} 分钟计划收益`;
  $('recommendation-rows').innerHTML=rows.map(row=>{
    const leading=best&&String(row.map_id)===String(best.map_id)&&String(row.difficulty)===String(best.difficulty),reasons=Array.isArray(row.reasons)?row.reasons:[];
    const extra=[row.calibrated===true?'耗时采用实战校准均值。':'按平均伤害粗估，正常刷图自动校准。',...reasons,...(row.cycle_estimate?[`计划可获经验（含失败保留经验）：${recommendationPlanXp(row)}`,`完整通关经验：${recommendationNumber(row.completed_runs_xp)}`]:[]),`速度折算经验：${recommendationNumber(row.projected_xp)}`,row.fixed_seconds!=null?`固定开销：${recommendationNumber(row.fixed_seconds)} 秒`:'',row.samples?`已校准样本：${recommendationNumber(row.samples)} 次`:''].filter(Boolean);
    return `<tr class="${leading?'experience-leading':''} ${row.accessible===false?'recommendation-locked':''}"><td><strong>${esc(row.stage||'未命名地图')}</strong><small>${esc(recommendationDifficulty(row.difficulty))}</small><div class="recommendation-badges">${recommendationBadges(row)}</div></td><td><strong>${recommendationNumber(row.xp_per_run)}</strong><small>${esc(recommendationRunTime(row))}</small></td><td>${recommendationNumber(row.xp_per_hour)}</td><td class="experience-value">${recommendationPlanXp(row)}${row.cycle_estimate?'<small>含失败保留经验</small>':''}</td><td>${recommendationNumber(row.completed_runs)}</td><td><span class="badge ${recommendationRiskClass(row.risk)}">${esc(recommendationRisk(row.risk))}</span><small>可信度 ${esc(recommendationConfidence(row.confidence))}</small><details class="recommendation-reasons"><summary>查看依据</summary>${extra.map(reason=>`<p>${esc(reason)}</p>`).join('')}</details></td></tr>`;
  }).join('');
  $('recommendation-empty').hidden=rows.length>0;$('recommendation-empty').textContent=data.error||'当前没有可比较的地图。连接游戏后自动读取，无需逐图录入。';
  const count=recommendationValue(calibration.samples);
  $('recommendation-calibration-samples').textContent=count===null?'尚无校准记录':`已记录 ${recommendationNumber(count)} 次有效样本`;
  const statuses={observing:'正在观察',active:'正在观察',waiting:'等待完整战斗',idle:'等待观察',calibrated:'已校准',unavailable:'等待连接',paused:'已暂停'};
  $('recommendation-calibration-status').textContent=statuses[calibration.status]||(calibration.active?'正在观察':count>0?'已有校准':'等待观察');
  $('recommendation-calibration-status').className=`badge ${calibration.active?'match':'miss'}`;
  $('recommendation-calibration-detail').textContent=(calibration.status&&!statuses[calibration.status]?calibration.status+' ':'')+(calibration.active?'正常刷图即可。完整有效的战斗会自动更新耗时和经验估计。':'保持助手连接并正常刷图，完整有效的战斗会自动更新地图耗时和经验估计。');
  const last=recommendationDate(calibration.last_recorded_at);$('recommendation-calibration-time').textContent=last?`最近记录：${last}`:'';
  for(const id of ['recommendation-refresh','recommendation-save','recommendation-calibration-reset'])$(id).disabled=recommendationBusy;
  for(const id of ['recommendation-minutes','recommendation-difficulty','recommendation-sort','recommendation-overhead','recommendation-include-locked'])$(id).disabled=recommendationBusy;
}
async function loadRecommendations(quiet=false){
  if(exiting||quiet&&recommendationLoading)return;
  const request=++recommendationRequest;recommendationLoading=true;
  try{const data=await api('recommendations');if(request!==recommendationRequest||exiting)return;recommendationData=data;renderRecommendations();}
  catch(error){if(request!==recommendationRequest||exiting)return;$('recommendation-error').textContent=error.message;$('recommendation-error').hidden=false;$('recommendation-read-state').textContent=recommendationData?'刷新失败，显示上次读取的结果。':'暂时无法读取推荐数据，请重试。';if(!quiet)toast(error.message,true);}
  finally{if(request===recommendationRequest)recommendationLoading=false;}
}
$('recommendation-refresh').addEventListener('click',()=>loadRecommendations());
$('recommendation-settings-form').addEventListener('input',markRecommendationDirty);
$('recommendation-settings-form').addEventListener('change',markRecommendationDirty);
$('recommendation-settings-form').addEventListener('submit',async e=>{
  e.preventDefault();if(recommendationBusy)return;
  const settings=recommendationSettings();
  if(!Number.isInteger(settings.minutes)||settings.minutes<1||settings.minutes>10080||!Number.isFinite(settings.overhead_seconds)||settings.overhead_seconds<0||settings.overhead_seconds>300){toast('请输入 1 至 10080 的整数分钟和 0 至 300 秒的固定开销。',true);return;}
  recommendationBusy=true;renderRecommendations();
  try{await api('recommendations/settings',settings);recommendationDirty=false;await loadRecommendations();toast('刷图计划已更新。');}
  catch(error){toast(error.message,true);}
  finally{recommendationBusy=false;renderRecommendations();}
});
$('recommendation-calibration-reset').addEventListener('click',async()=>{
  if(recommendationBusy)return;
  if(!await confirmAction('重新校准当前配装？','将清除当前配装已积累的校准。正常刷图会重新积累数据，推荐暂时使用预估值。','重新校准',false))return;
  recommendationBusy=true;renderRecommendations();
  try{await api('recommendations/calibration/reset',{});await loadRecommendations();toast('当前配装的校准已重置，正常刷图即可重新积累。');}
  catch(error){toast(error.message,true);}
  finally{recommendationBusy=false;renderRecommendations();}
});
setInterval(()=>{if(!exiting&&activeTab==='recommendations'&&!recommendationBusy)loadRecommendations(true);},3000);
boot();
