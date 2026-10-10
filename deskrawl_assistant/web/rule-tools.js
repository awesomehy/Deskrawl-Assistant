'use strict';
let importPayload=null, managerGear=null, managerBusy=false, copyContext=null;

function openImportDialog(payload,fileName){
  const rules=Array.isArray(payload)?payload:payload?.rules;
  if(!Array.isArray(rules)||!rules.length)throw new Error('文件中没有可导入的组合。');
  importPayload=payload;
  $('import-summary').textContent=`${fileName} · ${rules.length} 个词条组合`;
  $('import-enabled').value='false';$('import-confirm').disabled=false;
  $('import-dialog').showModal();
}
$('import-cancel').onclick=()=>{$('import-dialog').close();importPayload=null;};
$('import-dialog').addEventListener('cancel',()=>{importPayload=null;});
$('import-confirm').onclick=async()=>{
  if(!importPayload)return;
  $('import-confirm').disabled=true;
  try{
    const enabled=$('import-enabled').value==='true';
    const result=await api('rule/import',{payload:importPayload,enabled});
    $('import-dialog').close();importPayload=null;await pollState();
    if(gearKey){if(!dirty)await chooseGear(gearKey,draft.id);else renderEditor();}
    toast(`已导入 ${result.imported} 个组合，${enabled?'已启用':'已停用'}`);
  }catch(error){toast(error.message,true);}finally{$('import-confirm').disabled=false;}
};

async function removeCombinations(ids,key){
  const result=await api('rule/delete',{ids,equipment_key:key});
  const removedCurrent=gearKey===key&&ids.includes(draft.id);
  if(removedCurrent){setDirty(false);draft=makeDraft(null);}
  await pollState();
  if(gearKey===key){if(removedCurrent)await chooseGear(key);else renderEditor();}
  toast(`已删除 ${result.deleted} 个词条组合`);
}
async function deleteCurrentCombination(){
  const id=draft.id,key=gearKey,name=rulesFor(key).find(r=>r.id===id)?.name||draft.name;
  if(!id)return;
  if(await confirmAction(`删除「${name}」？`,'删除当前组合及其未保存改动，其他组合保留。','删除组合')){
    try{await removeCombinations([id],key);}catch(error){toast(error.message,true);}
  }
}
function openCombinationManager(){
  managerGear=gearKey;renderCombinationManager();$('manage-dialog').showModal();
}
function renderCombinationManager(){
  const rules=rulesFor(managerGear);
  $('manage-title').textContent=`管理 ${gearMap.get(managerGear).name} 的组合`;
  $('manage-list').innerHTML=rules.length?rules.map((r,i)=>{
    const summary=r.groups?`主词条 ≥${r.groups.primary.count} 类${r.groups.secondary?` · 副词条 ≥${r.groups.secondary.count} 类`:' · 不检查副词条'}`:'旧版数值规则';
    return `<label class="manage-row"><input type="checkbox" data-manage-id="${esc(r.id)}" aria-label="选择第 ${i+1} 个组合 ${esc(r.name)}"><span><strong>${i+1}. ${esc(r.name)}</strong><small>${esc(summary)}</small></span><span class="badge ${r.enabled?'match':'miss'}">${r.enabled?'已启用':'已停用'}</span></label>`;
  }).join(''):'<div class="empty-state">这件装备的组合已清空</div>';
  updateManagerSelection();
}
function selectedManagerIds(){return [...$('manage-list').querySelectorAll('input:checked')].map(i=>i.dataset.manageId);}
function updateManagerSelection(){
  const count=selectedManagerIds().length,total=rulesFor(managerGear).length;
  $('manage-count').textContent=`已选 ${count} / ${total} 个`;
  $('manage-delete').disabled=!count||managerBusy;
  $('manage-all').disabled=!total||managerBusy;$('manage-none').disabled=!total||managerBusy;
}
$('manage-list').addEventListener('change',updateManagerSelection);
$('manage-all').onclick=()=>{$('manage-list').querySelectorAll('input').forEach(i=>i.checked=true);updateManagerSelection();};
$('manage-none').onclick=()=>{$('manage-list').querySelectorAll('input').forEach(i=>i.checked=false);updateManagerSelection();};
$('manage-close').onclick=()=>$('manage-dialog').close();
$('manage-delete').onclick=async()=>{
  const ids=selectedManagerIds(),key=managerGear;
  if(!ids.length||managerBusy)return;
  managerBusy=true;updateManagerSelection();
  try{
    if(await confirmAction(`删除所选 ${ids.length} 个组合？`,'只清理当前装备所选的组合，未勾选的组合保留。','删除所选')){
      await removeCombinations(ids,key);renderCombinationManager();
    }
  }catch(error){toast(error.message,true);}finally{managerBusy=false;updateManagerSelection();}
};

function openCopyDialog(){
  const source=gearMap.get(gearKey),rules=rulesFor(gearKey);
  if(!rules.length){toast('请先保存来源装备的组合。',true);return;}
  const targets=catalog.equipment.filter(g=>g.legendary&&g.slot===source.slot&&g.key!==source.key)
    .sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'));
  if(!targets.length){toast('此部位没有其他可复制的传说装备。',true);return;}
  copyContext={key:gearKey,rules,currentId:draft.id,targetIds:[]};
  $('copy-source').textContent=`来源：${source.name} · ${source.slot_label} · ${rules.length} 个已保存组合`;
  $('copy-scope').value='all';$('copy-scope').querySelector('option[value="current"]').disabled=!draft.id;
  $('copy-mode').value='append';
  $('copy-target').innerHTML=targets.map(g=>`<option value="${esc(g.key)}">${esc(g.name)} · ${rulesFor(g.key).length} 个组合</option>`).join('');
  $('copy-confirm').disabled=false;renderCopySummary();$('copy-dialog').showModal();
}
function renderCopySummary(){
  if(!copyContext)return;
  const key=$('copy-target').value,target=gearMap.get(key),replace=$('copy-mode').value==='replace';
  const rules=rulesFor(key);copyContext.targetIds=rules.map(r=>r.id);
  const count=$('copy-scope').value==='current'?1:copyContext.rules.length;
  $('copy-target-preview').innerHTML=`${gearIcon(target)}<div><strong>${esc(target.name)}</strong><small>${esc(target.slot_label)} · ${esc(classesLabel(target.class_mask))} · 已有 ${rules.length} 个组合</small></div>`;
  $('copy-summary').textContent=replace?`将清理目标的 ${rules.length} 个组合，再复制 ${count} 个来源组合。`:`将追加 ${count} 个来源组合，保留目标的 ${rules.length} 个组合。`;
  $('copy-summary').className=replace?'copy-replace-note':'';
  $('copy-confirm').textContent=replace?'替换并复制':'复制组合';
  $('copy-confirm').className='button '+(replace?'danger':'primary');
}
for(const id of ['copy-target','copy-mode','copy-scope'])$(id).addEventListener('change',renderCopySummary);
$('copy-cancel').onclick=()=>{$('copy-dialog').close();copyContext=null;};
$('copy-dialog').addEventListener('cancel',()=>{copyContext=null;});
$('copy-confirm').onclick=async()=>{
  if(!copyContext)return;
  $('copy-confirm').disabled=true;
  try{
    const target_key=$('copy-target').value,context=copyContext;
    const rule_ids=$('copy-scope').value==='current'?[context.currentId]:context.rules.map(r=>r.id);
    const result=await api('rule/copy',{source_key:context.key,target_key,rule_ids,
      replace_existing:$('copy-mode').value==='replace',expected_target_ids:context.targetIds});
    $('copy-dialog').close();copyContext=null;await pollState();
    toast(`已复制 ${result.copied} 个组合到 ${gearMap.get(target_key).name}${result.skipped?`，跳过 ${result.skipped} 个重复组合`:''}${result.replaced?`，替换 ${result.replaced} 个旧组合`:''}`);
  }catch(error){toast(error.message,true);await pollState();renderCopySummary();}finally{$('copy-confirm').disabled=false;}
};
