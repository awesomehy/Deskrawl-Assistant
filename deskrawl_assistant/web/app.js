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
let updateState=null, updatePolling=false, updateSaving=false, updateNoticeShown=false;

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
  $('rules-page').hidden=tab!=='rules';$('inventory-page').hidden=tab!=='inventory';$('logs-page').hidden=tab!=='logs';
  if(tab==='inventory')renderInventory();
  if(tab==='logs')loadLogs();
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
    setDirty(false);draft.id=result.rule.id;await pollState();renderEditor();toast(draft.enabled?'规则已保存并启用':'规则已保存，当前停用');return true;
  }catch(error){toast(error.message,true);updateSummary();return false;}
}
function scopedItems(){const scope=$('action-scope').value;return state.items.filter(i=>i.is_equipment&&(scope==='all'||i.container===scope));}
function visibleItems(){
  const query=$('item-search').value.trim().toLowerCase(),lock=$('lock-filter').value,kind=$('kind-filter').value;
  return (state?.items||[]).filter(i=>(!containerFilter||i.container===containerFilter)&&
    (!kind||i.kind===kind)&&(!query||(i.name+' '+(gearMap.get(i.name_key)?.en||'')).toLowerCase().includes(query))&&
    (!lock||(i.is_equipment&&(lock==='locked'?i.locked===true:lock==='unlocked'?i.locked===false:lock==='perfect'?i.primary_perfect:i.matches))));
}
function resultBadge(item){
  if(!item.is_equipment)return '<span class="badge miss">—</span>';
  const mark=item.primary_perfect?'<span class="badge primary-perfect" title="四条随机主词条全部命中同一个启用组合，不检查副词条">✦ 主词条全中</span>':'';
  const normal=item.matches?'<span class="badge match">符合规则</span>':item.review?'<span class="badge review">待核验</span>':'<span class="badge miss">未命中</span>';
  return `<span class="result-badges">${mark}${normal}</span>`;
}
function renderInventory(){
  if(!state)return;
  const view=visibleItems();
  selectedItems=new Set([...selectedItems].filter(id=>state.items.some(i=>i.selection_id===id)));
  $('visible-count').textContent=`${view.length} 组物品`;
  const signature=JSON.stringify([view,detailId,[...selectedItems]]);
  if(signature!==itemSignature){
    itemSignature=signature;
    $('item-rows').innerHTML=view.map(i=>`<tr data-item="${esc(i.selection_id)}" class="${i.selection_id===detailId?'active':''} ${i.primary_perfect?'primary-perfect-item':''}" tabindex="0">
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
    <div class="detail-rule">${resultBadge(item)}${item.primary_perfect_combinations?.length?`<p class="perfect-combinations">主词条全中组合：${item.primary_perfect_combinations.map(esc).join('、')}<small>四条随机主词条都在该组合的勾选范围内；此标记不检查副词条，不改变自动锁定条件。</small></p>`:''}${item.matched_combinations?.length?`<p class="matched-combinations">命中组合：${item.matched_combinations.map(esc).join('、')}</p>`:''}${item.reasons.map(r=>`<p>${esc(r)}</p>`).join('')}<button class="text-button" data-configure-item="${esc(item.name_key)}">配置这件装备的规则 →</button></div>${moveButton}`;
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
async function saveAutomation(){try{const result=await api('automation/settings',{settings:readAutomationForm()});automationDraft=result.settings;automationDirty=false;setDirty(dirty);await pollState();toast('自动整理设置已保存');return true;}catch(error){toast(error.message,true);return false;}}
$('save-automation').addEventListener('click',saveAutomation);
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
  try{catalog=await api('catalog');buildCatalog();await pollState();const first=state.rules[0]?.equipment_keys[0]||'LegendaryBelt3';if(first)await chooseGear(first);await pollUpdate();setInterval(pollState,1000);setInterval(pollUpdate,1500);setInterval(()=>{if(activeTab==='logs'&&!logBefore)loadLogs(true);},2000);}catch(error){toast(error.message,true);}
}
function renderUpdate(){
  if(!updateState)return;
  const active=['downloading','installing'].includes(updateState.phase),checking=updateState.phase==='checking';
  $('update-bar').classList.toggle('available',!!updateState.has_update);
  $('update-message').textContent=updateState.message+(active&&updateState.progress?` ${updateState.progress.percent}%`:'');
  $('update-error').textContent=updateState.error||'';$('update-error').hidden=!updateState.error;
  $('update-release').href=updateState.release_url||'https://github.com/awesomehy/Deskrawl-Assistant/releases';
  $('check-update').disabled=active||checking||updateSaving;
  $('check-update').textContent=checking?'正在检查…':'检查更新';
  $('install-update').hidden=!updateState.has_update;
  $('install-update').disabled=!updateState.can_install||active||checking||updateSaving;
  $('install-update').textContent=updateSaving?'正在保存配置…':active?'更新中…':`下载 v${updateState.latest_version} 并更新（重启）`;
  $('install-update').title=updateState.can_install?'下载至当前程序目录，校验后替换当前 exe 并重启；规则和日志保留。':'请在 exe 独立窗口中更新；源码模式可从发行记录下载。';
  $('update-progress').hidden=!active||!updateState.progress;$('update-progress').value=updateState.progress?.percent||0;
  $('rules-page').inert=active||updateSaving;$('automation-panel').inert=active||updateSaving;
  if(updateState.notice&&!updateNoticeShown){updateNoticeShown=true;toast(updateState.notice.message,!updateState.notice.success);}
}
async function pollUpdate(){
  if(updatePolling||exiting)return;updatePolling=true;
  try{updateState=await api('update');renderUpdate();}catch(error){if(updateState?.phase==='installing')$('update-message').textContent='正在替换程序并启动新版本…';}finally{updatePolling=false;}
}
$('check-update').addEventListener('click',async()=>{try{await api('update/check',{});await pollUpdate();}catch(error){toast(error.message,true);}});
$('install-update').addEventListener('click',async()=>{
  if(updateSaving)return;updateSaving=true;renderUpdate();
  try{
    if(dirty&&(!await saveRule()||dirty))throw new Error('更新前请修正规则配置并保存。');
    if(automationDirty&&(!await saveAutomation()||automationDirty))throw new Error('更新前请修正整理设置并保存。');
    if(desktopMode)await api('window/draft',{dirty:false});
    await api('update/install',{saved:true});await pollUpdate();
  }catch(error){toast(error.message,true);}finally{updateSaving=false;renderUpdate();}
});
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
boot();
