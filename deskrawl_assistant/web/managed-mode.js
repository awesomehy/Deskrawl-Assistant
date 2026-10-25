'use strict';
(function(root){
  const growthTools=typeof module!=='undefined'&&module.exports?require('./managed-growth.js'):root.ManagedGrowthUI.helpers;
  const storageTools=typeof module!=='undefined'&&module.exports?require('./automation-tools.js'):root.AutomationTools;
  const auctionTools=typeof module!=='undefined'&&module.exports?require('./auction-ui.js'):root.AuctionUI.helpers;
  const phaseNames={idle:'尚未启动',stopped:'已停止',paused:'已暂停',starting:'准备托管',farming:'刷图中',pushing:'推图中',travel:'进入关卡',returning:'回城中',scanning:'扫描奖励',equipping:'自动换装',cleanup:'整理奖励',collecting:'收取马车 / 地面物品',opening_chests:'开启宝箱',waiting:'等待处理',blocked:'已暂停 · 需要处理',error:'已停止 · 操作异常',growing:'自动成长',shopping:'神秘商人购买',synthesizing:'自动合成'};
  phaseNames.reconnecting='断线重连中';
  phaseNames.auction='拍卖行上传';
  const rarityNames=[['divine','神圣'],['black_mist','黑雾传说'],['ancient','远古传说'],['legendary','普通传说'],['rare','稀有'],['uncommon','优秀'],['normal','普通']];
  const disposalRarities=rarityNames.slice(2),dispositionNames={store:'放入仓库',sell:'出售',salvage:'分解',keep:'留在背包'};
  const storageMaterials=[['gold_bar','金条'],['legendary_rein','传说缰绳']];
  const storageKinds=['equipment','rune','gem',...storageMaterials.map(([key])=>key)];
  const capabilityNames={transfer:'物品转移',lock:'装备保护',equip:'后台换装',travel:'回城 / 进入关卡',difficulty:'主城切换难度',sell:'出售',salvage:'分解',chests:'开启宝箱',ground_pickup:'地面拾取',merchant:'神秘商人消费',talents:'职业天赋加点',life_skills:'生活技能学习',skills:'技能切换',gem_synthesis:'宝石合成',rune_synthesis:'符文合成',auction_upload:'游戏拍卖行上传'};
  const gemQualities=['粗糙','碎裂','粗砺','抛光','无瑕','璀璨'];
  const runeQualities={uncommon:'优秀',rare:'稀有',grand:'宏伟',set:'符文套装'};
  function normalizeSynthesis(value={}){
    const gems=value?.gems||{},runes=value?.runes||{};
    return {gems:{enabled:gems.enabled===true,max_quality:Number.isInteger(gems.max_quality)&&gems.max_quality>=1&&gems.max_quality<=6?gems.max_quality:5},
      runes:{enabled:runes.enabled===true,max_quality:Object.hasOwn(runeQualities,runes.max_quality)?runes.max_quality:'rare',
        excluded_keys:[...new Set((Array.isArray(runes.excluded_keys)?runes.excluded_keys:[]).filter(key=>typeof key==='string'&&key.trim()).map(key=>key.trim()))].sort()}};
  }
  const synthesisEnabled=value=>{const s=normalizeSynthesis(value?.synthesis);return s.gems.enabled||s.runes.enabled;};
  function runeQuality(row){
    const key=String(row.key??row.internal_name??'');
    if(Object.hasOwn(runeQualities,row.quality))return row.quality;
    if(/SetRune\d/.test(key)||row.rarity===5)return 'set';
    if(key.startsWith('LegendaryRune_')||row.rarity===3||row.rarity===4)return 'grand';
    if(key.startsWith('RareRune_')||row.rarity===2)return 'rare';
    return 'uncommon';
  }
  function synthesisRunes(catalog={},query='',excluded=[]){
    const source=catalog.synthesis_runes||catalog.collectibles||[],rows=Array.isArray(source)?source:Object.entries(source).map(([key,row])=>({key,...row})),all=new Map();
    for(const row of rows){const key=row.key??row.internal_name;if(typeof key==='string'&&(row.kind==='rune'||/Rune/.test(key))){const quality=runeQuality({...row,key});all.set(key,{...row,key,name:row.name||key,quality,quality_name:runeQualities[quality],icon:row.icon||'/icons/'+encodeURIComponent(key)+'.png'});}}
    for(const key of excluded)if(!all.has(key))all.set(key,{key,name:key,quality:'uncommon',quality_name:'未收录符文',icon:'',unknown:true});
    const search=query.trim().toLowerCase();
    return [...all.values()].filter(row=>!search||[row.name,row.key,row.quality_name].some(text=>String(text).toLowerCase().includes(search)))
      .sort((a,b)=>Object.keys(runeQualities).indexOf(a.quality)-Object.keys(runeQualities).indexOf(b.quality)||a.name.localeCompare(b.name,'zh-CN')||a.key.localeCompare(b.key));
  }
  function updateRuneExclusions(existing,choices){
    const selected=new Set(existing);
    for(const {key,excluded} of choices)if(excluded)selected.add(key);else selected.delete(key);
    return [...selected].sort();
  }
  const difficultyNames={Normal:'普通',Nightmare:'噩梦',Inferno:'炼狱',Mythic:'神话裂隙'};
  const stargazingRewards=['divine_equipment','black_mist_equipment','primary_perfect','rule_match','set_rune','legendary_rune','rare_rune','uncommon_rune','gem','silver_chest','golden_chest','mythic_chest','legendary_rein','gold_bar'];
  const fixedStargazing=new Set(['divine_equipment','black_mist_equipment','set_rune','legendary_rune']);
  function normalizeStargazing(value={}){value=value||{};return {enabled:value.enabled===true,rewards:Object.fromEntries(stargazingRewards.map(key=>[key,fixedStargazing.has(key)||value.rewards?.[key]!==false]))};}
  const disabledChoice='__disabled_slot__';
  const clone=value=>JSON.parse(JSON.stringify(value));
  const safeNumber=(value,fallback=0)=>Number.isFinite(Number(value))&&value!==null&&value!==''?Number(value):fallback;
  const pageIds=values=>[...new Set((Array.isArray(values)?values:[]).map(Number).filter(n=>Number.isInteger(n)&&n>0))].sort((a,b)=>a-b);
  const oneOf=(value,choices,fallback)=>choices.includes(value)?value:fallback;
  function normalizeSettings(value={}){
    const eq=value.auto_equip||{},farm=value.farming||{},cleanup=value.cleanup||{},background=value.background||{};
    const mode=oneOf(farm.mode,['repeat','push'],'repeat'),repeatMaps=Array.isArray(farm.repeat_maps)&&farm.repeat_maps.length?clone(farm.repeat_maps):farm.dungeon_id!==null&&farm.dungeon_id!==undefined?[{dungeon_id:farm.dungeon_id,difficulty:farm.difficulty||'current',runs:1}]:[];
    return {...growthTools.normalize(value),auction:auctionTools.normalize(value.auction),reconnect:{enabled:value.reconnect?.enabled===true,character_index:safeNumber(value.reconnect?.character_index,1)},synthesis:normalizeSynthesis(value.synthesis),background:{rule_monitoring:background.rule_monitoring!==false,inventory_automation:background.inventory_automation!==false},auto_equip:{enabled:!!eq.enabled,minimum_delta:safeNumber(eq.minimum_delta,1),slot_locks:{...(eq.slot_locks||{})},disabled_slots:Array.isArray(eq.disabled_slots)?[...eq.disabled_slots]:[]},
      farming:{enabled:!!farm.enabled,mode,dungeon_id:mode==='repeat'&&repeatMaps.length?repeatMaps[0].dungeon_id:farm.dungeon_id??null,difficulty:mode==='repeat'&&repeatMaps.length?repeatMaps[0].difficulty:oneOf(farm.difficulty,['current','Normal','Nightmare','Inferno','Mythic'],'current'),progression:Array.isArray(farm.progression)?clone(farm.progression):[],repeat_maps:repeatMaps,free_threshold:safeNumber(farm.free_threshold,4),stargazing:normalizeStargazing(farm.stargazing)},
      cleanup:{equipment_action:oneOf(cleanup.equipment_action,['store','sell','salvage'],'store'),equipment_fallback:oneOf(cleanup.equipment_fallback,['sell','salvage'],'sell'),
        equipment_policies:Object.fromEntries(disposalRarities.map(([key])=>{const p=cleanup.equipment_policies?.[key]||{};return [key,{action:oneOf(p.action,['store','sell','salvage'],oneOf(cleanup.equipment_action,['store','sell','salvage'],'store')),fallback:oneOf(p.fallback,['sell','salvage'],oneOf(cleanup.equipment_fallback,['sell','salvage'],'sell'))}];})),equipment_pages:pageIds(cleanup.equipment_pages??storageTools.defaultPages.equipment),
        rune_action:'store',rune_pages:pageIds(cleanup.rune_pages??storageTools.defaultPages.rune),gem_action:'store',gem_pages:pageIds(cleanup.gem_pages??storageTools.defaultPages.gem),
        ...Object.fromEntries(storageMaterials.flatMap(([key])=>[[key+'_action','store'],[key+'_pages',pageIds(cleanup[key+'_pages']??storageTools.defaultPages.gem)]]))}};
  }
  function equipmentPreset(settings,preset){
    const next=normalizeSettings(settings);
    if(!['sell_low','sell','salvage','store'].includes(preset))throw new Error('装备处理预设无效。');
    for(const [key] of disposalRarities){
      const action=preset==='sell_low'?(['normal','uncommon'].includes(key)?'sell':'salvage'):preset;
      next.cleanup.equipment_policies[key].action=action;
      if(action!=='store')next.cleanup.equipment_policies[key].fallback=action;
    }
    return next;
  }
  function equipmentSummary(cleanup,fallback=false){
    const policies=normalizeSettings({cleanup}).cleanup.equipment_policies;
    return disposalRarities.map(([key,name])=>`${name}：${dispositionNames[policies[key][fallback?'fallback':'action']]}`).join('；');
  }
  function validateSettings(settings,warehouse){
    const auctionError=auctionTools.validation(settings.auction||auctionTools.normalize());if(auctionError)return auctionError;
    const reconnect=settings.reconnect||{enabled:false,character_index:1};
    if(typeof reconnect.enabled!=='boolean'||!Number.isInteger(reconnect.character_index)||reconnect.character_index<1||reconnect.character_index>100)return '断线重连的角色列表序号须为 1 至 100 的整数。';
    const f=settings.farming,c=settings.cleanup;
    const disabled=settings.auto_equip.disabled_slots;
    if(!Array.isArray(disabled)||disabled.some(n=>!Number.isInteger(n)||n<0||n>=12)||new Set(disabled).size!==disabled.length)return '不参与自动换装的部位无效。';
    if(f.enabled&&f.mode==='repeat'&&!f.repeat_maps.length)return '请选择重复刷图关卡。';
    if(f.repeat_maps.length>2||f.repeat_maps.some(row=>row.dungeon_id===null||row.dungeon_id===undefined||row.dungeon_id===''||!['current','Normal','Nightmare','Inferno','Mythic'].includes(row.difficulty)||!Number.isInteger(row.runs)||row.runs<1||row.runs>10000))return '请选择一张或两张关卡，每轮局数为 1 至 10000。';
    if(f.repeat_maps.some(row=>(row.difficulty==='Mythic')!==/^MythicDungeon::mythic::([1-9][0-9]?|100)$/.test(String(row.dungeon_id))))return '神话裂隙请选择已解锁的层数。';
    if(new Set(f.repeat_maps.map(row=>`${row.difficulty}:${row.dungeon_id}`)).size!==f.repeat_maps.length)return '两张重复刷图关卡不能相同。';
    if(!Number.isInteger(f.free_threshold)||f.free_threshold<1||f.free_threshold>2048)return '回城阈值须为 1 至 2048 的整数。';
    if(!c.equipment_pages.length)return '请选择装备存放页，黑雾 / 神圣与符合规则的装备需要保留空间。';
    if(!c.rune_pages.length)return '请勾选符文存放页，所有符文必须收纳。';
    if(!c.gem_pages.length)return '请至少勾选一个宝石存放页，宝石必须收纳。';
    for(const [key,label] of storageMaterials){
      if(!c[key+'_pages'].length)return label+'必须收纳，请至少勾选一个仓库存放页。';
    }
    if(warehouse?.warehouse_ready===false)return '回城整理：仓库开放格位尚未读取完整，请刷新物品清单后再保存或启动。';
    if(warehouse?.warehouse_ready===true){
      const available=new Set((warehouse.warehouse_pages||[]).filter(row=>Number.isInteger(row.index)&&row.index>0&&Number.isInteger(row.capacity)&&row.capacity>0).map(row=>row.index));
      const missing=[['equipment','装备'],['rune','符文'],['gem','宝石'],...storageMaterials].filter(([key])=>!c[key+'_pages'].some(page=>available.has(page))).map(([,label])=>label);
      if(missing.length)return '回城整理：'+missing.join('、')+'必须至少配置 1 个已解锁且有开放格位的仓库页；未解锁页只能作为额外预留。';
    }
    const {merchant:m,life_skills:l}=growthTools.normalize(settings);
    if(Object.values(settings.progression.talent_targets).some(targets=>Object.values(targets).reduce((sum,n)=>sum+n,0)>growthTools.talentPointLimit))return `每个职业的战斗天赋预设最多 ${growthTools.talentPointLimit} 点，请先减少预设点数。`;
    if(!Number.isInteger(m.trigger_percent)||!Number.isInteger(m.spend_to_percent)||m.trigger_percent<1||m.trigger_percent>100||m.spend_to_percent<0||m.spend_to_percent>=m.trigger_percent)return '购买后保留比例须低于碎片回城触发比例。';
    if(Object.values(m.weights).some(n=>!Number.isInteger(n)||n<0||n>1000)||m.enabled&&!Object.values(m.weights).some(n=>n>0))return '购买比例须为 0 至 1000 的整数，至少选择一个部位。';
    if(!Number.isInteger(l.gold_reserve)||l.gold_reserve<0||l.gold_reserve>2147483647)return '生活技能保留金币数量无效。';
    return '';
  }
  function capabilityAvailable(value){return value===true||!!(value&&typeof value==='object'&&(value.native_verified!==false||value.test_enabled===true||value.release_enabled===true)&&(value.available===true||(value.available===undefined&&value.supported===true)));}
  function requiredCapabilities(settings){
    const c=settings.cleanup,keys=[],synthesis=normalizeSynthesis(settings.synthesis);
    const cleanupEnabled=settings.farming.enabled||growthTools.normalize(settings).merchant.enabled||synthesis.gems.enabled||synthesis.runes.enabled||settings.auction?.upload_enabled===true;
    if(cleanupEnabled)keys.push('transfer','lock','chests','ground_pickup');
    else if(settings.auto_equip.enabled)keys.push('transfer');
    if(settings.auto_equip.enabled)keys.push('equip');
    if(settings.background.rule_monitoring&&!keys.includes('lock'))keys.push('lock');
    if(settings.farming.enabled){keys.push('travel');if(settings.farming.mode==='push'||settings.farming.difficulty!=='current'||settings.farming.repeat_maps?.some(row=>row.difficulty!=='current'))keys.push('difficulty');}
    const growth=growthTools.normalize(settings);
    if(growth.progression.talents_enabled)keys.push('talents');if(growth.progression.skills_enabled)keys.push('skills');if(growth.life_skills.enabled)keys.push('life_skills');
    if(synthesis.gems.enabled)keys.push('gem_synthesis','transfer');
    if(synthesis.runes.enabled)keys.push('rune_synthesis','transfer');
    if(settings.auction?.upload_enabled===true)keys.push('auction_upload','transfer');
    if(growth.merchant.enabled)keys.push('merchant','travel','transfer','lock','chests','ground_pickup');
    const actions=cleanupEnabled?Object.values(normalizeSettings(settings).cleanup.equipment_policies).flatMap(p=>[p.action,p.fallback]):[];
    if(actions.includes('sell'))keys.push('sell');if(actions.includes('salvage'))keys.push('salvage');return [...new Set(keys)];
  }
  function controlState(packet,settings,connected,busy=false){
    const running=!!packet?.running,phase=packet?.phase||'idle',growth=growthTools.normalize(settings);
    const missing=requiredCapabilities(settings).filter(key=>!capabilityAvailable(packet?.capabilities?.[key]));
    const configurationError=validateSettings(settings,connected?packet:undefined)||(!settings.auto_equip.enabled&&!settings.farming.enabled&&!synthesisEnabled(settings)&&!auctionTools.enabled(settings.auction)&&!growth.merchant.enabled&&!growth.life_skills.enabled&&!growth.progression.talents_enabled&&!growth.progression.skills_enabled?'请至少启用一项托管功能。':'');
    const merchantBlocked=growth.merchant.enabled&&(packet?.progression_state?.available!==true||packet.progression_state.merchant?.unlock_known!==true||packet.progression_state.merchant?.unlocked!==true);
    const serverReasons=Array.isArray(packet?.blocked_reasons)?packet.blocked_reasons.filter(reason=>typeof reason==='string'&&reason):[];
    const startDisabledReason=packet?.config_error||packet?.start_disabled_reason||serverReasons.join('；')
      ||(merchantBlocked?'神秘商人：尚未解锁或解锁状态未确认。':'')
      ||(settings.farming.enabled&&packet?.control_available===false?'关卡读取：'+(packet.control_error||'等待关卡与场景完整读取。'):'');
    const blocked=packet?.can_start===false||serverReasons.length>0||missing.length>0||!!configurationError||merchantBlocked||(settings.farming.enabled&&packet?.control_available===false);
    return {canStart:!!connected&&!running&&!busy&&!blocked,canPause:running,canStop:running||!!packet?.paused||!['idle','stopped'].includes(phase),canReset:!running&&!packet?.paused&&!busy,stopLabel:packet?.finishing?'立即停止':'回城整理后停止',missing,configurationError,startDisabledReason};
  }
  function statistics(value={}){
    const count=row=>Math.max(0,safeNumber(row?.count)),group=rows=>Array.isArray(rows)?rows:Object.entries(rows||{}).map(([name,row])=>typeof row==='object'?{name,...row}:{name,count:row});
    const equipment=rarityNames.map(([key,name])=>{const row=value.equipment?.[key]||(key==='black_mist'?value.equipment?.mist:null)||{};return {key,name,count:count(row),sold:Math.max(0,safeNumber(row.sold)),salvaged:Math.max(0,safeNumber(row.salvaged)),stored:Math.max(0,safeNumber(row.stored)),estimatedSold:Math.max(0,safeNumber(row.estimated_sold)),estimatedSalvaged:Math.max(0,safeNumber(row.estimated_salvaged))};});
    const runes=group(value.runes).map(row=>({...row,count:count(row)})),gems=group(value.gems).map(row=>({...row,count:count(row)})),others=group(value.others).map(row=>({...row,count:count(row)}));
    const runeCount=runes.reduce((sum,row)=>sum+row.count,0),gemCount=gems.reduce((sum,row)=>sum+row.count,0),otherCount=others.reduce((sum,row)=>sum+row.count,0);
    return {equipment,runes,gems,others,runeCount,gemCount,otherCount,gold:count(value.gold),goldAvailable:value.gold?.available===true,goldReason:value.gold?.reason||'',total:equipment.reduce((sum,row)=>sum+row.count,0)+runeCount+gemCount+otherCount};
  }
  function duration(seconds){const n=Math.max(0,Math.floor(safeNumber(seconds)));return [Math.floor(n/3600),Math.floor(n/60)%60,n%60].map(n=>String(n).padStart(2,'0')).join(':');}
  function selectedDifficulty(settings,packet){return settings.farming.difficulty==='current'?packet?.current_difficulty:settings.farming.difficulty;}
  function availableMaps(packet,settings,difficulty=settings.farming.difficulty){const tier=difficulty==='current'?packet?.current_difficulty:difficulty,rows=packet?.maps_by_difficulty?.[tier]||(tier==='Mythic'?[]:packet?.maps)||[];return tier==='Mythic'?rows.filter(row=>row.unlocked===true):rows.filter(row=>row.map_type!==1);}
  function characterClass(character){
    const mask=character?.class_mask;
    return character?.available===true&&[1,2,4,8].includes(mask)?mask:null;
  }
  function slotEquipment(equipment,slot,character,query=''){
    const mask=characterClass(character),name=slot==='Ring1'||slot==='Ring2'?'Ring':slot,search=query.trim().toLowerCase();
    if(mask===null)return [];
    return (equipment||[]).filter(gear=>gear.legendary===true&&gear.slot===name&&Number.isInteger(gear.class_mask)
      &&gear.class_mask>0&&(gear.class_mask&mask)!==0&&(!search||[gear.name,gear.en,gear.key].some(v=>String(v||'').toLowerCase().includes(search))));
  }
  function withSlotChoice(settings,index,key){
    if(!Number.isInteger(index)||index<0||index>=12||typeof key!=='string')throw new Error('自动换装部位选择无效。');
    const next=normalizeSettings(settings),disabled=new Set(next.auto_equip.disabled_slots);
    if(key===disabledChoice)disabled.add(index);
    else{disabled.delete(index);if(key)next.auto_equip.slot_locks[index]=key;else delete next.auto_equip.slot_locks[index];}
    next.auto_equip.disabled_slots=[...disabled].sort((a,b)=>a-b);return next;
  }
  const foldKeys=['background','equip','farming','merchant','growth','synthesis','auction','cleanup','talents','life','skills'];
  const foldStorageKey='deskrawl-managed-folds-v1';
  function normalizeFoldPreferences(value){
    if(!value||typeof value!=='object'||Array.isArray(value))return {};
    return Object.fromEntries(foldKeys.filter(key=>typeof value[key]==='boolean').map(key=>[key,value[key]]));
  }
  function defaultFolds(value){
    const s=normalizeSettings(value),growth=s.progression.talents_enabled||s.life_skills.enabled||s.progression.skills_enabled;
    return {background:s.background.rule_monitoring||s.background.inventory_automation,equip:s.auto_equip.enabled,
      farming:s.farming.enabled,merchant:s.merchant.enabled,growth,synthesis:synthesisEnabled(s),auction:auctionTools.enabled(s.auction),cleanup:s.farming.enabled||s.merchant.enabled||synthesisEnabled(s)||s.auction.upload_enabled,
      talents:s.progression.talents_enabled,life:s.life_skills.enabled,skills:s.progression.skills_enabled};
  }
  function foldSummaries(value,packet){
    const s=normalizeSettings(value),mask=packet?.progression_state?.class_mask,
      targets=s.progression.talent_targets[String(mask)]||{},stages=s.progression.skill_stages[String(mask)]||[],
      currentClass=[1,2,4,8].includes(mask),points=Object.values(targets).reduce((sum,n)=>sum+n,0),
      disabled=s.auto_equip.disabled_slots.length,named=Object.keys(s.auto_equip.slot_locks).filter(key=>!s.auto_equip.disabled_slots.includes(Number(key))).length,
      buying=Object.values(s.merchant.weights).filter(n=>n>0),growing=[s.progression.talents_enabled?'天赋':'',s.life_skills.enabled?'生活技能':'',s.progression.skills_enabled?'技能':''].filter(Boolean),
      maps=s.farming.repeat_maps.map(row=>{const name=availableMaps(packet,s,row.difficulty).find(map=>String(mapIdValue(map))===String(row.dungeon_id))?.name||row.dungeon_id;return `${name} · ${row.runs} 局`;}),
      liveRepeat=(packet?.running||packet?.paused)&&packet?.settings?.farming?.enabled&&packet.settings.farming.mode==='repeat'&&packet.repeat_maps?.[packet.repeat_index],
      progress=liveRepeat?`图 ${packet.repeat_index+1} · 本轮 ${packet.repeat_completed??0} / ${liveRepeat.runs} 局；`:'',
      folders=kind=>s.cleanup[kind+'_pages'].join(' / ')||'未选择';
    const state=(enabled,summary)=>({enabled,status:enabled?'已启用':'未启用',summary});
    return {
      background:state(s.background.rule_monitoring||s.background.inventory_automation,`规则锁定${s.background.rule_monitoring?'开':'关'} · 自动整理${s.background.inventory_automation?'开':'关'}`),
      equip:state(s.auto_equip.enabled,`暗金限制 ${named} 部位 · 跳过 ${disabled} 部位`),
      farming:state(s.farming.enabled,progress+(s.farming.mode==='push'?'自动推图 · 最高已解锁关卡':maps.length?maps.join(' → '):'重复刷图 · 未选择关卡')+(s.farming.mode==='repeat'&&s.farming.stargazing.enabled?' · 观星开启':'')+` · 空格 < ${s.farming.free_threshold} 回城`),
      merchant:state(s.merchant.enabled,`${s.merchant.trigger_percent}% 回城 · 保留 ${s.merchant.spend_to_percent}% · ${buying.length} 部位 / 每轮 ${buying.reduce((a,b)=>a+b,0)} 件`),
      growth:state(growing.length>0,growing.length?growing.join(' / ')+'自动养成':'战斗天赋 / 生活技能 / 技能预设'),
      synthesis:state(synthesisEnabled(s),`宝石${s.synthesis.gems.enabled?' → '+gemQualities[s.synthesis.gems.max_quality-1]:'关闭'} · 符文${s.synthesis.runes.enabled?' → '+runeQualities[s.synthesis.runes.max_quality]:'关闭'} · 跳过 ${s.synthesis.runes.excluded_keys.length} 种符文`),
      auction:state(auctionTools.enabled(s.auction),`上传${s.auction.upload_enabled?'开':'关'} · 出售${s.auction.sell_enabled?'开':'关'} · 允许 ${s.auction.categories.length} 类 · 保留白名单 ${s.auction.excluded_keys.length} 种优先`),
      cleanup:{enabled:true,status:'固定保护',summary:`装备 ${folders('equipment')} 页 · 符文 ${folders('rune')} 页 · 宝石 ${folders('gem')} 页 · `+storageMaterials.map(([key,label])=>`${label} ${folders(key)} 页`).join(' · ')},
      talents:state(s.progression.talents_enabled,currentClass?`当前职业目标 ${points} / ${growthTools.talentPointLimit} 点`:'按职业保存 · 连接后显示当前预设'),
      life:state(s.life_skills.enabled,`最终点满 · ${s.life_skills.priorities.length} 个优先节点 · 保留 ${s.life_skills.gold_reserve.toLocaleString('zh-CN')} 金币`),
      skills:state(s.progression.skills_enabled,currentClass?`${growthTools.skillTargets(stages).filter(Boolean).length} 个预设技能 · 达到等级后切换`:'按职业保存 · 达到等级后切换')
    };
  }
  const mapIdValue=row=>row.id??row.dungeon_id;
  function createFoldState(elements,storage,onReveal=()=>{}){
    let preferences={};try{preferences=normalizeFoldPreferences(JSON.parse(storage?.getItem(foldStorageKey)||'{}'));}catch(_){/* UI preferences must never block the assistant. */}
    const expected={};let settingsApplied=false;
    const set=(element,open)=>{expected[element.dataset.managedFold]=open;element.open=open;};
    for(const element of elements){
      const key=element.dataset.managedFold;if(!foldKeys.includes(key))continue;
      expected[key]=element.open;
      element.addEventListener('toggle',()=>{
        if(element.open!==expected[key]){
          expected[key]=element.open;preferences[key]=element.open;
          try{storage?.setItem(foldStorageKey,JSON.stringify(preferences));}catch(_){/* Keep the preference for this window when storage is unavailable. */}
        }
        onReveal(key,element.open); // Includes native keyboard activation.
      });
      if(key in preferences)set(element,preferences[key]);
    }
    return {apply(value){
      if(settingsApplied)return;settingsApplied=true;
      const defaults=defaultFolds(value);
      for(const element of elements){const key=element.dataset.managedFold;if(foldKeys.includes(key))set(element,key in preferences?preferences[key]:defaults[key]);}
    }};
  }
  const helpers={normalizeSettings,normalizeStargazing,normalizeSynthesis,synthesisEnabled,synthesisRunes,updateRuneExclusions,runeQuality,gemQualities,runeQualities,validateSettings,pageIds,controlState,requiredCapabilities,statistics,duration,capabilityAvailable,selectedDifficulty,availableMaps,equipmentPreset,equipmentSummary,characterClass,slotEquipment,withSlotChoice,disabledChoice,foldKeys,foldStorageKey,normalizeFoldPreferences,defaultFolds,foldSummaries,createFoldState};
  if(typeof module!=='undefined'&&module.exports)module.exports=helpers;
  if(!root.document)return;
  const $=id=>root.document.getElementById(id);
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let context=null,packet=null,draft=null,dirty=false,connected=false,busy=false,polling=false,active=false,initialized=false;
  let receivedAt=0,pageSignature='',mapSignature='',slotSignature='',statsSignature='',historySignature='',actionVersion=0;
  let currentCharacter=null,pickerSlot=null,pickerSignature='',synthesisSignature='';
  let folds=null;
  let actionError='';
  const farmFields=['managed-farm-mode','managed-difficulty','managed-map','managed-repeat-two','managed-repeat-count','managed-difficulty-two','managed-map-two','managed-repeat-count-two','managed-free-threshold','managed-stargazing'];
  const fieldIds=['managed-reconnect','managed-reconnect-index','managed-rule-monitoring','managed-inventory-automation','managed-auto-equip','managed-farming',...farmFields,'managed-synthesis-gems','managed-synthesis-gems-max','managed-synthesis-runes','managed-synthesis-runes-max'];
  const catalog=()=>context?.catalog||{};
  const maps=()=>availableMaps(packet,draft||normalizeSettings());
  const mapId=row=>row.id??row.dungeon_id;
  const phaseLabel=()=>packet?.phase_label||phaseNames[packet?.phase]||packet?.phase||'尚未启动';
  function markDirty(value=true){dirty=value;if(value)actionError='';const text=dirty?'有未保存的托管设置':'设置已保存';for(const id of ['managed-draft-state','managed-growth-save-state','managed-shop-save-state'])$(id).textContent=text;context?.onDirty?.();}
  function applySharedPages(){
    if(!draft)return;
    draft=root.AutomationTools.withSharedPages(draft,context?.sharedStorage?.current,true);
    if(packet?.settings){
      const baseline=root.AutomationTools.withSharedPages(normalizeSettings(packet.settings),context?.sharedStorage?.saved,true);
      markDirty(JSON.stringify(draft)!==JSON.stringify(baseline));
    }
    pageSignature='';renderPages();
  }
  function report(error){actionError=error?.message||String(error);renderControls();context?.toast?.(actionError,true);}
  function readForm(){
    const next=normalizeSettings({...draft,...root.ManagedGrowthUI?.read?.()}),f=next.farming,c=next.cleanup;
    next.reconnect={enabled:$('managed-reconnect').checked,character_index:Number($('managed-reconnect-index').value)};
    next.background.rule_monitoring=$('managed-rule-monitoring').checked;next.background.inventory_automation=$('managed-inventory-automation').checked;
    next.auto_equip.enabled=$('managed-auto-equip').checked;next.auto_equip.slot_locks={};
    root.document.querySelectorAll('[data-managed-slot]').forEach(select=>{if(select.value)next.auto_equip.slot_locks[select.dataset.managedSlot]=select.value;});
    f.enabled=$('managed-farming').checked;f.mode=$('managed-farm-mode').value;f.free_threshold=Number($('managed-free-threshold').value);
    f.stargazing=normalizeStargazing({enabled:$('managed-stargazing').checked,rewards:Object.fromEntries(stargazingRewards.map(key=>[key,$('managed-stargazing-'+key).checked]))});
    f.difficulty=$('managed-difficulty').value;
    const selectedMap=(id,difficulty)=>{const value=$(id).value,found=availableMaps(packet,next,difficulty).find(row=>String(mapId(row))===value);return found?mapId(found):value||null;};
    f.dungeon_id=selectedMap('managed-map',f.difficulty);
    f.repeat_maps=f.dungeon_id!==null||$('managed-repeat-two').checked?[{dungeon_id:f.dungeon_id,difficulty:f.difficulty,runs:Number($('managed-repeat-count').value)}]:[];
    if($('managed-repeat-two').checked)f.repeat_maps.push({dungeon_id:selectedMap('managed-map-two',$('managed-difficulty-two').value),difficulty:$('managed-difficulty-two').value,runs:Number($('managed-repeat-count-two').value)});
    if(f.mode==='push'&&f.repeat_maps.some(row=>row.dungeon_id===null||!Number.isInteger(row.runs)||row.runs<1||row.runs>10000))f.repeat_maps=[];
    if(f.mode==='repeat')f.progression=[]; // Only the backend declares the campaign route.
    for(const [key] of disposalRarities){const p=c.equipment_policies[key];for(const field of ['action','fallback'])p[field]=$(`managed-equipment-${key}-${field}`).value;}
    next.synthesis.gems={enabled:$('managed-synthesis-gems').checked,max_quality:Number($('managed-synthesis-gems-max').value)};
    next.synthesis.runes.enabled=$('managed-synthesis-runes').checked;next.synthesis.runes.max_quality=$('managed-synthesis-runes-max').value;
    next.synthesis.runes.excluded_keys=updateRuneExclusions(next.synthesis.runes.excluded_keys,[...root.document.querySelectorAll('[data-synthesis-rune-key]')].map(input=>({key:input.dataset.synthesisRuneKey,excluded:input.checked})));
    for(const kind of storageKinds)c[kind+'_pages']=pageIds([...root.document.querySelectorAll(`[data-managed-pages="${kind}"]:checked`)].map(input=>input.value));
    next.auction=root.AuctionUI?.read(next.auction)||next.auction;
    return next;
  }
  function slots(){
    if(Array.isArray(catalog().equipped_slot_options))return catalog().equipped_slot_options;
    return ['Weapon','Helm','Chest','Pants','Boots','Belt','Ring','Ring','Necklace','Shoulder','Gloves','Back'].map((slot,index)=>({index,slot,name:['武器','头盔','胸甲','裤子','靴子','腰带','戒指 1','戒指 2','项链','护肩','手套','披风'][index]}));
  }
  const character=()=>connected?currentCharacter:null;
  function className(){const mask=characterClass(character());return (catalog().classes||[]).find(row=>row.value===mask)?.name||'当前职业';}
  function gearIcon(gear){return gear?.disabled?'<svg class="managed-prohibited-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6 18.4 18.4"/></svg>':gear?.icon?`<img src="${esc(gear.icon)}" alt="" loading="lazy">`:'<span aria-hidden="true">◇</span>';}
  function closePicker(restoreFocus=false){
    const button=pickerSlot===null?null:root.document.querySelector(`[data-managed-picker="${pickerSlot}"]`);
    $('managed-gear-menu').hidden=true;pickerSlot=null;pickerSignature='';
    root.document.querySelectorAll('[data-managed-picker]').forEach(input=>input.setAttribute('aria-expanded','false'));
    if(restoreFocus)button?.focus();
  }
  function positionPicker(){
    if(pickerSlot===null)return;
    const button=root.document.querySelector(`[data-managed-picker="${pickerSlot}"]`),menu=$('managed-gear-menu');
    if(!button||!button.getClientRects().length){closePicker();return;}
    const rect=button.getBoundingClientRect(),margin=8,gap=5,width=Math.min(Math.max(rect.width,280),root.innerWidth-margin*2);
    if(rect.bottom<=0||rect.top>=root.innerHeight){closePicker();return;}
    const below=root.innerHeight-rect.bottom-margin-gap,above=rect.top-margin-gap,up=below<240&&above>below;
    menu.style.width=width+'px';menu.style.left=Math.max(margin,Math.min(rect.left,root.innerWidth-width-margin))+'px';
    menu.style.maxHeight=Math.max(0,Math.min(360,up?above:below))+'px';
    menu.style.top=up?'auto':rect.bottom+gap+'px';menu.style.bottom=up?root.innerHeight-rect.top+gap+'px':'auto';
  }
  function renderSlots(){
    const mask=characterClass(character()),signature=JSON.stringify([slots(),catalog().equipment,draft?.auto_equip.slot_locks,draft?.auto_equip.disabled_slots,mask,connected]);if(signature===slotSignature)return;slotSignature=signature;
    $('managed-class-scope').textContent=mask===null?(connected?'正在读取角色职业，读取完成后显示可用装备。':'连接游戏后，自动按当前职业筛选装备。'):`当前职业：${className()} · 仅显示本职业及通用装备`;
    $('managed-slot-locks').innerHTML=slots().map(row=>{
      const index=String(row.index??row.value??row.id),slot=row.slot==='Ring1'||row.slot==='Ring2'?'Ring':row.slot;
      const options=slotEquipment(catalog().equipment,slot,character()),selected=draft?.auto_equip.slot_locks[index]||'',gear=(catalog().equipment||[]).find(g=>g.key===selected);
      const excluded=draft?.auto_equip.disabled_slots.includes(Number(index));
      const unavailable=selected&&!options.some(g=>g.key===selected),note=unavailable?(mask===null?'等待读取职业':gear?'当前职业不可用':'暂未收录'):'点击选择暗金装备';
      const unknown=selected&&!options.some(g=>g.key===selected)?`<option value="${esc(selected)}" selected>${esc(selected)} · 暂未收录</option>`:'';
      const label=row.name||row.label||slot,disabled=packet?.running||busy||!draft?.auto_equip.enabled;
      return `<div class="managed-slot-field"><span>${esc(label)}</span><button class="managed-gear-select ${excluded?'excluded':unavailable?'unavailable':''}" type="button" data-managed-picker="${esc(index)}" aria-label="${esc(label)}自动换装设置" aria-haspopup="listbox" aria-controls="managed-gear-choices" aria-expanded="${pickerSlot===index}" ${disabled?'disabled':''}><span class="managed-gear-icon">${gearIcon(excluded?{disabled:true}:gear)}</span><span class="managed-gear-label"><strong>${esc(excluded?'不参与自动换装':gear?.name||selected||'不限制名称')}</strong><small>${esc(excluded?'保持当前部位装备':selected?note:'所有可穿戴品质')}</small></span><span class="managed-gear-caret" aria-hidden="true">⌄</span></button><select data-managed-slot="${esc(index)}" hidden aria-hidden="true" tabindex="-1"><option value="">不限制名称 · 所有品质</option>${unknown}${options.map(g=>`<option value="${esc(g.key)}" ${g.key===selected?'selected':''}>${esc(g.name)}</option>`).join('')}</select></div>`;
    }).join('');
    if(pickerSlot!==null){renderPicker();positionPicker();}
  }
  function renderPicker(){
    const row=slots().find(row=>String(row.index??row.value??row.id)===pickerSlot);if(!row)return;
    const query=$('managed-gear-search').value,options=slotEquipment(catalog().equipment,row.slot,character(),query),selected=draft?.auto_equip.disabled_slots.includes(Number(pickerSlot))?disabledChoice:draft?.auto_equip.slot_locks[pickerSlot]||'';
    const signature=JSON.stringify([pickerSlot,characterClass(character()),query,options,selected]);if(signature===pickerSignature)return;pickerSignature=signature;
    $('managed-gear-choices').setAttribute('aria-label',(row.name||row.label||row.slot)+'自动换装选项');
    $('managed-gear-scope').textContent=characterClass(character())===null?'连接游戏后显示可用装备':`${className()}可用 · ${options.length} 件装备`;
    const choice=(gear)=>`<button class="managed-gear-choice ${gear.disabled?'excluded ':''}${gear.key===selected?'selected':''}" type="button" role="option" data-managed-choice="${esc(gear.key)}" aria-selected="${gear.key===selected}" title="${esc(gear.description||'所有可穿戴品质均可参与战力比较')}"><span class="managed-gear-icon">${gearIcon(gear)}</span><span><strong>${esc(gear.name)}</strong><small>${esc(gear.disabled?'保持当前部位装备':gear.key?gear.en||gear.slot_label||'传说装备':'所有可穿戴品质')}</small></span><b aria-hidden="true">${gear.key===selected?'✓':''}</b></button>`;
    $('managed-gear-choices').innerHTML=choice({key:disabledChoice,name:'不参与自动换装',disabled:true,description:'跳过此部位的自动换装，保留已配置的暗金限制。'})+choice({key:'',name:'不限制名称'})+options.map(choice).join('')+(!options.length?'<p class="managed-picker-empty">'+esc(characterClass(character())===null?'等待读取职业':'没有符合搜索条件的装备')+'</p>':'');
  }
  function openPicker(index){
    if(packet?.running||busy||!draft?.auto_equip.enabled)return;
    if(!slots().some(row=>String(row.index??row.value??row.id)===index))return;
    if(pickerSlot===index){closePicker(true);return;}
    closePicker();pickerSlot=index;pickerSignature='';$('managed-gear-search').value='';renderPicker();
    $('managed-gear-menu').hidden=false;
    root.document.querySelector(`[data-managed-picker="${index}"]`).setAttribute('aria-expanded','true');
    positionPicker();if(pickerSlot!==null)$('managed-gear-search').focus();
  }
  function selectGear(key){
    if(pickerSlot===null||packet?.running||busy||!draft?.auto_equip.enabled)return;
    const row=slots().find(row=>String(row.index??row.value??row.id)===pickerSlot);
    if(!row||key&&key!==disabledChoice&&!slotEquipment(catalog().equipment,row.slot,character()).some(gear=>gear.key===key))return;
    const previous=readForm(),next=withSlotChoice(previous,Number(pickerSlot),key);
    if(JSON.stringify(previous)!==JSON.stringify(next)){draft=next;markDirty();slotSignature='';renderSlots();}
    closePicker(true);renderDependencies();renderCapabilities();
  }
  function renderPages(){
    const rows=packet?.warehouse_pages||[],ready=connected&&packet?.warehouse_ready===true,signature=JSON.stringify([rows,ready,draft?.cleanup]);if(signature===pageSignature)return;pageSignature=signature;
    for(const kind of storageKinds){
      const choices=storageTools.pageChoices(rows,draft?.cleanup[kind+'_pages']||[],ready);
      $('managed-'+kind+'-pages').innerHTML=choices.map(row=>{
        const label=row.status==='available'?`${row.free} 空`:row.status==='locked'?'未解锁':'待读取';
        const title=row.status==='available'?`剩余 ${row.free} / ${row.capacity} 格`:row.status==='locked'?'该页尚未解锁；保留选择，解锁后自动使用。也可取消并选择其他页。':'连接后读取实际仓库权限。';
        return `<label class="${row.status==='available'?'':'storage-page-unavailable'}" title="${esc(title)}"><input type="checkbox" data-managed-pages="${kind}" value="${row.index}" ${row.selected?'checked':''}><span>${esc(row.name)}</span><small>${esc(label)}</small></label>`;
      }).join('')||'<small class="managed-pages-empty">连接后读取仓库页签</small>';
    }
  }
  function renderSynthesis(){
    if(!draft)return;
    const s=draft.synthesis,disabled=!!packet?.running||busy,query=$('managed-synthesis-rune-search').value;
    $('managed-synthesis-gems-max').disabled=disabled||!s.gems.enabled;
    $('managed-synthesis-runes-max').disabled=disabled||!s.runes.enabled;
    $('managed-synthesis-rune-search').disabled=disabled;
    $('managed-synthesis-gems-note').textContent=s.gems.max_quality===1?'粗糙是最低品质，没有可执行的合成配方。':`最多消耗${gemQualities[s.gems.max_quality-2]}合成至${gemQualities[s.gems.max_quality-1]}，不消耗${gemQualities[s.gems.max_quality-1]}。`;
    const runeKeys=Object.keys(runeQualities),index=runeKeys.indexOf(s.runes.max_quality);
    $('managed-synthesis-runes-note').textContent=index===0?'优秀是最低品质，没有可执行的合成配方。':`最多消耗${runeQualities[runeKeys[index-1]]}合成至${runeQualities[s.runes.max_quality]}，不消耗${runeQualities[s.runes.max_quality]}。`+(index===3?' 消耗宏伟符文需要已解锁炼狱。':'');
    const options=synthesisRunes(catalog(),query,s.runes.excluded_keys),signature=JSON.stringify([options,s.runes.excluded_keys,disabled]);
    $('managed-synthesis-rune-count').textContent=`已跳过 ${s.runes.excluded_keys.length} 种 · 当前显示 ${options.length} 种`;
    if(signature===synthesisSignature)return;synthesisSignature=signature;
    const selected=new Set(s.runes.excluded_keys),focusedKey=root.document.activeElement?.dataset?.synthesisRuneKey;
    $('managed-synthesis-rune-options').innerHTML=options.map(row=>`<label class="synthesis-rune-choice ${selected.has(row.key)?'selected':''}" data-quality="${row.quality}" title="${esc(row.key)}"><input type="checkbox" data-synthesis-rune-key="${esc(row.key)}" ${selected.has(row.key)?'checked':''} ${disabled?'disabled':''}><span class="synthesis-rune-icon">${row.icon?`<img src="${esc(row.icon)}" alt="" loading="lazy">`:'◇'}</span><span class="synthesis-rune-label"><strong>${esc(row.name)}</strong><small>${esc(row.quality_name)} · 所有等级</small></span></label>`).join('')||'<p class="synthesis-empty">没有符合搜索条件的符文</p>';
    if(focusedKey)[...$('managed-synthesis-rune-options').querySelectorAll('[data-synthesis-rune-key]')].find(input=>input.dataset.synthesisRuneKey===focusedKey)?.focus();
  }
  function renderMaps(){
    for(const id of ['managed-difficulty','managed-difficulty-two'])for(const option of $(id).options){
      if(option.value==='current'){option.textContent='沿用启动时当前难度'+(difficultyNames[packet?.current_difficulty]?' · '+difficultyNames[packet.current_difficulty]:'');continue;}
      const native=packet?.difficulties?.find(row=>row.id===option.value);
      option.disabled=native?.unlocked===false||(option.value==='Mythic'&&native?.unlocked!==true);
      option.textContent=difficultyNames[option.value]+(native?.unlocked===false?' · 未解锁':'');
    }
    const target=packet?.automatic_start,tier=difficultyNames[target?.difficulty],name=target?availableMaps(packet,draft,target.difficulty).find(r=>String(mapId(r))===String(target.dungeon_id))?.name:null;
    $('managed-push-target').textContent=target?`${tier} · ${name||target.dungeon_id}`:connected?'等待读取已解锁关卡':'连接后读取';
    const progress=$('managed-farm-progress'),showProgress=(packet?.running||packet?.paused)&&packet?.settings?.farming?.enabled;
    progress.hidden=!showProgress;
    if(showProgress){
      const liveName=availableMaps(packet,draft,packet.target_difficulty).find(r=>String(mapId(r))===String(packet.target_dungeon))?.name||packet.target_dungeon;
      const repeat=packet.settings.farming.mode==='repeat'&&packet.repeat_maps?.[packet.repeat_index];
      progress.textContent=`当前目标：${difficultyNames[packet.target_difficulty]||'当前难度'} · ${liveName}${repeat?` · 图 ${packet.repeat_index+1} 本轮已完成 ${packet.repeat_completed} / ${repeat.runs} 局`:''}`;
    }
    const star=$('managed-stargazing-status'),live=packet?.stargazing;
    star.hidden=!showProgress||!packet?.settings?.farming?.stargazing?.enabled||packet.settings.farming.mode!=='repeat';
    star.textContent=`观星已跳过 ${safeNumber(live?.skipped)} 局 · ${live?.reason||'等待本局掉落计划'}${live?.hits?.length?' 命中：'+live.hits.map(hit=>`${hit.name}（${hit.labels.join('、')}）`).join('；'):''}`;
    const signature=JSON.stringify([packet?.maps_by_difficulty,packet?.maps,draft?.farming.repeat_maps,connected,packet?.control_error]);if(signature===mapSignature)return;mapSignature=signature;
    for(const [i,id] of [[0,'managed-map'],[1,'managed-map-two']]){
      const point=draft?.farming.repeat_maps[i],selected=point?.dungeon_id,rows=availableMaps(packet,draft,point?.difficulty||(i===0?draft.farming.difficulty:'current')),unknown=selected!==null&&selected!==undefined&&!rows.some(row=>String(mapId(row))===String(selected));
      $(i===0?'managed-map-label':'managed-map-label-two').textContent=point?.difficulty==='Mythic'?'层数':'关卡';
      $(id).innerHTML='<option value="">'+(rows.length?(point?.difficulty==='Mythic'?'请选择层数':'请选择关卡'):point?.difficulty==='Mythic'&&connected?'暂无已解锁层数':!connected?'连接后读取关卡':packet?.control_error?'关卡暂不可读':'正在读取关卡…')+'</option>'+(unknown&&point?.difficulty!=='Mythic'?`<option value="${esc(selected)}" selected>关卡 ${esc(selected)} · 待读取</option>`:'')+rows.map(row=>`<option value="${esc(mapId(row))}" ${String(mapId(row))===String(selected)?'selected':''} ${row.automation_supported===false||row.unlocked===false?'disabled':''}>${esc(row.name||row.label||'关卡 '+mapId(row))}${row.unlocked===false?' · 未解锁':''}</option>`).join('');
    }
  }
  function writeForm(){
    if(!draft)return;
    $('managed-reconnect').checked=draft.reconnect.enabled;$('managed-reconnect-index').value=draft.reconnect.character_index;
    $('managed-rule-monitoring').checked=draft.background.rule_monitoring;$('managed-inventory-automation').checked=draft.background.inventory_automation;
    $('managed-auto-equip').checked=draft.auto_equip.enabled;$('managed-farming').checked=draft.farming.enabled;$('managed-farm-mode').value=draft.farming.mode;$('managed-free-threshold').value=draft.farming.free_threshold;
    $('managed-difficulty').value=draft.farming.difficulty;
    $('managed-repeat-two').checked=draft.farming.repeat_maps.length===2;
    $('managed-repeat-count').value=draft.farming.repeat_maps[0]?.runs||1;
    $('managed-difficulty-two').value=draft.farming.repeat_maps[1]?.difficulty||'current';
    $('managed-repeat-count-two').value=draft.farming.repeat_maps[1]?.runs||1;
    $('managed-stargazing').checked=draft.farming.stargazing.enabled;
    for(const key of stargazingRewards)$('managed-stargazing-'+key).checked=draft.farming.stargazing.rewards[key];
    $('managed-synthesis-gems').checked=draft.synthesis.gems.enabled;$('managed-synthesis-gems-max').value=draft.synthesis.gems.max_quality;
    $('managed-synthesis-runes').checked=draft.synthesis.runes.enabled;$('managed-synthesis-runes-max').value=draft.synthesis.runes.max_quality;
    for(const [key] of disposalRarities)for(const field of ['action','fallback'])$(`managed-equipment-${key}-${field}`).value=draft.cleanup.equipment_policies[key][field];
    renderSlots();renderMaps();renderPages();renderDependencies();
  }
  function renderDependencies(){
    const running=!!packet?.running;
    fieldIds.forEach(id=>{$(id).disabled=running||busy;});
    for(const id of farmFields)$(id).disabled=running||busy||!$('managed-farming').checked;
    root.document.querySelectorAll('[data-managed-slot]').forEach(input=>input.disabled=running||busy||!$('managed-auto-equip').checked);
    root.document.querySelectorAll('[data-managed-picker]').forEach(input=>input.disabled=running||busy||!$('managed-auto-equip').checked);
    if(running||busy||!$('managed-auto-equip').checked)closePicker();
    for(const kind of storageKinds)root.document.querySelectorAll(`[data-managed-pages="${kind}"]`).forEach(input=>input.disabled=running||busy);
    root.document.querySelectorAll('[data-managed-policy], [data-managed-preset]').forEach(input=>input.disabled=running||busy);
    for(const id of ['managed-save','managed-growth-save','managed-shop-save'])$(id).disabled=running||busy||!draft;
    $('managed-reset-draft').disabled=running||busy||!dirty;
    const push=$('managed-farm-mode').value==='push',two=$('managed-repeat-two').checked;
    $('managed-push-preview').hidden=!push;$('managed-repeat-fields').hidden=push;$('managed-repeat-second').hidden=!two;
    $('managed-stargazing-options').hidden=!$('managed-stargazing').checked;
    for(const key of stargazingRewards)$('managed-stargazing-'+key).disabled=fixedStargazing.has(key)||running||busy||!$('managed-farming').checked||!$('managed-stargazing').checked;
    root.document.querySelector('.managed-repeat-count-label').hidden=!two;
    $('managed-farm-note').textContent=push?'从最高已解锁难度的最后一张已解锁图开始；通关前进一关，失败后退一关，上一关通关后再次挑战。普通通关后进入噩梦，噩梦通关后进入炼狱。':two?'依次打满图 1、图 2 的成功通关局数后循环；观星退出、失败和途中回城均不计数。':'重复进入选定关卡，仅成功通关计数；神话裂隙只显示已解锁层数，每次入场消耗游戏要求的材料；背包空格不足时先回城整理，再继续刷图。';
    root.ManagedGrowthUI?.render(draft,packet?.progression_catalog,packet?.progression_state,running||busy,(next,renderNow=true)=>{
      draft={...readForm(),...next};markDirty();if(renderNow){renderDependencies();renderCapabilities();}else {renderFoldSummaries();renderControls();}
    },connected);
    root.AuctionUI?.render(draft?.auction,catalog(),running||busy);
    renderSynthesis();renderFoldSummaries();renderControls();
  }
  function renderFoldSummaries(){
    const rows=foldSummaries(draft||normalizeSettings(),connected?packet:null);
    for(const key of foldKeys){
      const row=rows[key],summary=root.document.querySelector(`[data-managed-fold-summary="${key}"]`),status=root.document.querySelector(`[data-managed-fold-status="${key}"]`);
      if(summary)summary.textContent=row.summary;
      if(status){status.textContent=row.status;status.classList.toggle('enabled',row.enabled);}
    }
  }
  function renderControls(){
    const controls=controlState(packet,draft||normalizeSettings(),connected,busy);
    $('managed-stop').textContent=controls.stopLabel;$('managed-stop').title=packet?.finishing?'立即中止整理，等待当前已提交动作结束。':'返回主城，整理背包、马车和地面奖励后停止。';
    $('managed-start').disabled=!controls.canStart;$('managed-start').hidden=!!packet?.running;$('managed-start').textContent=packet?.paused?'继续托管':'启动托管';
    $('managed-pause').hidden=!packet?.running;$('managed-pause').disabled=!controls.canPause;$('managed-stop').disabled=!controls.canStop;$('managed-reset-stats').disabled=!controls.canReset;
    const reason=controls.configurationError||(controls.missing.length?'尚未通过验证：'+controls.missing.map(key=>capabilityNames[key]).join('、'):'')||controls.startDisabledReason||(packet?.can_start===false?'启动条件尚未满足，请刷新状态。':'');
    $('managed-start').title=!connected?'请先连接游戏':reason||'按保存的设置开始托管';
    let error=actionError||packet?.error||(connected&&!packet?.running?reason:'');
    if(error&&!actionError&&!packet?.error&&dirty&&packet?.can_start===false&&!controls.configurationError)error+=' 如已修改相关功能，请先保存托管设置。';
    $('managed-error').textContent=String(error||'');$('managed-error').hidden=!error;
  }
  function renderCapabilities(){
    const requirements=requiredCapabilities(draft||normalizeSettings());
    $('managed-test-notice').hidden=packet?.capabilities?.test_mode!==true;
    $('managed-capabilities').innerHTML=requirements.map(key=>{const value=packet?.capabilities?.[key],available=capabilityAvailable(value),testing=available&&value?.test_enabled===true,reason=typeof value==='object'?value?.reason:'';return `<span class="managed-capability ${available?'available':'unavailable'}" title="${esc(reason||(available?'已验证可用':'尚未验证，启动前需要完成适配'))}"><i></i>${esc(capabilityNames[key])}<small>${testing?'测试可用':available?'可用':'待验证'}</small></span>`;}).join('');
  }
  function renderStats(){
    const signature=JSON.stringify(packet?.stats||{});if(signature===statsSignature)return;statsSignature=signature;
    const data=statistics(packet?.stats);$('managed-loot-total').textContent=data.total.toLocaleString('zh-CN')+' 件';
    $('managed-gold-total').textContent=data.gold.toLocaleString('zh-CN');
    $('managed-gold-total').title=data.goldAvailable?'本次托管实际拾取的金币；不含出售收入，不受金币消费影响。':(data.goldReason||'等待读取游戏金币掉落统计。')+' 已累计的数量保留，读取恢复后继续累计。';
    $('managed-equipment-stats').innerHTML=data.equipment.map(row=>`<tr class="managed-rarity-${row.key}"><td>${esc(row.name)}</td><td>${row.count.toLocaleString('zh-CN')}</td><td title="${row.estimatedSold||row.estimatedSalvaged?'包含按稳定马车策略估算的低品质处置数量':'已核验的助手回执、游戏马车记录或系统日志'}"><span>${row.estimatedSold?'≈ ':''}${row.sold.toLocaleString('zh-CN')}</span><small> / </small><span>${row.estimatedSalvaged?'≈ ':''}${row.salvaged.toLocaleString('zh-CN')}</span></td></tr>`).join('');
    for(const [kind,label,rows,count] of [['rune','符文',data.runes,data.runeCount],['gem','宝石',data.gems,data.gemCount],['other','其他物品',data.others,data.otherCount]]){
      $('managed-'+kind+'-total').textContent=count.toLocaleString('zh-CN')+' 个';
      $('managed-'+kind+'-stats').innerHTML=rows.length?rows.map(row=>`<div data-rarity="${kind!=='gem'&&Number.isInteger(row.rarity)?row.rarity:''}"><span title="${esc(row.name||row.name_key||row.key)}">${esc(row.name||row.name_key||row.key||'未命名物品')}${row.level?` <small>Lv.${esc(row.level)}</small>`:''}</span><strong>${row.count.toLocaleString('zh-CN')}</strong><small>已出售 ${Math.max(0,safeNumber(row.sold)).toLocaleString('zh-CN')} · 已存入 ${Math.max(0,safeNumber(row.stored)).toLocaleString('zh-CN')}</small></div>`).join(''):'<p class="managed-stat-empty">尚无'+label+'掉落</p>';
    }
    if(packet?.stats?.note)$('managed-stats-note').textContent=packet.stats.note;
  }
  function renderHistory(){
    const rows=packet?.history||[],signature=JSON.stringify(rows);if(signature===historySignature)return;historySignature=signature;
    $('managed-history-count').textContent=rows.length+' 条';
    $('managed-history-list').innerHTML=rows.length?[...rows].reverse().slice(0,50).map(row=>{
      const time=row.time||row.timestamp,date=new Date(time),label=time?(Number.isNaN(date.getTime())?String(time):date.toLocaleTimeString('zh-CN',{hour12:false})):'';
      return `<li class="${row.level==='error'?'managed-history-error':''}"><time>${esc(label)}</time><p>${esc(row.message||row.text||'')}</p></li>`;
    }).join(''):'<li class="managed-history-empty">尚未启动托管</li>';
  }
  function renderTime(){const stats=packet?.stats||{},extra=packet?.running?Math.max(0,(Date.now()-receivedAt)/1000):0;$('managed-elapsed').textContent=duration(safeNumber(stats.elapsed_seconds??packet?.elapsed_seconds)+extra);}
  function render(){
    $('managed-phase').textContent=packet?.finishing?'结束中 · '+phaseLabel():phaseLabel();$('managed-message').textContent=packet?.message||(!connected?'请先连接游戏，读取关卡和仓库页签。':'配置托管策略后，点击启动。');
    $('managed-status-dot').className='managed-status-dot '+(packet?.running?'running':packet?.phase==='error'||packet?.phase==='blocked'?'error':'');
    renderMaps();renderPages();renderSlots();renderDependencies();renderCapabilities();renderStats();renderHistory();renderTime();
  }
  function applyPacket(value){
    if(!value||typeof value!=='object')return;
    const next=value.managed||value;packet={...next,stats:next.stats||next.statistics,message:next.message||next.status,paused:next.paused??next.phase==='paused'};receivedAt=Date.now();actionError='';
    if(!dirty&&packet.settings){draft=normalizeSettings(packet.settings);writeForm();}
    if(packet.settings)folds?.apply(draft||packet.settings);
    if(!draft){draft=normalizeSettings();writeForm();}applySharedPages();render();
  }
  async function refresh(){
    if(!context||polling)return;polling=true;
    try{applyPacket(await context.api('managed'));}
    catch(error){report(error);}finally{polling=false;}
  }
  async function save(){
    draft=readForm();const error=validateSettings(draft,connected?packet:undefined);if(error)throw new Error(error);
    const submitted=clone(draft),value=await context.api('managed/settings',{settings:submitted});
    context.sharedStorage?.commit(root.AutomationTools.sharedPages(value.settings,true),root.AutomationTools.sharedPages(submitted,true));
    markDirty(false);applyPacket(value);context.toast?.('托管设置已保存，共用存放页已同步');return true;
  }
  async function runAction(action){
    if(!context||busy&&!['pause','stop'].includes(action))return;
    const ticket=++actionVersion;busy=true;actionError='';renderDependencies();
    try{
      if(action==='save')await save();
      if(action==='start'){
        draft=readForm();const error=validateSettings(draft,connected?packet:undefined);if(error)throw new Error(error);
        if(!draft.auto_equip.enabled&&!draft.farming.enabled&&!synthesisEnabled(draft)&&!auctionTools.enabled(draft.auction)&&!growthTools.enabled(draft))throw new Error('请至少启用一项托管功能。');
        if(dirty)await save();
        if(ticket!==actionVersion)return;
        const c=draft.cleanup;
        const growthNotice=[draft.progression.talents_enabled?'按当前职业的目标逐点学习天赋，不重置已有天赋。':'',draft.progression.skills_enabled?'到达设定等级并解锁后切换目标技能组合。':'',draft.life_skills.enabled?`按优先顺序学习生活技能，随后补满；至少保留 ${draft.life_skills.gold_reserve} 金币。`:'',draft.merchant.enabled?'灵魂碎片达到实际容量的回城比例后，整理奖励并按部位比例消费；新购买的装备重新筛选整理。':''].filter(Boolean).join('\n');
        const synthesisNotice=synthesisEnabled(draft)?'仅在主城整理时按最高产出品质，从低到高合成背包与已解锁仓库材料。'+(draft.synthesis.gems.enabled?`\n宝石合成至${gemQualities[draft.synthesis.gems.max_quality-1]}，会消耗金币。`:'')+(draft.synthesis.runes.enabled?`\n符文合成至${runeQualities[draft.synthesis.runes.max_quality]}，每次消耗 9 枚；概率合成失败也消耗，已有等级和经验不保留。已锁定、已装备及跳过名单中的 ${draft.synthesis.runes.excluded_keys.length} 种符文不参与。`:''):'';
        const auctionNotice=auctionTools.enabled(draft.auction)?`仅处理勾选的 ${draft.auction.categories.length} 类商品。保留白名单的 ${draft.auction.excluded_keys.length} 种物品始终优先，不自动上传或出售。`+(draft.auction.upload_enabled?'\n允许将符合原生交易条件的允许列表物品移出游戏，上传至 Steam 库存；包含本来需要保护入库的高价值物品。':'')+(draft.auction.upload_enabled?'\n锁定状态不影响上传；不会自动解锁，已绑定、强化、有孔或已穿戴物品仍不参与。':'')+(draft.auction.sell_enabled?`\n允许在 Steam 市场按${auctionTools.priceRules[draft.auction.price_rule]}自动出售本工具精确匹配的允许列表上传资产。报价为买方支付总价，币种以 Steam 钱包为准。手机确认中的出售不重复提交。`:''):'';
        const cleanupNotice=(draft.farming.enabled||draft.merchant.enabled||synthesisEnabled(draft))?`不符合规则的装备按品质处理：\n${equipmentSummary(c)}。\n仓库满时清理所选页的未锁定垃圾：\n${equipmentSummary(c,true)}。\n所有符文、宝石、金条、传说缰绳必须入库，指定页满时留在背包并暂停，腾出容量后继续。出售和分解无法撤回，强化和宝石不自动返还。神圣、黑雾装备必须收纳；已锁定、符合规则与待核验装备保留。神圣装备禁止自动穿戴，主城奖励未收完不得入图。`:draft.auto_equip.enabled?'按固定单体 Boss 战力更换更高战力的装备。神圣装备禁止自动穿戴。未指定暗金时允许其他品质；指定但尚未穿上时允许普通、优秀、稀有装备过渡，穿上后只比较同名装备。不自动转移宝石。':'';
        const confirmed=await context.confirmAction?.('启动托管？',[growthNotice,synthesisNotice,auctionNotice,cleanupNotice].filter(Boolean).join('\n\n'),'启动托管',false);
        if(confirmed===false||ticket!==actionVersion)return;
        const result=await context.api('managed/run',{enabled:true});if(ticket===actionVersion)applyPacket(result);
      }
      if(action==='pause'){const result=await context.api('managed/run',{enabled:false});if(ticket===actionVersion)applyPacket(result);}
      if(action==='stop'){const result=await context.api(packet?.finishing?'managed/abort':'managed/stop',{});if(ticket===actionVersion)applyPacket(result);}
      if(action==='reset'){
        const confirmed=await context.confirmAction?.('清空本次统计？','只清空托管的累计掉落和本次流程记录，本机物品操作日志保留。','清空统计',false);
        if(confirmed===false)return;applyPacket(await context.api('managed/reset',{}));
      }
    }catch(error){if(ticket===actionVersion)report(error);}finally{if(ticket===actionVersion){busy=false;renderDependencies();renderCapabilities();}}
  }
  function init(options){
    context=options;if(initialized)return;initialized=true;root.ManagedGrowthUI?.init();draft=normalizeSettings();
    root.AuctionUI?.init({api:context.api,toast:context.toast,confirmAction:context.confirmAction,onChange:()=>{draft=readForm();markDirty();renderDependencies();renderCapabilities();}});
    context.sharedStorage?.subscribe(applySharedPages);
    let storage=null;try{storage=root.localStorage;}catch(_){/* Storage access can be blocked independently of the UI. */}
    folds=createFoldState([...root.document.querySelectorAll('[data-managed-fold]')],storage,(key,open)=>{
      if(key==='equip'&&!open)closePicker();
      if(open)root.requestAnimationFrame?.(()=>root.ManagedGrowthUI?.reveal?.());
    });
    writeForm();render();
    $('managed-page').addEventListener('change',event=>{
      if(event.target.matches('input,select')&&event.target.id!=='managed-synthesis-rune-search'&&!event.target.closest('#managed-growth, #managed-auction')){
        draft=readForm();
        const storageKind=event.target.dataset.managedPages;
        if(['rune','gem'].includes(storageKind))context.sharedStorage?.edit({[storageKind]:draft.cleanup[storageKind+'_pages']});
        if(['managed-difficulty','managed-difficulty-two','managed-repeat-two'].includes(event.target.id)){
          const index=event.target.id==='managed-difficulty-two'?1:0,point=draft.farming.repeat_maps[index],selected=point&&availableMaps(packet,draft,point.difficulty).find(row=>String(mapId(row))===String(point.dungeon_id));
          if(point&&(!selected||selected.automation_supported===false||selected.unlocked===false)){point.dungeon_id=null;if(index===0)draft.farming.dungeon_id=null;}
          mapSignature='';renderMaps();
        }
        markDirty();renderDependencies();renderCapabilities();
      }
    });
    for(const id of ['managed-free-threshold','managed-repeat-count','managed-repeat-count-two'])$(id).addEventListener('input',()=>{draft=readForm();markDirty();renderFoldSummaries();renderControls();});
    for(const [id,action] of [['managed-save','save'],['managed-growth-save','save'],['managed-shop-save','save'],['managed-start','start'],['managed-pause','pause'],['managed-stop','stop'],['managed-reset-stats','reset']])$(id).addEventListener('click',()=>runAction(action));
    $('managed-refresh').addEventListener('click',refresh);
    $('managed-slot-locks').addEventListener('click',event=>{const button=event.target.closest('[data-managed-picker]');if(button)openPicker(button.dataset.managedPicker);});
    $('managed-gear-search').addEventListener('input',renderPicker);
    $('managed-synthesis-rune-search').addEventListener('input',renderSynthesis);
    $('managed-gear-choices').addEventListener('click',event=>{const button=event.target.closest('[data-managed-choice]');if(button)selectGear(button.dataset.managedChoice);});
    root.document.addEventListener('pointerdown',event=>{
      if(pickerSlot!==null&&!event.target.closest('#managed-gear-menu, [data-managed-picker]'))closePicker();
    });
    root.document.addEventListener('focusin',event=>{
      if(pickerSlot!==null&&!event.target.closest('#managed-gear-menu, [data-managed-picker]'))closePicker();
    });
    root.document.addEventListener('keydown',event=>{
      if(pickerSlot!==null&&event.key==='Escape'){event.preventDefault();closePicker(true);}
    });
    $('managed-gear-menu').addEventListener('keydown',event=>{
      const choices=[...$('managed-gear-choices').querySelectorAll('[data-managed-choice]')];
      if(!choices.length)return;
      const index=choices.indexOf(root.document.activeElement);
      if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)&&!(event.target.id==='managed-gear-search'&&['Home','End'].includes(event.key))){
        event.preventDefault();
        const target=event.key==='Home'?0:event.key==='End'?choices.length-1:event.key==='ArrowDown'?(index+1)%choices.length:index<0?choices.length-1:(index-1+choices.length)%choices.length;
        choices[target].focus();
      }
    });
    $('managed-lock-details').addEventListener('toggle',()=>{if(!$('managed-lock-details').open)closePicker();});
    root.addEventListener('resize',positionPicker);
    root.document.addEventListener('scroll',positionPicker,true);
    root.document.querySelectorAll('[data-managed-preset]').forEach(button=>button.addEventListener('click',()=>{
      if(packet?.running||busy)return;draft=equipmentPreset(readForm(),button.dataset.managedPreset);markDirty();pageSignature='';writeForm();renderCapabilities();
    }));
    $('managed-reset-draft').addEventListener('click',()=>{draft=normalizeSettings(packet?.settings);context.sharedStorage?.reset();markDirty(false);applySharedPages();slotSignature='';pageSignature='';mapSignature='';writeForm();render();});
    root.setInterval(()=>{renderTime();if(active||packet?.running)refresh();},1500);
  }
  function setConnection(value){
    connected=!!value;
    // Reconnecting must wait for a fresh character snapshot, rather than
    // temporarily resurrecting the previous connection's profession.
    if(!connected&&packet)packet={...packet,progression_state:{available:false,reason:'连接游戏后读取成长状态。'}};
    renderPages();renderDependencies();renderSlots();renderControls();
  }
  function setCharacter(value){currentCharacter={available:value?.available===true,class_mask:value?.class_mask};renderSlots();}
  function setActive(value){active=!!value;if(active)refresh();else closePicker();}
  root.ManagedModeUI={init,refresh,applyPacket,setConnection,setCharacter,setActive,isDirty:()=>dirty,helpers};
})(typeof window!=='undefined'?window:globalThis);
