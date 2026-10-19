'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const desktopMode=new URLSearchParams(location.search).get('desktop')==='1';
let token = document.querySelector('meta[name="assistant-token"]').content;
let catalog, state, gearKey, draft, dirty=false, selectedClass=0, detailId=null;
let affixClass=null;
let selectedItems=new Set(), itemSignature='', catalogSignature='', polling=false, toastTimer, exiting=false;
const gearMap=new Map(), statMap=new Map();
let lastRules='';
let automationDraft=null, automationDirty=false, collectionSignature='';
let automationPageSignature='';
let activeTab='rules', logBefore=null, logNext=null, logRequest=0, logTimer;
let updateState=null, updatePolling=false, updateSaving=false, updateNoticeShown=false;
let detailTarget=null, characterSignature='', equippedPreviewId=null, equippedPreviewSignature='';
let lootPreviewData=null, lootPreviewLoading=false, lootPreviewRequest=0, lootPreviewSignature='';
let lootCaptureBusy=false;
let columnFilters={containers:null,locks:null,rules:null}, columnMenuKey=null, columnDraft=new Set(), columnAnchor=null;
const columnMenus={
  position:{label:'位置',sort:'position',filter:'containers',options:[['inventory','背包'],['storage','仓库']]},
  level:{label:'物品等级',sort:'level'},upgrade:{label:'强化',sort:'upgrade'},sockets:{label:'总孔数',sort:'sockets'},
  lock:{label:'锁状态',filter:'locks',options:[['locked','已锁定'],['unlocked','未锁定']]},
  power:{label:'换装攻击力',sort:'power'},rules:{label:'规则结果',filter:'rules',options:[['matches','符合规则'],['miss','不符合规则'],['perfect','主词条全中']]}
};
const inventoryFilterIds=['inventory-class','inventory-slot','inventory-rarity','inventory-sockets','inventory-upgrade','inventory-level-min','inventory-level-max','inventory-power','inventory-sort','inventory-ancient','inventory-mist','inventory-divine','inventory-tradable'];

async function api(path,data,retry=true){
  const response=await fetch('/api/'+path,data===undefined?{headers:{'X-Assistant-Token':token}}:{method:'POST',headers:{'Content-Type':'application/json','X-Assistant-Token':token},body:JSON.stringify(data)});
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
function itemNameClass(item){
  if(item?.is_equipment&&(item.rarity===4||item.is_divine===true))return 'item-quality-4';
  if(item?.is_equipment&&item.is_black_mist===true)return 'item-quality-black-mist';
  return Number.isInteger(item?.rarity)&&item.rarity>=0&&item.rarity<=5?'item-quality-'+item.rarity:'item-quality-unknown';
}
function rulesFor(key){return (state?.rules||[]).filter(r=>r.equipment_keys.includes(key));}
function switchTab(tab){
  if(!['rules','inventory','managed','loot-preview','logs','feedback'].includes(tab))return;
  closeColumnMenu();
  activeTab=tab;
  if(tab!=='inventory')window.SteamMarketUI?.update([],false);
  document.querySelectorAll('.tab').forEach(button=>{let active=button.dataset.tab===tab;button.classList.toggle('active',active);button.setAttribute('aria-selected',active);});
  $('rules-page').hidden=tab!=='rules';$('inventory-page').hidden=tab!=='inventory';$('logs-page').hidden=tab!=='logs';
  $('managed-page').hidden=tab!=='managed';
  window.ManagedModeUI?.setActive(tab==='managed');
  $('loot-preview-page').hidden=tab!=='loot-preview';
  $('feedback-page').hidden=tab!=='feedback';
  if(tab==='inventory')renderInventory();
  if(tab==='logs')loadLogs();
  if(tab==='loot-preview'){clearLootPreview('正在读取本趟计划…');loadLootPreview();}
}
function buildCatalog(){
  catalog.equipment.forEach(g=>gearMap.set(g.key,g));catalog.stats.forEach(s=>statMap.set(s.key,s));
  $('class-filters').innerHTML=`<button class="selected" data-class="0">全部职业</button>`+catalog.classes.map(c=>`<button data-class="${c.value}">${esc(c.name)}</button>`).join('');
  $('slot-filter').innerHTML='<option value="">全部部位</option>'+catalog.slots.map(s=>`<option value="${esc(s.key)}">${esc(s.name)}</option>`).join('');
  $('inventory-slot').innerHTML=$('slot-filter').innerHTML;
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
  affixClass=preferredAffixClass(gearMap.get(key));
  gearKey=key;const candidates=rulesFor(key);const rule=candidates.find(r=>r.id===ruleId)||candidates[0];
  draft=makeDraft(rule);setDirty(false);renderCatalog();renderEditor();
  const grid=$('equipment-grid'),active=grid.querySelector('.gear-card.active');
  if(active){const area=grid.getBoundingClientRect(),card=active.getBoundingClientRect();if(card.bottom>area.bottom)grid.scrollTop+=card.bottom-area.bottom+10;else if(card.top<area.top)grid.scrollTop+=card.top-area.top-10;}
}
function preferredAffixClass(gear){
  if(selectedClass&&(gear.class_mask&selectedClass))return selectedClass;
  if(affixClass===0||(affixClass!==null&&(gear.class_mask&affixClass)))return affixClass;
  const character=state?.character,live=character?.available===true?character.class_mask:null;
  if(catalog.classes.some(c=>c.value===live)&&(gear.class_mask&live))return live;
  return catalog.classes.find(c=>gear.class_mask&c.value)?.value??0;
}
function currentAffixPool(group){
  const gear=gearMap.get(gearKey);
  return (catalog.class_pools?.[affixClass]?.[gear.slot]||catalog.pools[gear.slot])?.[group]||[];
}
function affixClassMarkup(){
  if(!catalog.class_pools)return '';
  return `<label class="combination-name affix-class-select">词条池职业<select id="affix-class" aria-label="词条池职业"><option value="0" ${affixClass===0?'selected':''}>全部职业 · 合并查看</option>${catalog.classes.map(c=>`<option value="${c.value}" ${affixClass===c.value?'selected':''}>${esc(c.name)}</option>`).join('')}</select></label><p class="note">按装备生成职业显示随机主词条，副词条遵循游戏原生部位池。切换只改变候选项，已选词条保留。</p>`;
}
function groupMarkup(group,title){
  const gear=gearMap.get(gearKey),optional=group==='secondary',enabled=!optional||draft.secondaryEnabled;
  const pool=currentAffixPool(group),broad=catalog.pools[gear.slot]?.[group]||[],selected=draft[group];
  const options=[...pool,...selected.filter(key=>!pool.includes(key))];
  return `<div class="affix-group ${enabled?'':'disabled'}" data-group="${group}"><div class="group-heading"><div class="group-title"><span class="step">${optional?'2':'1'}</span><h3>${title}</h3></div>
    ${optional?`<label class="group-toggle"><input type="checkbox" id="secondary-enabled" ${enabled?'checked':''}>启用副词条筛选</label>`:'<small>先判断这一组</small>'}</div>
    <div class="count-line">选中的词条，至少命中 <input type="number" min="1" max="${Math.max(1,selected.length)}" value="${draft[group+'Count']}" data-count="${group}" aria-label="${title}至少命中数量" ${enabled?'':'disabled'}> 类<span class="selected-total" data-total="${group}">已选 ${selected.length} 类</span></div>
    <input class="affix-search" data-affix-search="${group}" placeholder="搜索${title}…" aria-label="搜索${title}" ${enabled?'':'disabled'}>
    <div class="affix-options">${options.length?options.map(key=>`<label class="affix-chip ${selected.includes(key)?'checked':''}" data-stat-name="${esc((statMap.get(key)?.name+' '+statMap.get(key)?.en).toLowerCase())}"><input type="checkbox" data-affix="${esc(key)}" data-group="${group}" ${selected.includes(key)?'checked':''} ${enabled?'':'disabled'}>${esc(statMap.get(key)?.name||key)}${!broad.includes(key)?' · 不参与筛选，请取消勾选':!pool.includes(key)?' · 其他职业已选':''}</label>`).join(''):'<div class="note">此部位的本地随机词条池为空，目前无法配置该组条件。</div>'}</div></div>`;
}
function renderEditor(){
  if(!gearKey||!draft)return;
  const gear=gearMap.get(gearKey),existing=rulesFor(gearKey);
  $('rule-editor').innerHTML=`<div class="editor-head">${gearIcon(gear)}<div><div class="eyebrow">传说装备 · ${esc(gear.slot_label)} · ${esc(classesLabel(gear.class_mask))}</div><h2>${esc(gear.name)}</h2><small>${esc(gear.en)}</small></div></div>
    <div class="editor-content">${gear.description?`<div class="legendary-description">${esc(gear.description)}</div>`:''}
    <div class="editor-rule-select"><select id="existing-rule" aria-label="选择词条组合">${!draft.id?'<option value="" selected>新组合 · 尚未保存</option>':''}${existing.map((r,index)=>`<option value="${esc(r.id)}" ${draft.id===r.id?'selected':''}>${index+1}. ${esc(r.name)}${r.enabled?' · 已启用':' · 已停用'}</option>`).join('')}</select></div>
    <div class="combination-toolbar"><button id="new-rule" class="button ghost">新增组合</button><button id="copy-rule" class="button ghost" ${draft.id?'':'hidden'}>复制组合</button><button id="delete-rule" class="button danger" ${draft.id?'':'hidden'}>删除当前</button><button id="cancel-new-rule" class="button ghost" ${!draft.id&&dirty?'':'hidden'}>取消新建</button><button id="manage-rules" class="button ghost" ${existing.length?'':'disabled'}>管理组合</button><button id="copy-to-gear" class="button ghost" ${existing.length?'':'disabled'}>复制到同类装备</button></div>
    <label class="combination-name">组合名称<input id="rule-name" value="${esc(draft.name)}" maxlength="80" placeholder="例如：暴击流、冰伤流" aria-label="组合名称"></label>
    <p class="combination-note">这件装备有 ${existing.length} 个已保存组合，${existing.filter(r=>r.enabled).length} 个启用。满足任意一个启用组合，即符合规则。</p>
    <div class="rule-state"><label><input type="checkbox" id="rule-enabled" ${draft.enabled?'checked':''}>启用当前词条组合</label><span id="draft-status">${dirty?'有未保存的改动':draft.id?'已保存':'尚未配置'}</span></div>
    ${draft.legacy?'<div class="inline-error">这是旧版数值规则；保存将改为当前主、副词条数量规则。</div>':''}
    ${affixClassMarkup()}
    ${groupMarkup('primary','主词条')}<div class="flow-label">↓ 主词条达标后，再检查副词条</div>${groupMarkup('secondary','副词条')}
    <div id="rule-summary" class="rule-summary"></div><div id="rule-error" class="inline-error" hidden></div>
    <p class="note">每个组合独立判断主、副词条。只需所选池中任意 n 类达标；同类型只算一次，基础属性不参与计数。一键锁定和持续监控都会检查所有启用组合。</p></div>
    <div class="editor-actions"><small class="saved-state">${existing.length} 个已保存组合</small><span class="spacer"></span><button id="reset-rule" class="button ghost">重置改动</button><button id="save-rule" class="button primary">${draft.id?'保存修改':'保存并应用'}</button></div>`;
  updateSummary();
}
function hasPendingDrafts(){return dirty||automationDirty||!!window.ManagedModeUI?.isDirty();}
function syncDraftState(){if(desktopMode)api('window/draft',{dirty:hasPendingDrafts()}).catch(()=>{});}
function setDirty(value){dirty=value;syncDraftState();}
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
  const filters={...columnFilters,search:$('item-search').value,lock:$('lock-filter').value,kind:$('kind-filter').value,
    class:$('inventory-class').value,slot:$('inventory-slot').value,rarity:$('inventory-rarity').value,sockets:$('inventory-sockets').value,
    upgrade:$('inventory-upgrade').value,levelMin:$('inventory-level-min').value,levelMax:$('inventory-level-max').value,
    power:$('inventory-power').value,sort:$('inventory-sort').value,
    ancient:$('inventory-ancient').checked,mist:$('inventory-mist').checked,divine:$('inventory-divine').checked,tradable:$('inventory-tradable').checked};
  return InventoryTools.select((state?.items||[]).map(i=>({...i,en:gearMap.get(i.name_key)?.en})),filters,state?.character?.class_mask);
}
function equipmentTags(item){return `<span class="equipment-tags">${(item.tags||[]).map(t=>`<span class="equipment-tag ${t==='远古'?'ancient':t==='黑雾'?'mist':'divine'}">${esc(t)}</span>`).join('')}</span>`;}
function powerNumber(value){return Number.isFinite(value)?Math.round(value).toLocaleString('zh-CN'):'—';}
function powerDelta(value){return (value>0?'+':'')+powerNumber(value);}
function powerClass(delta){return delta>0?'power-up':delta<0?'power-down':'power-equal';}
function powerCell(item){
  if(!item.is_equipment)return '<span class="note">—</span>';
  const p=item.power;
  if(!p?.available)return `<span class="power-unavailable" title="${esc(p?.reason||'等待读取角色')}">${p?.incompatible?'职业不符':p?.level_blocked?`需 Lv.${esc(p.required_level)}`:'暂不能比较'}</span>`;
  return `<div class="power-cell ${powerClass(p.delta)}" title="${esc(p.note)}"><strong>${p.delta>0?'↑ ':p.delta<0?'↓ ':'＝ '}${powerDelta(p.delta)}${p.percent==null?'':` <span>(${p.percent>0?'+':''}${p.percent.toFixed(1)}%)</span>`}</strong><small>换后 ${powerNumber(p.after)}${p.comparisons.length>1?' · '+esc(p.target_label):''}</small></div>`;
}
function renderGems(item){
  if(!item.is_equipment)return '';
  if(!item.gems_known)return '<p class="detail-hint">镶嵌宝石尚未读取</p>';
  const gems=item.gems||[],slots=item.sockets??0;
  return `<div class="detail-group gem-preview"><h4>镶嵌宝石 · ${gems.length} / ${esc(slots)} 孔</h4>${gems.map(g=>`<div class="socketed-gem">${g.icon?`<img src="${esc(g.icon)}" alt="">`:''}<div><strong class="quality-item-name ${itemNameClass(g)}">${esc(g.name)}</strong>${g.effects?g.effects.values.map(v=>`<p><span>${esc(v.name)}</span><b>${esc(v.display_value)}</b></p>`).join('')+(g.effects.texts||[]).map(t=>`<p>${esc(String(t).replace(/<[^>]+>/g,''))}</p>`).join(''):'<p>效果尚未适配</p>'}</div></div>`).join('')}${slots>gems.length?`<p class="detail-hint">${slots-gems.length} 个空孔</p>`:!slots?'<p class="detail-hint">无宝石孔位</p>':''}</div>`;
}
function renderComparison(item){
  const p=item.power;
  if(!p?.available)return `<div class="detail-comparison"><h4>换装预览</h4><p>${esc(p?.reason||'等待读取当前角色。')}</p></div>`;
  const selected=p.comparisons.find(c=>c.target===detailTarget)||p.comparisons.find(c=>c.target===p.target);
  const names=new Map((state.character?.attributes||[]).map(a=>[a.stat,a.name]));
  return `<div class="detail-comparison"><div class="comparison-heading"><h4>换装预览 · 估算</h4>${p.comparisons.length>1?`<select id="detail-ring-target" aria-label="替换戒指位置">${p.comparisons.map(c=>`<option value="${c.target}" ${c.target===selected.target?'selected':''}>${esc(c.target_label)}</option>`).join('')}</select>`:`<small>${esc(selected.target_label)}</small>`}</div><div class="comparison-score"><span>${powerNumber(selected.before)}</span><span>→</span><strong>${powerNumber(selected.after)}</strong></div><div class="comparison-delta ${powerClass(selected.delta)}">${selected.delta>0?'↑ 上升':selected.delta<0?'↓ 下降':'＝ 不变'} ${powerDelta(selected.delta)}${selected.percent==null?'':` (${selected.percent>0?'+':''}${selected.percent.toFixed(2)}%)`}</div>${selected.attributes.length?`<details class="comparison-attributes"><summary>查看属性变化 · ${selected.attributes.length} 项</summary>${selected.attributes.map(a=>`<div><span>${esc(names.get(a.stat)||a.stat)}</span><small>${esc(a.before_text)}</small><b>${esc(a.after_text)}</b></div>`).join('')}</details>`:''}<p class="detail-hint">${esc(p.note)}</p>${(state.character?.warnings||[]).map(w=>`<p class="detail-hint">${esc(w)}</p>`).join('')}</div>`;
}
function renderCharacter(){
  renderEquippedPreview();
  const c=state.character||{available:false,reason:'等待读取角色。'},signature=JSON.stringify([c,equippedPreviewId]);
  if(signature===characterSignature)return;characterSignature=signature;
  $('character-name').textContent=c.available?`${c.name} · ${classesLabel(c.class_mask)} · Lv.${c.level}`:'角色数据暂不可用';
  $('character-status').textContent=c.available?`主属性：${['力量','敏捷','智力'][c.primary]} · 属性采样 ${new Date(c.captured_at).toLocaleTimeString('zh-CN',{hour12:false})} · 换装战力按固定 Boss 场景计算`:c.reason;
  $('character-attack').textContent=c.available?powerNumber(c.attack):'—';
  const values=new Map((c.attributes||[]).map(a=>[a.stat,a]));
  const main=[c.primary,4,5,8,9,13];
  $('character-metrics').innerHTML=c.available?main.map(n=>`<div><span>${esc(values.get(n)?.name||'')}</span><strong>${esc(values.get(n)?.display_value??'—')}</strong></div>`).join('')+`<div><span>生命 / 最大生命</span><strong>${powerNumber(c.health)} / ${powerNumber(c.health_max)}</strong></div><div><span>每秒攻击次数</span><strong>${esc(c.attack_speed)}</strong></div>`:'';
  $('character-warnings').hidden=!c.available||!c.warnings?.length;$('character-warnings').textContent=c.warnings?.join(' ')||'';
  const slotLabels=['武器','头盔','胸甲','裤子','靴子','腰带','戒指 1','戒指 2','项链','护肩','手套','披风'];
  const attributesOpen=$('character-content').querySelector('.all-character-attributes')?.open;
  $('character-content').innerHTML=!c.available?'<div class="empty-state">'+esc(c.reason)+'</div>':`<div class="equipped-grid">${slotLabels.map((label,n)=>{const item=c.equipment.find(i=>i.equipped_slot===n);return item?`<button class="equipped-card ${equippedPreviewId===item.selection_id?'active':''}" data-equipped="${esc(item.selection_id)}"><span class="equipped-slot">${label}</span><div>${item.icon?`<img src="${esc(item.icon)}" alt="">`:''}<strong class="quality-item-name ${itemNameClass(item)}">${esc(item.name)}</strong></div>${equipmentTags(item)}<small>Lv.${esc(item.level)} · +${esc(item.upgrade??0)} · ${item.occupied_sockets}/${esc(item.sockets??'?')} 孔</small></button>`:`<div class="equipped-card empty"><span class="equipped-slot">${label}</span><small>未穿戴</small></div>`;}).join('')}</div><details class="all-character-attributes" ${attributesOpen?'open':''}><summary>全部角色属性 · ${c.attributes.length} 项</summary><div class="character-attribute-grid">${c.attributes.map(a=>`<div><span>${esc(a.name)}</span><strong>${esc(a.display_value)}</strong></div>`).join('')}</div></details>`;
}
function resultBadge(item){
  if(!item.is_equipment)return '<span class="badge miss">—</span>';
  const mark=item.primary_perfect?'<span class="badge primary-perfect" title="四条随机主词条全部命中同一个启用组合，不检查副词条">✦ 主词条全中</span>':'';
  const normal=item.matches?'<span class="badge match">符合规则</span>':item.review?'<span class="badge review">待核验</span>':'<span class="badge miss">未命中</span>';
  return `<span class="result-badges">${mark}${normal}</span>`;
}
function renderInventory(){
  if(!state)return;
  renderColumnButtons();
  const view=visibleItems();
  selectedItems=new Set([...selectedItems].filter(id=>state.items.some(i=>i.selection_id===id)));
  $('visible-count').textContent=`${view.length} 组物品`;
  const signature=JSON.stringify([view,detailId,[...selectedItems]]);
  if(signature!==itemSignature){
    itemSignature=signature;
    $('item-rows').innerHTML=view.map(i=>`<tr data-item="${esc(i.selection_id)}" class="${i.selection_id===detailId?'active':''} ${i.primary_perfect?'primary-perfect-item':''}" tabindex="0">
      <td><input type="checkbox" data-item-select="${esc(i.selection_id)}" aria-label="选择${esc(i.name)}" ${selectedItems.has(i.selection_id)?'checked':''} ${!i.selection_id?'disabled':''}></td>
      <td><div class="item-name">${i.icon?`<img src="${esc(i.icon)}" alt="">`:''}<div><strong class="quality-item-name ${itemNameClass(i)}">${esc(i.name)}</strong><small>${esc(i.slot_label)} ${equipmentTags(i)}</small>${window.SteamMarketUI?.slot(i)||''}</div></div></td>
      <td>${i.container==='inventory'?'背包':'仓库'} · ${Number(i.slot_index)+1}</td><td>${esc(i.count)}</td><td>${esc(i.level??'—')}</td><td>${i.is_equipment?i.upgrade==null?'未确认':'+'+esc(i.upgrade):'—'}</td><td title="已镶嵌 / 总孔位">${i.is_equipment?i.sockets==null?'未确认':(i.gems_known?i.occupied_sockets+'/':'')+i.sockets+' 孔':'—'}</td>
      <td>${i.is_equipment?`<span class="badge ${i.locked?'locked':'unlocked'}">${i.locked===true?'已锁定':i.locked===false?'未锁定':'未确认'}</span>`:'<span class="note">—</span>'}</td><td>${powerCell(i)}</td><td>${resultBadge(i)}</td></tr>`).join('');
    $('inventory-empty').hidden=view.length>0;
    $('inventory-empty').textContent=state.connected?'当前筛选条件下没有物品':'连接游戏，查看背包和仓库物品';
    const previousDetail=$('item-detail').dataset.selection,scroll=$('item-detail').scrollTop,expanded=$('item-detail').querySelector('.comparison-attributes')?.open;
    renderDetail();$('item-detail').dataset.selection=detailId||'';
    if(previousDetail===detailId){$('item-detail').scrollTop=scroll;const attributes=$('item-detail').querySelector('.comparison-attributes');if(attributes)attributes.open=!!expanded;}
  }
  const available=view.filter(i=>i.selection_id);
  $('select-visible').checked=available.length>0&&available.every(i=>selectedItems.has(i.selection_id));
  $('select-visible').indeterminate=available.some(i=>selectedItems.has(i.selection_id))&&!$('select-visible').checked;
  $('select-visible').disabled=!available.length;
  $('selected-count').textContent=state.items.filter(i=>selectedItems.has(i.selection_id)&&i.is_equipment&&i.locked===false).length;
  const hiddenSelection=[...selectedItems].filter(id=>!view.some(i=>i.selection_id===id)).length;
  $('transfer-selection').textContent=selectedItems.size?`已选 ${selectedItems.size} 组${hiddenSelection?`（筛选外 ${hiddenSelection} 组）`:''}`:'勾选物品后移动';
  updateControls();
  window.SteamMarketUI?.update(view,activeTab==='inventory');
}
function renderDetail(){
  const item=state.items.find(i=>i.selection_id===detailId);
  if(!item){$('item-detail').innerHTML='<div class="empty-state">点击物品查看详情</div>';return;}
  const worn=item.container==='equipped';
  const moveButton=worn?'':`<button class="button detail-move" data-move-item="${esc(item.selection_id)}" data-target="${item.container==='inventory'?'storage':'inventory'}" ${!item.movable||state.busy?'disabled':''}>${item.container==='inventory'?'移入仓库':'取回背包'}</button>`;
  if(!item.is_equipment){$('item-detail').innerHTML=`<div class="detail-title">${item.icon?`<img src="${esc(item.icon)}" alt="">`:`<div class="item-placeholder">${esc(item.kind_label[0])}</div>`}<div><h3 class="quality-item-name ${itemNameClass(item)}">${esc(item.name)}</h3><small>${esc(item.kind_label)}</small></div></div><div class="detail-meta">数量 ${esc(item.count)} · ${item.container==='inventory'?'背包':'仓库'}第 ${Number(item.slot_index)+1} 格</div>${window.SteamMarketUI?.slot(item,true)||''}${renderEffects(item)}<p class="detail-hint">${esc(item.effect_note||'')}</p><p class="detail-hint">${item.kind==='gem'?`同种同等级宝石优先堆叠${item.max_stack>1?`，每组最多 ${esc(item.max_stack)} 颗`:''}，剩余数量放入空格。`:'整组移动到目标空格，保留原有数量。'}不参与装备词条筛选。</p>${moveButton}`;window.SteamMarketUI?.paint();return;}
  $('item-detail').innerHTML=equipmentPreviewHtml(item,moveButton);
  window.SteamMarketUI?.paint();
}
function equipmentPreviewHtml(item,moveButton=''){
  const worn=item.container==='equipped';
  return `<div class="detail-title">${item.icon?`<img src="${esc(item.icon)}" alt="">`:''}<div><h3 class="quality-item-name ${itemNameClass(item)}">${esc(item.name)}</h3><small>${esc(item.slot_label)} · ${esc(classesLabel(item.class_mask))}</small></div></div>
    <div class="detail-meta"><span>${worn?'已穿戴 · ':''}${esc(item.rarity_label)} · Lv.${esc(item.level)}</span><span>强化 ${item.upgrade==null?'未确认':'+'+esc(item.upgrade)} · ${esc(item.sockets??'未确认')} 孔</span></div>${equipmentTags(item)}
    ${window.SteamMarketUI?.slot(item,true)||''}
    ${worn?'':renderComparison(item)}
    ${renderEffects(item)}
    ${['base','primary','secondary','unknown'].map(g=>{const values=item.groups[g]||[];return values.length?`<div class="detail-group"><h4>${{base:'基础属性 · 不参与筛选',primary:'主词条',secondary:'副词条',unknown:'待核验词条'}[g]}</h4><div class="type-list">${values.map(v=>`<div class="affix-row ${v.matched?'matched':''}" data-affix-key="${esc(v.key)}" title="${esc(v.value_note||(v.matched?'命中规则：'+v.matched_rules.join('、'):''))}"><span class="affix-name">${esc(v.name)}</span><strong class="affix-value">${esc(v.display_value??'未读取')}</strong>${v.matched?'<small class="affix-hit" aria-label="命中规则">✓</small>':''}</div>`).join('')}</div></div>`:'';}).join('')}
    <p class="detail-hint">${worn?'当前穿戴装备的属性预览。':'绿色为命中已启用规则的词条；整件装备是否达标见下方结果。'}数值包含已确认的装备强化。</p>
    ${renderGems(item)}
    <div class="detail-rule">${worn?'':resultBadge(item)}${item.primary_perfect_combinations?.length?`<p class="perfect-combinations">主词条全中组合：${item.primary_perfect_combinations.map(esc).join('、')}<small>四条随机主词条都在该组合的勾选范围内；此标记不检查副词条，不改变自动锁定条件。</small></p>`:''}${item.matched_combinations?.length?`<p class="matched-combinations">命中组合：${item.matched_combinations.map(esc).join('、')}</p>`:''}${item.reasons.map(r=>`<p>${esc(r)}</p>`).join('')}<button class="text-button" data-configure-item="${esc(item.name_key)}">配置这件装备的规则 →</button></div>${moveButton}`;
}
function renderEquippedPreview(){
  const dialog=$('equipped-dialog');
  if(!dialog.open)return;
  const item=state?.character?.equipment?.find(i=>i.selection_id===equippedPreviewId);
  if(!item){dialog.close();toast('该装备已不再穿戴或角色暂不可读，预览已关闭。');return;}
  const signature=JSON.stringify(item);
  if(signature===equippedPreviewSignature)return;
  equippedPreviewSignature=signature;
  const body=$('equipped-preview'),scroll=body.scrollTop;
  $('equipped-title').textContent='身上装备 · '+item.slot_label;
  body.innerHTML=equipmentPreviewHtml(item);body.scrollTop=scroll;
}
function openEquippedPreview(id){
  closeColumnMenu();equippedPreviewId=id;equippedPreviewSignature='';
  $('equipped-dialog').showModal();renderEquippedPreview();renderCharacter();
}
function closeColumnMenu(focus=false){
  $('column-menu').hidden=true;columnMenuKey=null;
  if(columnAnchor){columnAnchor.setAttribute('aria-expanded','false');if(focus)columnAnchor.focus({preventScroll:true});}
  columnAnchor=null;
}
function renderColumnButtons(){
  const sort=$('inventory-sort').value;
  document.querySelectorAll('[data-column]').forEach(button=>{
    const spec=columnMenus[button.dataset.column],filtered=spec.filter&&columnFilters[spec.filter]!==null;
    const sorted=spec.sort&&sort.startsWith(spec.sort+'-');
    button.classList.toggle('filtered',!!filtered);button.classList.toggle('sorted',!!sorted);
    button.querySelector('.column-indicator').textContent=(filtered?'● ':'')+(sorted?(sort.endsWith('asc')?'↑ ':'↓ '):'')+'▾';
    button.parentElement.setAttribute('aria-sort',sorted?(sort.endsWith('asc')?'ascending':'descending'):'none');
  });
}
function openColumnMenu(key,button){
  if(columnMenuKey===key){closeColumnMenu(true);return;}
  closeColumnMenu();columnMenuKey=key;columnAnchor=button;
  const spec=columnMenus[key],menu=$('column-menu');
  columnDraft=new Set(spec.options?(columnFilters[spec.filter]??spec.options.map(o=>o[0])):[]);
  menu.innerHTML=`<div class="column-menu-title">${esc(spec.label)}<button class="text-button" data-column-close aria-label="关闭表头菜单">×</button></div>
    ${spec.sort?`<div class="column-sort-actions"><button data-column-sort="asc" class="${$('inventory-sort').value===spec.sort+'-asc'?'selected':''}">↑ 从小到大${key==='sockets'?' · 总孔数':''}</button><button data-column-sort="desc" class="${$('inventory-sort').value===spec.sort+'-desc'?'selected':''}">↓ 从大到小${key==='sockets'?' · 总孔数':''}</button></div>`:''}
    ${spec.options?`<div class="column-filter-actions"><button class="text-button" data-column-all>全选</button><button class="text-button" data-column-none>全不选</button></div><div class="column-filter-options">${spec.options.map(([value,label])=>`<label><input type="checkbox" data-column-value="${value}" ${columnDraft.has(value)?'checked':''}>${label}</label>`).join('')}</div>${key==='rules'||key==='lock'?'<p class="column-menu-note">未筛选时包含材料和待核验物品。</p>':''}`:''}
    <div class="column-menu-footer"><button class="button ghost" data-column-clear>清除此列设置</button>${spec.options?'<button class="button primary" data-column-apply>应用筛选</button>':''}</div>`;
  menu.hidden=false;button.setAttribute('aria-expanded','true');
  const rect=button.getBoundingClientRect();
  menu.style.left=Math.max(10,Math.min(rect.left,window.innerWidth-menu.offsetWidth-10))+'px';
  menu.style.top=Math.max(10,Math.min(rect.bottom+6,window.innerHeight-menu.offsetHeight-10))+'px';
  menu.querySelector('button').focus({preventScroll:true});
}
function renderEffects(item){
  const plain=s=>String(s??'').replace(/<[^>]+>/g,'');
  return (item.effect_groups||[]).map(g=>`<div class="detail-group item-effect"><h4>${esc(g.title)}</h4>${(g.values||[]).map(v=>`<div class="effect-row"><div><span>${esc(v.name)}</span><strong>${esc(v.display_value)}</strong></div>${v.growth?`<small>${esc(v.growth)}</small>`:''}</div>`).join('')}${(g.texts||[]).map(t=>`<p>${esc(plain(t))}</p>`).join('')}</div>`).join('');
}
function readAutomationForm(){
  const pages=kind=>[...document.querySelectorAll(`[data-automation-pages="${kind}"]:checked`)].map(input=>Number(input.value)).sort((a,b)=>a-b);
  return {carriage:{enabled:$('auto-carriage').checked,types:[...automationDraft.carriage.types]},
    gems:{enabled:$('auto-gems').checked,pages:pages('gem')},runes:{enabled:$('auto-runes').checked,pages:pages('rune')},
    pressure:{enabled:$('auto-pressure').checked,threshold:Number($('pressure-threshold').value),target_free:Number($('pressure-target').value),equipment_filter:$('pressure-filter').value,pages:pages('equipment')}};
}
function markAutomationDirty(){
  automationDraft=readAutomationForm();refreshAutomationDirty();
}
const sharedStorage=window.AutomationTools?.createSharedStorage();
function refreshAutomationDirty(){
  if(!automationDraft||!state?.automation)return;
  const baseline=window.AutomationTools.withSharedPages(state.automation.settings,sharedStorage?.saved);
  automationDirty=JSON.stringify(automationDraft)!==JSON.stringify(baseline);
  setDirty(dirty);
  $('automation-draft-state').textContent=automationDirty?'有未保存的设置':'设置已保存';
  $('save-automation').disabled=!automationDirty||!!state.managed?.running;
  $('run-automation').disabled=!state.automation.running&&(!state.connected||state.busy||state.managed?.running||automationDirty||!Object.values(automationDraft).some(v=>v.enabled));
}
sharedStorage?.subscribe(()=>{
  if(!automationDraft)return;
  automationDraft=window.AutomationTools.withSharedPages(automationDraft,sharedStorage.current);
  renderAutomationPages();refreshAutomationDirty();
});
function filteredCollectionChoices(){
  return window.AutomationTools.kinds.map(([key,name])=>({key,name}));
}
function setFilteredCollectionSelection(checked){
  if(!automationDraft)return;
  const choices=filteredCollectionChoices();if(!choices.length)return;
  const selected=new Set(automationDraft.carriage.types);
  choices.forEach(i=>checked?selected.add(i.key):selected.delete(i.key));
  automationDraft.carriage.types=[...selected];
  markAutomationDirty();renderCollectionChoices();
}
function renderCollectionChoices(){
  if(!automationDraft)return;
  const selected=new Set(automationDraft.carriage.types);
  const choices=filteredCollectionChoices();
  const signature=JSON.stringify([choices,automationDraft.carriage.types]);
  if(signature!==collectionSignature){collectionSignature=signature;$('carriage-choices').innerHTML=choices.map(i=>`<label><input type="checkbox" data-collect-type="${esc(i.key)}" ${selected.has(i.key)?'checked':''}><span>${esc(i.name)}</span></label>`).join('');}
  $('carriage-selected').textContent=`已选 ${selected.size} / ${choices.length} 类物品`;
  $('carriage-select-all').disabled=!!state.managed?.running||!choices.some(i=>!selected.has(i.key));
  $('carriage-select-none').disabled=!!state.managed?.running||!choices.some(i=>selected.has(i.key));
}
function renderAutomationPages(){
  if(!automationDraft)return;
  const rows=window.AutomationTools.storagePages(state.transfer,state.items),ready=state.connected&&state.complete&&window.AutomationTools.storageReady(state.transfer),signature=JSON.stringify([rows,ready,automationDraft.gems.pages,automationDraft.runes.pages,automationDraft.pressure.pages]);
  if(signature===automationPageSignature)return;automationPageSignature=signature;
  for(const [kind,group] of [['gem','gems'],['rune','runes'],['equipment','pressure']]){
    const choices=window.AutomationTools.pageChoices(rows,automationDraft[group].pages,ready);
    $('automation-'+kind+'-pages').innerHTML=choices.map(row=>{
      const label=row.status==='available'?`${row.free} 空`:row.status==='locked'?'未解锁':'待读取';
      const title=row.status==='available'?`剩余 ${row.free} / ${row.capacity} 格`:row.status==='locked'?'该页尚未解锁；保留选择，解锁后自动使用。也可取消并选择其他页。':'连接后读取实际仓库权限。';
      return `<label class="${row.status==='available'?'':'storage-page-unavailable'}" title="${esc(title)}"><input type="checkbox" data-automation-pages="${kind}" value="${row.index}" ${row.selected?'checked':''}><span>${esc(row.name)}</span><small>${esc(label)}</small></label>`;
    }).join('')||'<small>连接后读取已开放的仓库页</small>';
  }
}
function renderAutomation(){
  const a=state.automation;if(!a)return;
  if(!automationDraft||(!automationDirty&&JSON.stringify(automationDraft)!==JSON.stringify(a.settings))){
    automationDraft=JSON.parse(JSON.stringify(a.settings));$('auto-carriage').checked=automationDraft.carriage.enabled;$('auto-gems').checked=automationDraft.gems.enabled;$('auto-runes').checked=automationDraft.runes.enabled;$('auto-pressure').checked=automationDraft.pressure.enabled;$('pressure-threshold').value=automationDraft.pressure.threshold;$('pressure-target').value=automationDraft.pressure.target_free;$('pressure-filter').value=automationDraft.pressure.equipment_filter;renderCollectionChoices();
  }
  automationDraft=window.AutomationTools.withSharedPages(automationDraft,sharedStorage?.current);
  renderAutomationPages();refreshAutomationDirty();
  $('automation-panel').querySelectorAll('input,select,button').forEach(input=>input.disabled=!!state.managed?.running);
  renderCollectionChoices();
  $('automation-summary').textContent=(a.running?'运行中':'已暂停')+' · 点击配置';
  $('automation-summary').classList.toggle('running',a.running);
  $('automation-status').textContent=a.status;
  const carriage=a.carriage||{};
  $('carriage-state').textContent=!state.connected?'连接后读取马车':carriage.available?`当前马车 ${carriage.count} 组：${carriage.items.map(i=>i.name+' ×'+i.count).join('、')||'暂无物品'}`:carriage.reason||'当前场景没有马车';
  $('run-automation').textContent=a.running?'暂停自动整理':'启动自动整理';
  $('run-automation').disabled=!a.running&&(!state.connected||state.busy||state.managed?.running||automationDirty||!Object.values(automationDraft).some(v=>v.enabled));
  $('save-automation').disabled=!automationDirty||!!state.managed?.running;
  $('automation-draft-state').textContent=automationDirty?'有未保存的设置':'设置已保存';
}
function updateControls(){
  if(!state)return;
  const managedRunning=!!state.managed?.running;
  const available=state.connected&&!state.busy&&!managedRunning&&state.complete,enabled=state.rules.some(r=>r.enabled);
  $('connect').hidden=state.connected;$('connect').disabled=state.busy;
  $('disconnect').hidden=!state.connected;$('disconnect').disabled=state.busy||managedRunning;
  $('refresh').disabled=!state.connected||state.busy||managedRunning;
  $('unlock-all').disabled=!available||!scopedItems().some(i=>i.locked===true);
  $('lock-selected').disabled=!available||!state.items.some(i=>selectedItems.has(i.selection_id)&&i.is_equipment&&i.locked===false);
  for(const target of ['storage','inventory']){const items=state.items.filter(i=>selectedItems.has(i.selection_id)&&i.container!==target);$('move-'+target+'-count').textContent=items.length;$('move-'+target).disabled=!available||!state.transfer?.available||!items.length||items.some(i=>!i.movable);}
  $('transfer-hint').textContent=state.connected&&!state.transfer?.available?(state.transfer?.reason||'移动条件尚未核验，请刷新物品。'):'优先堆叠同种同等级宝石；满堆后使用已开放的空格。';
  $('lock-rules').disabled=!available||!enabled;
  $('lock-rules').title=enabled?'按已启用规则处理批量范围内的装备':'请先在规则页保存并启用至少一条规则';
  $('export-items').disabled=!state.items.length;
  $('monitor-switch').disabled=managedRunning||!state.connected||(!state.monitoring&&(state.busy||!state.complete||!enabled));
  $('monitor-switch').title=!state.connected?'请先连接游戏':!enabled?'请先保存并启用至少一条规则':'监控背包和仓库里的新增装备';
  $('monitor-switch').classList.toggle('on',state.monitoring);$('monitor-switch').setAttribute('aria-checked',state.monitoring);
  $('stop').hidden=!state.busy&&!state.monitoring&&!state.automation?.running&&!managedRunning;
  $('monitor-badge').hidden=!state.monitoring&&!state.automation?.running&&!managedRunning;
}
function renderManagedTab(packet){
  const running=state?.connected===true&&packet?.running===true;
  $('managed-tab-status').hidden=!running;
  $('managed-tab-status').title=running?(packet.status||'托管正在运行。'):'';
  $('managed-tab').setAttribute('aria-label',running?'托管模式，运行中':'托管模式');
}
function renderState(){
  renderManagedTab(state.managed);
  if(state.loot_capture)renderLootCapture(state.loot_capture);
  window.ManagedModeUI?.setConnection(state.connected);
  window.ManagedModeUI?.setCharacter(state.character);
  const compatibility=state.compatibility||{phase:'idle',message:'连接时自动检测游戏兼容性'};
  if(!state.connected)clearLootPreview('连接游戏并进入在线关卡，查看这趟会掉落什么。');
  else if(['blocked','changed','error'].includes(compatibility.phase))clearLootPreview(compatibility.message||'当前游戏版本暂不支持读取掉落计划。','unsupported');
  $('compatibility-message').textContent=compatibility.message;
  $('compatibility-bar').dataset.phase=compatibility.phase;
  const connection=$('connection-state');connection.className='status-pill'+(state.connected?' online':'')+(state.busy?' busy':'');
  connection.innerHTML='<i></i>'+ (state.connected?`已连接${state.busy?' · 处理中':''}`:state.busy?'正在连接…':'未连接游戏');
  connection.title=state.pid?`游戏进程 ${state.pid}`:'';
  const game=state.game||{},gameLabels={running:'游戏运行中',stopped:'游戏未运行',starting:'Steam 启动中…',closing:'游戏退出中…',unknown:'游戏状态未确认'};
  $('game-state').textContent=game.status==='running'&&game.control_unavailable?'游戏运行中 · 控制暂不可用':gameLabels[game.status]||'检测游戏中…';
  $('game-state').dataset.status=game.status||'unknown';
  $('game-state').title=game.error||(game.count>1?'检测到多个游戏进程，请在游戏窗口中退出。':'');
  $('game-start').hidden=game.running===true||game.status==='closing';
  $('game-start').disabled=game.can_start!==true;
  $('game-close').hidden=game.running!==true;
  $('game-close').disabled=game.can_close!==true;
  $('game-close').title=game.can_close===true?'关闭游戏前需要再次确认':game.error||(game.count>1?'检测到多个游戏进程，请在游戏窗口中退出。':'等待重新核验游戏进程后关闭。');
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
  renderCharacter();renderInventory();updateControls();renderAutomation();
  $('logging-state').textContent=state.connected?'已连接 · 每 2 秒记录物品变化':'连接游戏后开始记录物品变化';
}
async function pollState(){
  if(polling||exiting)return;
  polling=true;
  const sharedEpoch=sharedStorage?.epoch;
  try{state=await api('state');if(state.automation)sharedStorage?.observe(window.AutomationTools.sharedPages(state.automation.settings),sharedEpoch);renderState();}
  catch(error){renderManagedTab(null);clearLootPreview('助手服务已断开，请重新启动后连接游戏。','unavailable');$('footer-state').textContent='助手服务已断开，请重新双击启动文件。';if(!state)$('operation-message').textContent=error.message;}
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
$('game-start').addEventListener('click',async()=>{try{$('game-start').disabled=true;await api('game/start',{});await pollState();}catch(error){toast(error.message,true);await pollState();}});
$('game-close').addEventListener('click',async()=>{
  const target=state?.game?.target;if(!target)return;
  if(!await confirmAction('关闭游戏？','将停止托管、监控和自动整理，并请求游戏正常退出。未保存的助手配置会继续保留在当前窗口。','关闭游戏'))return;
  try{$('game-close').disabled=true;await api('game/close',{confirmed:true,target});await pollState();}catch(error){toast(error.message,true);await pollState();}
});
$('refresh').addEventListener('click',()=>action('read'));
$('equipment-search').addEventListener('input',renderCatalog);$('slot-filter').addEventListener('change',renderCatalog);$('configured-filter').addEventListener('change',renderCatalog);
$('class-filters').addEventListener('click',e=>{const button=e.target.closest('[data-class]');if(!button)return;selectedClass=Number(button.dataset.class);document.querySelectorAll('[data-class]').forEach(b=>b.classList.toggle('selected',b===button));renderCatalog();if(selectedClass&&gearKey&&(gearMap.get(gearKey).class_mask&selectedClass)){affixClass=selectedClass;renderEditor();}});
$('equipment-grid').addEventListener('click',e=>{const card=e.target.closest('[data-gear]');if(card)chooseGear(card.dataset.gear);});
$('rule-editor').addEventListener('change',async e=>{
  const input=e.target;
  if(input.id==='affix-class'){affixClass=Number(input.value);renderEditor();}
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
  if(button.id==='delete-rule')deleteCurrentCombination();
  if(button.id==='cancel-new-rule'){setDirty(false);draft=makeDraft(rulesFor(gearKey)[0]);renderEditor();}
  if(button.id==='manage-rules')openCombinationManager();
  if(button.id==='copy-to-gear')openCopyDialog();
});
$('import-rules').addEventListener('click',()=>$('import-file').click());
$('import-file').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file)return;
  try{openImportDialog(JSON.parse((await file.text()).replace(/^\uFEFF/,'')),file.name);}catch(error){toast(error.message,true);}finally{e.target.value='';}
});
$('export-rules').addEventListener('click',()=>download('export/rules','Deskrawl筛选规则.json'));
$('export-items').addEventListener('click',()=>download('export/items','Deskrawl装备清单.json'));
$('item-search').addEventListener('input',renderInventory);$('lock-filter').addEventListener('change',renderInventory);$('kind-filter').addEventListener('change',renderInventory);$('action-scope').addEventListener('change',updateControls);
inventoryFilterIds.forEach(id=>$(id).addEventListener($(id).type==='number'?'input':'change',renderInventory));
$('inventory-reset').addEventListener('click',()=>{inventoryFilterIds.forEach(id=>{const input=$(id);if(input.type==='checkbox')input.checked=false;else input.value=id==='inventory-sort'?'position':'';});$('item-search').value='';$('kind-filter').value='';$('lock-filter').value='';columnFilters={containers:null,locks:null,rules:null};closeColumnMenu();renderInventory();});
$('character-content').addEventListener('click',e=>{const button=e.target.closest('[data-equipped]');if(button)openEquippedPreview(button.dataset.equipped);});
$('equipped-close').addEventListener('click',()=>$('equipped-dialog').close());
$('equipped-dialog').addEventListener('close',()=>{equippedPreviewId=null;equippedPreviewSignature='';renderCharacter();});
$('equipped-dialog').addEventListener('click',async e=>{if(e.target===$('equipped-dialog')){$('equipped-dialog').close();return;}const button=e.target.closest('[data-configure-item]');if(button&&gearMap.has(button.dataset.configureItem)){$('equipped-dialog').close();await chooseGear(button.dataset.configureItem);switchTab('rules');}});
document.querySelector('.inventory-table thead').addEventListener('click',e=>{const button=e.target.closest('[data-column]');if(button)openColumnMenu(button.dataset.column,button);});
$('column-menu').addEventListener('change',e=>{const value=e.target.dataset.columnValue;if(value){if(e.target.checked)columnDraft.add(value);else columnDraft.delete(value);}});
$('column-menu').addEventListener('click',e=>{
  const button=e.target.closest('button');if(!button||!columnMenuKey)return;
  const spec=columnMenus[columnMenuKey];
  if(button.hasAttribute('data-column-close')){closeColumnMenu(true);return;}
  if(button.hasAttribute('data-column-all')||button.hasAttribute('data-column-none')){
    const all=button.hasAttribute('data-column-all');columnDraft=new Set(all?spec.options.map(o=>o[0]):[]);
    $('column-menu').querySelectorAll('[data-column-value]').forEach(input=>input.checked=all);return;
  }
  if(button.dataset.columnSort)$('inventory-sort').value=spec.sort+'-'+button.dataset.columnSort;
  if(button.hasAttribute('data-column-apply')){
    columnFilters[spec.filter]=columnDraft.size===spec.options.length?null:[...columnDraft];
  }
  if(button.hasAttribute('data-column-clear')){if(spec.filter)columnFilters[spec.filter]=null;if(spec.sort&&$('inventory-sort').value.startsWith(spec.sort+'-'))$('inventory-sort').value='position';}
  closeColumnMenu(true);renderInventory();
});
document.addEventListener('click',e=>{if(columnMenuKey&&!e.target.closest('#column-menu')&&!e.target.closest('[data-column]'))closeColumnMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&columnMenuKey){e.preventDefault();e.stopPropagation();closeColumnMenu(true);}});
window.addEventListener('resize',()=>closeColumnMenu());
document.addEventListener('scroll',e=>{if(columnMenuKey&&!$('column-menu').contains(e.target))closeColumnMenu();},true);
$('item-detail').addEventListener('change',e=>{if(e.target.id==='detail-ring-target'){detailTarget=Number(e.target.value);renderDetail();}});
$('item-rows').addEventListener('change',e=>{const id=e.target.dataset.itemSelect;if(!id)return;if(e.target.checked)selectedItems.add(id);else selectedItems.delete(id);renderInventory();});
$('item-rows').addEventListener('click',e=>{if(e.target.matches('input'))return;const row=e.target.closest('[data-item]');if(row){if(detailId!==row.dataset.item)detailTarget=null;detailId=row.dataset.item;renderInventory();}});
$('item-rows').addEventListener('keydown',e=>{if(e.target.matches('input')||!['Enter',' '].includes(e.key))return;const row=e.target.closest('[data-item]');if(row){e.preventDefault();if(detailId!==row.dataset.item)detailTarget=null;detailId=row.dataset.item;renderInventory();}});
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
$('automation-panel').addEventListener('change',e=>{const key=e.target.dataset.collectType;if(key){const selected=new Set(automationDraft.carriage.types);e.target.checked?selected.add(key):selected.delete(key);automationDraft.carriage.types=[...selected];}markAutomationDirty();const kind=e.target.dataset.automationPages;if(['rune','gem'].includes(kind))sharedStorage?.edit({[kind]:automationDraft[kind+'s'].pages});renderCollectionChoices();renderAutomationPages();});
$('carriage-select-all').addEventListener('click',()=>setFilteredCollectionSelection(true));
$('carriage-select-none').addEventListener('click',()=>setFilteredCollectionSelection(false));
for(const id of ['pressure-threshold','pressure-target'])$(id).addEventListener('input',markAutomationDirty);
async function saveAutomation(){try{const submitted=readAutomationForm(),result=await api('automation/settings',{settings:submitted});state.automation.settings=result.settings;automationDraft=result.settings;sharedStorage?.commit(window.AutomationTools.sharedPages(result.settings),window.AutomationTools.sharedPages(submitted));automationDraft=window.AutomationTools.withSharedPages(automationDraft,sharedStorage?.current);refreshAutomationDirty();await pollState();await window.ManagedModeUI?.refresh();toast('自动整理设置已保存，共用存放页已同步');return true;}catch(error){toast(error.message,true);return false;}}
$('save-automation').addEventListener('click',saveAutomation);
$('run-automation').addEventListener('click',async()=>{try{await api('automation/run',{enabled:!state.automation.running});await pollState();}catch(error){toast(error.message,true);}});
$('stop').addEventListener('click',async()=>{try{await api('stop',{});await pollState();}catch(error){toast(error.message,true);}});
$('history-toggle').addEventListener('click',()=>switchTab('logs'));
async function requestExit(){
  const pending=hasPendingDrafts()?['未保存的配置']:[];
  if(pending.length&&!await confirmAction('退出助手？',`当前有${pending.join('、')}。退出将放弃这些内容，并停止持续监控和自动整理。`,'退出',false))return;
  try{await api('shutdown',{});exiting=true;document.body.innerHTML='<div class="empty-state"><h2>助手已退出</h2><p class="note">持续监控已停止。可关闭此页面，或双击启动文件重新打开。</p></div>';}catch(error){toast(error.message,true);}
}
$('exit').addEventListener('click',requestExit);
window.addEventListener('beforeunload',e=>{if(!exiting&&hasPendingDrafts()){e.preventDefault();e.returnValue='';}});
async function boot(){
  if(desktopMode)$('usage-note').textContent='监控只自动处理新增装备；已有装备请用“一键按规则锁定”。最小化窗口可继续监控，关闭窗口会退出助手并停止监控。';
  try{catalog=await api('catalog');buildCatalog();window.ManagedModeUI?.init({api,toast,confirmAction,onDirty:syncDraftState,catalog,sharedStorage});await pollState();const first=state.rules[0]?.equipment_keys[0]||'LegendaryBelt3';if(first)await chooseGear(first);await window.ManagedModeUI?.refresh();await pollUpdate();setInterval(pollState,1000);setInterval(pollUpdate,1500);setInterval(()=>{if(activeTab==='logs'&&!logBefore)loadLogs(true);},2000);}catch(error){toast(error.message,true);}
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
  for(const id of ['rules-page','automation-panel','managed-page'])$(id).inert=active||updateSaving;
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
    if(hasPendingDrafts())throw new Error('更新前请先保存未完成的配置。');
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
function lootPreviewNumber(value){
  return value!==null&&value!==undefined&&Number.isFinite(Number(value))?Math.max(0,Math.round(Number(value))).toLocaleString('zh-CN'):'—';
}
function clearLootPreview(reason,phase='waiting'){
  lootPreviewRequest++;lootPreviewLoading=false;lootPreviewData=null;lootPreviewSignature='';
  $('loot-preview-map').textContent='等待读取关卡';$('loot-preview-run').textContent=reason;
  $('loot-preview-phase').textContent=phase==='unsupported'?'版本暂不支持':phase==='unavailable'?'暂不能读取':'等待进图';
  $('loot-preview-phase').dataset.phase=phase;
  for(const id of ['items','gold','potions','wave'])$('loot-preview-'+id).textContent='—';
  $('loot-preview-equipment').textContent='包含装备、宝石、符文及其他物品';
  $('loot-preview-updated').textContent='';$('loot-preview-refresh').disabled=false;
  $('loot-preview-warnings').textContent='';$('loot-preview-warnings').hidden=true;
  $('loot-preview-waves').replaceChildren();$('loot-preview-empty').textContent=reason;$('loot-preview-empty').hidden=false;
}
function lootPreviewItem(item){
  const kinds={equipment:'装备',gem:'宝石',rune:'符文',chest:'宝箱',material:'材料',other:'其他物品'};
  const modifiers=Array.isArray(item.modifiers)?item.modifiers:[];
  const rarity=item.rarity==null?NaN:Number(item.rarity),rarityClass=Number.isInteger(rarity)&&rarity>=0&&rarity<=9?' loot-rarity-'+rarity:'';
  const icon=typeof item.icon==='string'&&item.icon.startsWith('/icons/')?item.icon:'';
  const equipment=item.kind==='equipment';
  const mistClass=equipment&&item.black_mist&&rarity!==4?' loot-black-mist':'';
  return `<article class="loot-preview-item${rarityClass}${mistClass}"><div class="loot-preview-item-head"><div class="loot-preview-icon">${icon?`<img src="${esc(icon)}" alt="" loading="lazy">`:'<span aria-hidden="true">◇</span>'}</div><div><h3>${esc(item.name||'名称尚未确认')}</h3><p>${esc(item.rarity_label||kinds[item.kind]||'其他物品')}${item.slot_label?' · '+esc(item.slot_label):''}</p></div><strong class="loot-preview-quantity" title="计划数量">×${lootPreviewNumber(item.quantity)}</strong></div>
    <div class="loot-preview-item-meta">${item.level!==null&&item.level!==undefined?`<span>物品等级 ${esc(item.level)}</span>`:''}${equipment?`<span>${item.sockets===null||item.sockets===undefined?'孔数未确认':esc(item.sockets)+' 孔'}</span>`:''}${equipment&&item.ancient?'<span class="equipment-tag ancient">远古</span>':''}${equipment&&item.black_mist?'<span class="equipment-tag mist">黑雾</span>':''}${equipment&&item.matches?`<span class="equipment-tag loot-rule-match" title="${esc(item.rule_note)}">符合规则</span>`:''}${equipment&&item.primary_perfect?'<span class="equipment-tag loot-rule-match">主词条全中</span>':''}</div>
    ${equipment&&item.groups?lootPreviewGroups(item):modifiers.length?`<ul class="loot-preview-affixes">${modifiers.map(modifier=>`<li>${esc(modifier.text||'词条待确认')}</li>`).join('')}</ul>`:equipment?'<p class="loot-preview-item-note">此装备暂无可显示词条。</p>':''}
    ${renderEffects(item)}${item.effect_note?`<p class="loot-preview-item-note">${esc(item.effect_note)}</p>`:''}
    ${equipment&&item.rule_note?`<p class="loot-preview-item-note">${esc(item.rule_note)}</p>`:''}
    ${item.item_key?`<p class="loot-preview-item-id">${esc(item.item_key)}</p>`:''}
    ${item.kind==='chest'&&Number(item.chest_gold)>0?`<p class="loot-preview-item-note">计划箱内金币 ${lootPreviewNumber(item.chest_gold)}（不计入波次合计）</p>`:''}${item.kind==='chest'?'<p class="loot-preview-item-note">箱子内物品需结算后确认。</p>':''}</article>`;
}
function lootPreviewGroups(item){
  const titles={base:'基础属性 · 不参与筛选',primary:'主词条',secondary:'副词条',unknown:'未确认词条'};
  return Object.entries(titles).map(([group,title])=>{
    const rows=Array.isArray(item.groups[group])?item.groups[group]:[];
    return rows.length?`<div class="detail-group loot-affix-group"><h4>${title}</h4>${rows.map(row=>`<div class="affix-row${row.matched?' matched':''}" title="${esc(row.matched?(row.matched_rules||[]).join('、'):row.value_note||'')}"><span>${esc(row.name)}</span><strong>${esc(row.display_value)}${row.matched?' ✓':''}</strong></div>`).join('')}</div>`:'';
  }).join('');
}
function renderLootCapture(value){
  if(!value)return;
  if(!lootCaptureBusy)$('loot-capture-enabled').checked=value.enabled===true;
  $('loot-capture-status').textContent=value.error||value.message||'监控未开启。';
  $('loot-capture-status').className=value.error?'inline-error':'';
  const time=value.last_saved?new Date(value.last_saved).toLocaleTimeString('zh-CN',{hour12:false}):'';
  const count=value.saved_runs===null||value.saved_runs===undefined?'—':lootPreviewNumber(value.saved_runs);
  $('loot-capture-summary').textContent=`留档 ${count} 份记录${time?' · 最近留档 '+time:''}`;
  $('loot-capture-directory').textContent=value.directory||'等待读取';
}
$('loot-capture-enabled').addEventListener('change',async()=>{
  if(lootCaptureBusy)return;
  const enabled=$('loot-capture-enabled').checked;lootCaptureBusy=true;$('loot-capture-enabled').disabled=true;
  try{const value=await api('loot-capture',{enabled});lootCaptureBusy=false;renderLootCapture(value);}
  catch(error){lootCaptureBusy=false;$('loot-capture-enabled').checked=!enabled;toast(error.message,true);}
  finally{$('loot-capture-enabled').disabled=false;}
});
function renderLootPreview(data){
  if(data?.capture_monitor)renderLootCapture(data.capture_monitor);
  if(!data||data.available!==true||data.phase!=='active'||!data.run||!data.run.key){
    clearLootPreview(data?.reason||'当前没有可读取的在线关卡计划。',data?.phase||'unavailable');return;
  }
  const run=data.run,totals=data.totals||{},waves=Array.isArray(data.waves)?data.waves:[];
  const captured=new Date(data.updated_at);
  $('loot-preview-updated').textContent=Number.isNaN(captured.getTime())?'每 2 秒自动更新':'读取于 '+captured.toLocaleTimeString('zh-CN',{hour12:false});
  $('loot-preview-refresh').disabled=false;
  const signature=JSON.stringify([run,totals,waves,data.warnings]);
  lootPreviewData=data;if(signature===lootPreviewSignature)return;lootPreviewSignature=signature;
  $('loot-preview-map').textContent=run.map_name||'当前在线关卡';
  $('loot-preview-run').textContent=(run.difficulty_label||'难度未确认')+' · 本趟服务器计划';
  $('loot-preview-phase').textContent=waves.length&&waves.every(wave=>wave.state==='completed')?'本趟波次已完成 · 待结算':'在线关卡进行中';$('loot-preview-phase').dataset.phase='active';
  $('loot-preview-items').textContent=lootPreviewNumber(totals.items);
  $('loot-preview-equipment').textContent='其中装备 '+lootPreviewNumber(totals.equipment)+' 件';
  $('loot-preview-gold').textContent=lootPreviewNumber(totals.gold);
  $('loot-preview-potions').textContent=lootPreviewNumber(totals.potion_charges);
  $('loot-preview-wave').textContent=lootPreviewNumber(run.current_wave)+' / '+lootPreviewNumber(run.total_waves);
  const warnings=Array.isArray(data.warnings)?data.warnings:[];
  $('loot-preview-warnings').textContent=warnings.join(' ');$('loot-preview-warnings').hidden=!warnings.length;
  const hasItems=waves.some(wave=>Array.isArray(wave.items)&&wave.items.length);
  $('loot-preview-empty').textContent=waves.length?'本趟计划没有物品掉落；金币与药水补充见各波次。':'本趟计划尚未提供波次内容，稍后自动重新读取。';
  $('loot-preview-empty').hidden=hasItems;
  $('loot-preview-waves').innerHTML=waves.map(wave=>{
    const progress=['completed','current','upcoming'].includes(wave.state)?wave.state:'upcoming';
    const label={completed:'已完成波次 · 待结算',current:'当前波次',upcoming:'后续波次'}[progress];
    const items=Array.isArray(wave.items)?wave.items:[];
    return `<section class="panel loot-preview-wave ${progress}"><div class="loot-preview-wave-heading"><h2>第 ${esc(wave.number)} 波 <span>${label}</span></h2><div><span>金币 ${lootPreviewNumber(wave.gold)}</span><span>药水补充 ${lootPreviewNumber(wave.potion_charges)} 次</span><span>${items.length} 组物品</span></div></div>${items.length?`<div class="loot-preview-item-grid">${items.map(lootPreviewItem).join('')}</div>`:'<p class="loot-preview-wave-empty">此波无计划物品。</p>'}</section>`;
  }).join('');
}
async function loadLootPreview(background=false){
  if(exiting||lootPreviewLoading)return;
  if(state&&!state.connected){clearLootPreview('连接游戏并进入在线关卡，查看这趟会掉落什么。');return;}
  if(['blocked','changed','error'].includes(state?.compatibility?.phase)){clearLootPreview(state.compatibility.message||'当前游戏版本暂不支持读取掉落计划。','unsupported');return;}
  const request=++lootPreviewRequest;lootPreviewLoading=true;
  if(!background)$('loot-preview-refresh').disabled=true;
  try{const data=await api('loot-preview');if(request!==lootPreviewRequest||exiting)return;renderLootPreview(data);}
  catch(error){if(request===lootPreviewRequest&&!exiting)clearLootPreview('本趟计划暂不能读取：'+error.message,'unavailable');}
  finally{if(request===lootPreviewRequest){lootPreviewLoading=false;$('loot-preview-refresh').disabled=false;}}
}
$('loot-preview-refresh').addEventListener('click',()=>loadLootPreview());
setInterval(()=>{if(!exiting&&activeTab==='loot-preview')loadLootPreview(true);},2000);
boot();
