'use strict';
(function(root){
  const copy=v=>JSON.parse(JSON.stringify(v));
  const talentPointLimit=70;
  function normalize(value={}){
    const p=value.progression||{},l=value.life_skills||{},m=value.merchant||{};
    return {progression:{talents_enabled:!!p.talents_enabled,skills_enabled:!!p.skills_enabled,talent_targets:copy(p.talent_targets||{}),skill_stages:copy(p.skill_stages||{})},
      life_skills:{enabled:!!l.enabled,priorities:copy(l.priorities||[]),gold_reserve:l.gold_reserve??0},
      merchant:{enabled:!!m.enabled,trigger_percent:m.trigger_percent??85,spend_to_percent:m.spend_to_percent??0,weights:Object.fromEntries(Array.from({length:10},(_,n)=>[String(n+1),m.weights?.[n+1]??0]))}};
  }
  const total=targets=>Object.values(targets).reduce((sum,n)=>sum+n,0);
  function activeHero(catalog,current,connected){
    if(connected!==true||current?.available!==true||![1,2,4,8].includes(current.class_mask))return null;
    const key=String(current.class_mask);return catalog?.heroes?.[key]?key:null;
  }
  function talentProgress(nodes,points){
    const gates=[...new Set(nodes.filter(n=>n.selectable!==false).map(n=>n.point_requirement||0))].sort((a,b)=>a-b);
    if(gates.length<2||!Number.isFinite(points)||points<=gates[0])return 0;
    for(let i=1;i<gates.length;i++)if(points<gates[i])return (i-1+(points-gates[i-1])/(gates[i]-gates[i-1]))/(gates.length-1);
    return 1;
  }
  function talentAllowed(node,nodes,ranks){
    const byId=Object.fromEntries(nodes.map(n=>[n.id,n]));
    return node.selectable!==false&&(ranks[node.id]||0)<node.max_points&&total(ranks)>=(node.point_requirement||0)
      &&(!node.prerequisite||(ranks[node.prerequisite]||0)>=byId[node.prerequisite].max_points)
      &&(!node.exclusive_group||!nodes.some(n=>n.id!==node.id&&n.exclusive_group===node.exclusive_group&&ranks[n.id]>0));
  }
  function talentChange(nodes,targets,key,amount){
    const next={...targets},byId=Object.fromEntries(nodes.map(n=>[n.id,n])),node=byId[key];if(!node||node.selectable===false)return next;
    if(amount>0)amount=Math.min(amount,Math.max(0,talentPointLimit-total(targets)));
    if(amount===0)return next;
    next[key]=Math.max(0,Math.min(node.max_points,(next[key]||0)+amount));
    const parents=(id,seen=new Set())=>{if(seen.has(id))throw new Error('天赋前置关系出现循环。');seen.add(id);const n=byId[id];if(n.prerequisite){next[n.prerequisite]=byId[n.prerequisite].max_points;parents(n.prerequisite,seen);}};
    if(next[key]>0){if(node.exclusive_group)for(const n of nodes)if(n.id!==key&&n.exclusive_group===node.exclusive_group)next[n.id]=0;parents(key);}
    // Reducing early points cannot leave unreachable descendants or point gates.
    const reachable={};let progressed=true;
    while(progressed){progressed=false;for(const n of nodes)if((reachable[n.id]||0)<(next[n.id]||0)&&talentAllowed(n,nodes,reachable)){reachable[n.id]=(reachable[n.id]||0)+1;progressed=true;}}
    // Filling prerequisite chains also consumes the same 70-point budget.
    return total(reachable)>talentPointLimit?{...targets}:reachable;
  }
  function lifePath(nodes,priorities){
    const byId=Object.fromEntries(nodes.map(n=>[n.id,n])),path=new Set();
    function visit(key,seen=new Set()){if(seen.has(key))throw new Error('生活技能前置关系出现循环。');seen.add(key);const n=byId[key];if(!n)return;if(n.prerequisite)visit(n.prerequisite,seen);path.add(key);}
    priorities.forEach(key=>visit(key));return [...path];
  }
  const lifeRemove=(nodes,priorities,key)=>priorities.filter(id=>!lifePath(nodes,[id]).includes(key));
  const skillTargets=(stages=[])=>[...stages].sort((a,b)=>a.level-b.level).reduce((slots,stage)=>slots.map((old,index)=>stage.slots[index]??old),[null,null,null,null]);
  function skillChoice(stages,slot,key){const slots=skillTargets(stages),other=slots.indexOf(key);if(slots[slot]===key)slots[slot]=null;else{if(other>=0)slots[other]=slots[slot];slots[slot]=key;}return [{level:1,slots}];}
  function description(node,rank=1){
    return String(node.description||'').replace(/\{([^}]+)\}/g,(match,key)=>{const t=node.description_tokens?.[key];if(!t)return '（随等级成长）';
      const percent=t.format?.includes('%'),value=(t.base+t.per_rank*(Math.max(1,rank)-1))*(percent?100:1),digits=(t.format?.split('.')[1]||'').replace('%','').length;
      return Number(value.toFixed(digits)).toLocaleString('zh-CN',{maximumFractionDigits:digits})+(percent?'%':'')+(t.suffix||'');});
  }
  function enabled(value){const s=normalize(value);return s.progression.talents_enabled||s.progression.skills_enabled||s.life_skills.enabled||s.merchant.enabled;}
  const helpers={normalize,activeHero,talentProgress,talentChange,talentAllowed,talentPointLimit,lifePath,lifeRemove,skillTargets,skillChoice,description,enabled};
  if(typeof module!=='undefined'&&module.exports)module.exports=helpers;if(!root.document)return;
  const $=id=>root.document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const setText=(id,value)=>{const node=$(id),text=String(value??'');if(node.textContent!==text)node.textContent=text;};
  let settings=null,data=null,state=null,disabled=false,onChange=null,selected=null,connected=false,tab='talents',specialSlot=2,search='',lifeZoom=.42,lifeCentered=false,drag=null;
  let talentSignature='',skillSignature='',lifeSignature='',lifeSummaryMarkup='',prioritySignature='',shopSignature='',tooltip=null;
  let previousTalentClass=null,previousTalentPoints=0;
  const slots=['普通攻击','强力攻击','特殊技能 1','特殊技能 2'];
  const bindings=[['managed-merchant','merchant','enabled'],['managed-shard-trigger','merchant','trigger_percent'],['managed-shard-reserve','merchant','spend_to_percent'],['managed-talents','progression','talents_enabled'],['managed-skills','progression','skills_enabled'],['managed-life','life_skills','enabled'],['managed-life-gold','life_skills','gold_reserve']];
  const live=()=>!!selected&&connected&&state?.available&&String(state.class_mask)===selected;
  const skillsLive=()=>live()&&state.skills?.readable!==false&&Array.isArray(state.skills?.equipped)&&Array.isArray(state.skills?.available);
  const waitingMessage=()=>!connected?'请先连接游戏，识别当前角色职业后显示此页。':'正在读取当前角色职业，请稍候。';
  function read(){const next=normalize(settings||{});for(const [id,key,field] of bindings){const input=$(id);if(input)next[key][field]=input.type==='checkbox'?input.checked:Number(input.value);}root.document.querySelectorAll('[data-shop-weight]').forEach(input=>next.merchant.weights[input.dataset.shopWeight]=Number(input.value));return next;}
  function update(fn,renderNow=true){if(disabled)return;const next=read();fn(next);onChange?.(next,renderNow);}
  function priority(key){update(next=>{const list=next.life_skills.priorities,index=list.indexOf(key);if(index<0)list.push(key);else list.splice(index,1);});}
  function setTab(value){tab=value;hideTooltip();for(const name of ['talents','life','skills']){$('growth-view-'+name).hidden=name!==tab;$('growth-tab-'+name).setAttribute('aria-selected',String(name===tab));$('growth-tab-'+name).tabIndex=name===tab?0:-1;}if(tab==='life')centerLife();}
  function renderLife(){
    const nodes=data?.life_skills||[],list=settings.life_skills.priorities,path=new Set(lifePath(nodes,list)),ranks=state?.available?state.life_skills.ranks:{},byId=Object.fromEntries(nodes.map(n=>[n.id,n]));
    const learned=nodes.filter(n=>ranks[n.id]>=n.max_points).length,remaining=nodes.reduce((sum,n)=>sum+(ranks[n.id]>=n.max_points?0:n.gold_costs[0]||0),0);
    const summary=`<h3>总览</h3><strong>最终 ${nodes.length} / ${nodes.length} 个节点</strong><p>优先路径 <b>${path.size}</b> 个节点</p><p>已学习 <b>${state?.available?learned:'—'}</b> / ${nodes.length}</p><p>待学金币 <b>${remaining.toLocaleString()}</b></p><p>当前金币 <b>${state?.available?state.gold.toLocaleString():'—'}</b></p><hr><p class="growth-green">优先路径为绿色连线</p><p class="growth-gold">金色编号为执行顺序</p><p>左键选优先节点，右键取消该节点及其后续优先路径。</p><p>拖动空白区域平移，使用 + / − 缩放。</p>`;
    if(summary!==lifeSummaryMarkup){lifeSummaryMarkup=summary;$('growth-life-summary').innerHTML=summary;}
    const psig=JSON.stringify([list,disabled]);if(psig!==prioritySignature){prioritySignature=psig;$('managed-life-priorities').innerHTML=list.map((key,index)=>{const n=byId[key];return `<li><img src="${esc(n?.icon)}" alt=""><span>${index+1}. ${esc(n?.name||key)}</span><small>${(n?.gold_costs[0]||0).toLocaleString()} 金币</small><button data-life-up="${esc(key)}" type="button" ${disabled||index===0?'disabled':''} aria-label="上移 ${esc(n?.name)} 的优先级">↑</button><button data-life-remove="${esc(key)}" type="button" ${disabled?'disabled':''} aria-label="移除 ${esc(n?.name)} 优先路径">×</button></li>`;}).join('')||'<li class="muted">尚未指定优先路径，将按前置关系和金币消耗逐步点满。</li>';}
    const sig=JSON.stringify([list,ranks,search,disabled,data?.build]);if(sig===lifeSignature)return;lifeSignature=sig;
    if(!nodes.length){$('managed-life-nodes').innerHTML='<p class="muted">等待载入生活技能资源。</p>';return;}
    const minX=Math.min(...nodes.map(n=>n.x)),maxX=Math.max(...nodes.map(n=>n.x)),minY=Math.min(...nodes.map(n=>n.y)),maxY=Math.max(...nodes.map(n=>n.y)),width=maxX-minX+180,height=maxY-minY+180,x=n=>n.x-minX+90,y=n=>n.y-minY+90;
    const match=n=>!search||[n.name,description(n),n.id].some(s=>s.toLowerCase().includes(search.toLowerCase()));
    // CSP blocks styles parsed from HTML attributes. Apply layout through the
    // trusted external script, like the other positioned menus in the app.
    $('managed-life-nodes').innerHTML=`<div class="growth-life-space"><div class="growth-life-canvas"><svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${nodes.filter(n=>n.prerequisite).map(n=>`<path class="${path.has(n.id)?'planned':ranks[n.id]?'learned':''}" d="M ${x(byId[n.prerequisite])} ${y(byId[n.prerequisite])} L ${x(n)} ${y(n)}"/>`).join('')}</svg>${nodes.map(n=>{const index=list.indexOf(n.id),planned=path.has(n.id);return `<button type="button" data-life-node="${esc(n.id)}" data-growth-tooltip="life" class="growth-life-node ${planned?'selected':''} ${ranks[n.id]?'learned':''} ${match(n)?'':'search-dim'} ${index>=0?'priority':''}" ${disabled?'disabled':''} aria-pressed="${index>=0}" aria-label="${esc(n.name)}，${index>=0?'优先顺序 '+(index+1):planned?'优先路径的前置':'点击设为优先'}"><img src="${esc(n.icon)}" alt="">${index>=0?`<b>${index+1}</b>`:''}${ranks[n.id]?'<i aria-label="已学习">✓</i>':''}</button>`;}).join('')}</div></div>`;
    const viewport=$('managed-life-nodes'),space=viewport.querySelector('.growth-life-space'),canvas=viewport.querySelector('.growth-life-canvas');
    space.style.width=width*lifeZoom+'px';space.style.height=height*lifeZoom+'px';
    canvas.style.width=width+'px';canvas.style.height=height+'px';canvas.style.transform=`scale(${lifeZoom})`;
    viewport.querySelectorAll('[data-life-node]').forEach(button=>{const n=byId[button.dataset.lifeNode];button.style.left=x(n)+'px';button.style.top=y(n)+'px';});
    $('growth-life-zoom-label').textContent=Math.round(lifeZoom*100)+'%';if(tab==='life')centerLife();
  }
  function centerLife(force=false){const viewport=$('managed-life-nodes'),canvas=viewport.querySelector?.('.growth-life-canvas');if(!canvas||(!force&&lifeCentered)||!viewport.clientWidth)return;const nodes=data.life_skills,minX=Math.min(...nodes.map(n=>n.x)),minY=Math.min(...nodes.map(n=>n.y));viewport.scrollLeft=(90-minX)*lifeZoom-viewport.clientWidth/2;viewport.scrollTop=(90-minY)*lifeZoom-viewport.clientHeight/2;lifeCentered=true;}
  function zoomLife(amount){const viewport=$('managed-life-nodes'),old=lifeZoom,x=(viewport.scrollLeft+viewport.clientWidth/2)/old,y=(viewport.scrollTop+viewport.clientHeight/2)/old;lifeZoom=Math.max(.3,Math.min(.9,Math.round((lifeZoom+amount)*100)/100));lifeSignature='';renderLife();viewport.scrollLeft=x*lifeZoom-viewport.clientWidth/2;viewport.scrollTop=y*lifeZoom-viewport.clientHeight/2;}
  function renderTalents(){
    if(!selected){
      previousTalentClass=null;previousTalentPoints=0;
      setText('managed-talent-total','尚未识别当前职业');setText('growth-talent-feedback','');
      const signature='waiting:'+connected;
      if(talentSignature!==signature){talentSignature=signature;$('managed-talent-tree').innerHTML=`<div class="growth-empty"><strong>${connected?'等待职业数据':'尚未连接游戏'}</strong><p>${waitingMessage()}</p></div>`;}return;
    }
    const hero=data?.heroes?.[selected],nodes=(hero?.talents||[]).filter(n=>n.selectable!==false),targets=settings.progression.talent_targets[selected]||{},ranks=live()?state.talents.ranks:{};
    setText('managed-talent-total',`预设投入 ${total(targets)} / ${talentPointLimit} 点${live()?' · 当前已投入 '+total(ranks)+' · 可用 '+state.talents.unspent+' 点':''}`);
    const sig=JSON.stringify([selected,targets,ranks,disabled,data?.build]);if(sig===talentSignature)return;talentSignature=sig;if(!nodes.length){$('managed-talent-tree').innerHTML='<p class="muted">等待载入天赋资源。</p>';return;}
    const ys=[...new Set(nodes.map(n=>n.y))].sort((a,b)=>a-b),minX=Math.min(...nodes.map(n=>n.x)),maxX=Math.max(...nodes.map(n=>n.x)),x=n=>14+(n.x-minX)*78/Math.max(1,maxX-minX),y=n=>50+ys.indexOf(n.y)*96,byId=Object.fromEntries(nodes.map(n=>[n.id,n]));
    const points=total(targets),progress=talentProgress(nodes,points),sameClass=previousTalentClass===selected;
    const oldHeight=sameClass?$('managed-talent-tree').querySelector('.growth-talent-fill')?.style.height:null;
    const lastGate=Math.max(...nodes.map(n=>n.point_requirement||0));
    $('managed-talent-tree').innerHTML=`<div class="growth-tree-canvas"><div class="growth-talent-rail" role="progressbar" aria-label="预设天赋投入进度" aria-valuemin="0" aria-valuemax="${lastGate}" aria-valuenow="${Math.min(points,lastGate)}" aria-valuetext="预设投入 ${points} 点"><div class="growth-talent-track"><div class="growth-talent-fill"></div><div class="growth-talent-pointer"></div></div><div class="growth-talent-rail-frame"></div></div>${ys.map((row,index)=>{const n=nodes.find(n=>n.y===row),core=n.exclusive_group,reached=points>=(n.point_requirement||0),newlyReached=sameClass&&previousTalentPoints<n.point_requirement&&reached;return `<div class="growth-talent-row ${core?'core':''}" data-talent-row="${index}"><span class="growth-gate ${reached?'available':''} ${newlyReached?'just-lit':''}" aria-label="投入 ${n.point_requirement} 点门槛，${reached?'已解锁':'未解锁'}">${n.point_requirement}</span>${core?`<strong>核心天赋 ${['Capstone10','Capstone30','Capstone60'].indexOf(core)+1}</strong>`:''}</div>`;}).join('')}<svg viewBox="0 0 720 ${ys.length*96+35}" preserveAspectRatio="none" aria-hidden="true">${nodes.filter(n=>n.prerequisite).map(n=>`<path d="M ${x(byId[n.prerequisite])*7.2} ${y(byId[n.prerequisite])} L ${x(n)*7.2} ${y(n)}"/>`).join('')}</svg>${nodes.map(n=>{const rank=targets[n.id]||0,gate=points>=(n.point_requirement||0);return `<button data-talent-node="${esc(n.id)}" data-growth-tooltip="talent" class="growth-talent ${rank?'selected':''} ${gate?'':'gated'} ${n.exclusive_group?'core':''}" type="button" ${disabled?'disabled':''} aria-label="${esc(n.name)}，目标 ${rank}/${n.max_points}" aria-pressed="${rank>0}"><img src="${esc(n.icon)}" alt=""><span>${rank}/${n.max_points}</span>${ranks[n.id]?`<i aria-label="实际已学 ${ranks[n.id]} 点">${ranks[n.id]}</i>`:''}</button>`;}).join('')}</div>`;
    const tree=$('managed-talent-tree');tree.querySelector('.growth-tree-canvas').style.height=ys.length*96+35+'px';
    const rail=tree.querySelector('.growth-talent-rail'),fill=tree.querySelector('.growth-talent-fill'),pointer=tree.querySelector('.growth-talent-pointer');
    rail.style.top='50px';rail.style.height=(ys.length-1)*96+'px';
    const targetHeight=progress*100+'%';fill.style.height=oldHeight||targetHeight;pointer.style.top=oldHeight||targetHeight;
    // Commit the old geometry so click-driven growth and shrinkage animate.
    fill.getBoundingClientRect?.();fill.style.height=targetHeight;pointer.style.top=targetHeight;
    previousTalentClass=selected;previousTalentPoints=points;
    tree.querySelectorAll('[data-talent-row]').forEach(row=>{row.style.top=Number(row.dataset.talentRow)*96+12+'px';});
    tree.querySelectorAll('[data-talent-node]').forEach(button=>{const n=byId[button.dataset.talentNode];button.style.left=x(n)+'%';button.style.top=y(n)+'px';});
  }
  function renderSkills(){
    if(!selected){setText('growth-skill-status','尚未识别当前职业');setText('growth-skills-note','');
      const signature='waiting:'+connected;
      if(skillSignature!==signature){skillSignature=signature;$('managed-skill-stages').innerHTML=`<div class="growth-empty"><strong>${connected?'等待职业数据':'尚未连接游戏'}</strong><p>${waitingMessage()}</p></div>`;}return;
    }
    const hero=data?.heroes?.[selected],stages=settings.progression.skill_stages[selected]||[],targets=skillTargets(stages),abilities=hero?.abilities||[],currentReadable=skillsLive(),waitingForSkills=live()&&!currentReadable;
    setText('growth-skill-status',live()?`${hero.name} · 当前 ${state.level} 级${waitingForSkills?' · 技能状态待读取':''}`:'未连接时也可预设技能');
    const planNote=stages.some(s=>s.level!==1)||stages.length>1?'原等级阶段仍按已保存设置执行。点选图标后，本职业改为一套预设，按各技能自身解锁等级切换。':'未预设的栏位保留当前技能。两个特殊技能分别配置，点击右侧栏位选择要替换的目标。';
    setText('growth-skills-note',(waitingForSkills?'当前技能待读取：'+(state.skills?.reason||'等待当前技能栏读取。')+' 可继续配置技能预设。':'')+planNote);
    const sig=JSON.stringify([selected,stages,live()?state.skills:null,live()?state.level:null,disabled,specialSlot,data?.build]);if(sig===skillSignature)return;skillSignature=sig;const byId=Object.fromEntries(abilities.map(a=>[a.id,a]));
    const icon=a=>{const target=targets.indexOf(a.id),current=currentReadable?state.skills.equipped.indexOf(a.id):-1,waiting=live()&&(state.level<a.level_requirement||(currentReadable&&!state.skills.available.includes(a.id)));return `<button type="button" data-skill-id="${esc(a.id)}" data-growth-tooltip="skill" class="growth-skill-icon ${target>=0?'selected':''} ${current>=0?'equipped':''} ${waiting?'locked-level':''}" ${disabled?'disabled':''} aria-pressed="${target>=0}" aria-label="${esc(a.name)}，${a.level_requirement} 级解锁${target>=0?'，已预设 '+slots[target]:''}"><img src="${esc(a.icon)}" alt="">${target>=2?`<b>${target-1}</b>`:''}<small>${a.level_requirement}级</small></button>`;};
    $('managed-skill-stages').innerHTML=`<div class="growth-skill-book"><h3>${esc(hero?.name||'')} · 法术</h3>${['普通攻击','强力攻击','特殊'].map((title,category)=>`<section><h4>${title}</h4><div class="growth-skill-grid">${abilities.filter(a=>category===0?a.slots.includes(0):category===1?a.slots.includes(1):a.slots.includes(2)).map(icon).join('')}</div></section>`).join('')}</div><aside class="growth-skill-loadout"><h3>目标技能</h3>${slots.map((label,index)=>{const target=byId[targets[index]],current=currentReadable?byId[state.skills.equipped[index]]:null,waiting=target&&live()&&(state.level<target.level_requirement||(currentReadable&&!state.skills.available.includes(target.id)));return `<button type="button" class="growth-skill-target ${index>=2&&specialSlot===index?'active':''}" ${index>=2?`data-special-slot="${index}"`:''} ${disabled||index<2?'disabled':''}><span>${label}</span><strong>${target?`<img src="${esc(target.icon)}" alt="">${esc(target.name)}`:'沿用当前技能'}</strong><small>${target?(waiting?`${target.level_requirement} 级解锁，等待解锁后切换`:`${target.level_requirement} 级解锁`):'不主动更改此栏位'+(waitingForSkills?'；当前技能待读取':'')}</small><em>当前：${esc(current?.name||(currentReadable?'未装备':'未读取'))}</em></button>`;}).join('')}<p><span class="growth-gold">金框</span> 预设技能 · <span class="growth-green">绿点</span> 当前技能</p></aside>`;
  }
  function hideTooltip(){if(tooltip)tooltip.hidden=true;}
  function showTooltip(button,event){
    if(!tooltip||!data)return;const kind=button.dataset.growthTooltip,hero=data.heroes[selected];let node,extra='',rank=1;
    if(kind==='talent'){node=hero.talents.find(n=>n.id===button.dataset.talentNode);rank=settings.progression.talent_targets[selected]?.[node.id]||1;extra=`目标 ${settings.progression.talent_targets[selected]?.[node.id]||0} / ${node.max_points} 点${live()?' · 实际 '+(state.talents.ranks[node.id]||0)+' 点':''}\n需先投入 ${node.point_requirement} 点${node.exclusive_group?' · 同组核心只能选一项':''}`;}
    else if(kind==='life'){node=data.life_skills.find(n=>n.id===button.dataset.lifeNode);const parent=data.life_skills.find(n=>n.id===node.prerequisite),index=settings.life_skills.priorities.indexOf(node.id);extra=`学习消耗 ${(node.gold_costs[0]||0).toLocaleString()} 金币${parent?'\n前置：'+parent.name:''}${index>=0?'\n优先顺序 '+(index+1):''}${state?.available?'\n实际：'+(state.life_skills.ranks[node.id]?'已学习':'未学习'):''}`;}
    else{node=hero.abilities.find(n=>n.id===button.dataset.skillId);extra=`${node.level_requirement} 级解锁${live()?' · 当前 '+state.level+' 级':''}`;}
    tooltip.innerHTML=`<strong>${esc(node.name)}</strong><p>${esc(description(node,rank))}</p><small>${esc(extra)}</small>`;tooltip.hidden=false;const rect=button.getBoundingClientRect(),width=tooltip.offsetWidth||300,height=tooltip.offsetHeight||150;let x=event?.clientX||rect.right,y=event?.clientY||rect.top;
    if(x+width+20>root.innerWidth)x=Math.max(8,x-width-12);else x+=14;if(y+height+20>root.innerHeight)y=Math.max(8,root.innerHeight-height-12);else y+=14;tooltip.style.left=x+'px';tooltip.style.top=y+'px';
  }
  function renderShop(){
    const container=$('managed-shop-weights'),rows=data?.shop||[],shardIcon=data?.ui_assets?.['soul-shard'];
    const signature=JSON.stringify([rows.map(row=>[row.slot,row.name,row.icon]),shardIcon]);
    if(signature!==shopSignature){
      shopSignature=signature;
      container.innerHTML=rows.map(row=>`<label data-shop-slot="${row.slot}" class="growth-shop-item"><img class="growth-shop-icon" src="${esc(row.icon)}" alt=""><span class="growth-shop-name">神秘${esc(row.name)}</span><span class="growth-shop-price"><span class="growth-shop-price-value"></span><img src="${esc(shardIcon)}" alt="灵魂碎片"></span><span class="growth-shop-quantity">每轮<input aria-label="神秘${esc(row.name)}每轮购买件数" type="number" min="0" max="1000" data-shop-weight="${row.slot}">件</span></label>`).join('');
    }
    const shop=state?.available?state.merchant:null,preview=shop?.preview||shop?.stock||[];
    for(const item of container.querySelectorAll('[data-shop-slot]')){
      const slot=item.dataset.shopSlot,input=item.querySelector('[data-shop-weight]'),price=item.querySelector('.growth-shop-price'),value=price.querySelector('.growth-shop-price-value');
      const quote=preview.find(row=>String(row.slot)===slot),valid=quote&&Number.isInteger(quote.price)&&quote.price>0;
      const text=valid?String(quote.price):connected?'待读取':'待连接';
      if(value.textContent!==text)value.textContent=text;
      const title=valid?(quote.price_source==='level'||quote.price_reference?'游戏等级定价：基础价格 × max(1,等级÷10取整)；在线购买前核对服务器报价':'游戏当前报价'):shop?.reason||'连接后读取报价';
      if(price.title!==title)price.title=title;
      item.classList.toggle('selected',settings.merchant.weights[slot]>0);
      input.disabled=disabled;
      if(root.document.activeElement!==input&&input.value!==String(settings.merchant.weights[slot]))input.value=settings.merchant.weights[slot];
    }
    setText('managed-shop-price-note',preview.some(row=>row.price_source==='level'||row.price_reference)?'按游戏的当前等级定价公式显示；在线购买前会刷新并核对服务器报价。':preview.length?'已读取游戏报价。':connected?'正在读取商人价格。':'连接后显示价格，购买计划可提前配置。');
  }
  function render(value,catalog,current,readonly,changed,isConnected=false){
    if(!$('managed-growth'))return;settings=normalize(value);data=catalog;disabled=readonly;onChange=changed;connected=isConnected;state=connected?current:null;
    const nextClass=activeHero(data,state,connected);if(nextClass!==selected)hideTooltip();selected=nextClass;
    const shop=state?.available?state.merchant:null,merchantUnlocked=connected&&shop?.unlock_known===true&&shop.unlocked===true;
    for(const [id,key,field] of bindings){const input=$(id);if(input.type==='checkbox')input.checked=settings[key][field];else if(root.document.activeElement!==input)input.value=settings[key][field];input.disabled=disabled||(['managed-talents','managed-skills'].includes(id)&&!selected)||(id==='managed-merchant'&&!merchantUnlocked&&!input.checked);}
    const requirements=shop?.unlock_requirements||data?.shop_unlock;
    const conditions=requirements?[`${requirements.level} 级`,`${requirements.gold} 金币`,...requirements.materials.map(row=>`${row.count} ${row.name}`)].join('、'):'';
    setText('managed-merchant-unlock',merchantUnlocked?'神秘商人已解锁':!connected?'连接后确认商人解锁状态':shop?.unlock_known?'神秘商人未解锁'+(conditions?' · 游戏内解锁需要 '+conditions:''):'神秘商人解锁状态待读取');
    $('managed-merchant').title=merchantUnlocked?'已完成游戏内解锁，可启用自动购买':'未确认已解锁时不能开启；旧配置若已开启，可先关闭';
    $('managed-skills').title=live()&&!skillsLive()?'技能状态待读取，托管会在技能栏读取完成后切换：'+(state.skills?.reason||'等待当前技能栏读取。'):'按预设切换已解锁的技能';
    setText('managed-shard-state',state?.available?`灵魂碎片 ${state.soul_shards.current} / ${state.soul_shards.maximum} · 金币 ${state.gold.toLocaleString()}`:state?.reason||'连接后读取实际灵魂碎片上限和成长状态。');
    renderShop();
    setText('managed-growth-class',selected?data.heroes[selected].name+' · 当前职业':'连接后识别职业');
    $('managed-talent-clear').disabled=disabled||!selected;$('growth-life-clear').disabled=disabled;$('growth-skills-clear').disabled=disabled||!selected;renderLife();renderTalents();renderSkills();
  }
  function init(){
    for(const [id,key,field] of bindings){$(id).addEventListener('change',event=>update(next=>{next[key][field]=event.target.type==='checkbox'?event.target.checked:Number(event.target.value);}));if($(id).type==='number')$(id).addEventListener('input',event=>update(next=>{next[key][field]=Number(event.target.value);},false));}
    root.document.querySelectorAll('[data-growth-tab]').forEach(button=>{button.addEventListener('click',()=>setTab(button.dataset.growthTab));button.addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();const tabs=['talents','life','skills'],index=tabs.indexOf(tab);setTab(tabs[(index+(event.key==='ArrowLeft'?2:1))%3]);$('growth-tab-'+tab).focus();}});});
    $('managed-shop-weights').addEventListener('change',event=>{if(event.target.dataset.shopWeight)update(next=>{next.merchant.weights[event.target.dataset.shopWeight]=Number(event.target.value);});});$('managed-shop-weights').addEventListener('input',event=>{if(event.target.dataset.shopWeight)update(next=>{next.merchant.weights[event.target.dataset.shopWeight]=Number(event.target.value);},false);});
    $('managed-life-search').addEventListener('input',event=>{search=event.target.value;renderLife();});
    $('managed-life-nodes').addEventListener('click',event=>{const key=event.target.closest('[data-life-node]')?.dataset.lifeNode;if(key)priority(key);});
    $('managed-life-nodes').addEventListener('contextmenu',event=>{const key=event.target.closest('[data-life-node]')?.dataset.lifeNode;if(key){event.preventDefault();update(next=>{next.life_skills.priorities=lifeRemove(data.life_skills,next.life_skills.priorities,key);});}});
    $('managed-life-priorities').addEventListener('click',event=>{const remove=event.target.closest('[data-life-remove]'),up=event.target.closest('[data-life-up]');if(remove)priority(remove.dataset.lifeRemove);if(up)update(next=>{const list=next.life_skills.priorities,index=list.indexOf(up.dataset.lifeUp);if(index>0)[list[index-1],list[index]]=[list[index],list[index-1]];});});
    $('growth-life-clear').addEventListener('click',()=>update(next=>{next.life_skills.priorities=[];}));$('growth-life-zoom-in').addEventListener('click',()=>zoomLife(.1));$('growth-life-zoom-out').addEventListener('click',()=>zoomLife(-.1));$('growth-life-center').addEventListener('click',()=>centerLife(true));
    const viewport=$('managed-life-nodes');viewport.addEventListener('pointerdown',event=>{if(event.button!==0||event.target.closest('[data-life-node]'))return;hideTooltip();drag={x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop,id:event.pointerId};viewport.setPointerCapture(event.pointerId);viewport.classList.add('dragging');});viewport.addEventListener('pointermove',event=>{if(drag){viewport.scrollLeft=drag.left+drag.x-event.clientX;viewport.scrollTop=drag.top+drag.y-event.clientY;}});const release=()=>{drag=null;viewport.classList.remove('dragging');};viewport.addEventListener('pointerup',release);viewport.addEventListener('pointercancel',release);viewport.addEventListener('lostpointercapture',release);
    const talentEvent=(event,amount)=>{const button=event.target.closest('[data-talent-node]');if(!button||disabled||!selected)return;event.preventDefault();hideTooltip();const nodes=data.heroes[selected].talents,node=nodes.find(n=>n.id===button.dataset.talentNode),targets=settings.progression.talent_targets[selected]||{};
      if(amount>0&&total(targets)<node.point_requirement){$('growth-talent-feedback').textContent=`请先在前面的天赋投入至少 ${node.point_requirement} 点，再选择 ${node.name}。`;return;}
      $('growth-talent-feedback').textContent='';if(event.shiftKey&&amount>0)amount=node.max_points;
      const nextTargets=talentChange(nodes,targets,node.id,amount);
      if(amount>0&&(targets[node.id]||0)<node.max_points&&(nextTargets[node.id]||0)<=(targets[node.id]||0)){
        $('growth-talent-feedback').textContent=`战斗天赋最多 ${talentPointLimit} 点，剩余 ${Math.max(0,talentPointLimit-total(targets))} 点不足，请先减少其他预设。`;return;
      }
      update(next=>{next.progression.talent_targets[selected]=nextTargets;});};
    $('managed-talent-tree').addEventListener('click',event=>talentEvent(event,1));$('managed-talent-tree').addEventListener('contextmenu',event=>talentEvent(event,-1));$('managed-talent-tree').addEventListener('keydown',event=>{if(event.key==='-'||event.key==='Delete')talentEvent(event,-1);});$('managed-talent-clear').addEventListener('click',()=>{if(selected)update(next=>{next.progression.talent_targets[selected]={};$('growth-talent-feedback').textContent='';});});
    $('growth-skills-clear').addEventListener('click',()=>{if(selected)update(next=>{next.progression.skill_stages[selected]=[];});});
    $('managed-skill-stages').addEventListener('click',event=>{if(!selected||disabled)return;const slotButton=event.target.closest('[data-special-slot]');if(slotButton){specialSlot=Number(slotButton.dataset.specialSlot);renderSkills();return;}const button=event.target.closest('[data-skill-id]');if(!button)return;hideTooltip();const skill=data.heroes[selected].abilities.find(s=>s.id===button.dataset.skillId),stages=settings.progression.skill_stages[selected]||[],targets=skillTargets(stages),currentSlot=targets.indexOf(skill.id),slot=skill.slots.includes(0)?0:skill.slots.includes(1)?1:currentSlot>=2?currentSlot:specialSlot;update(next=>{next.progression.skill_stages[selected]=skillChoice(stages,slot,skill.id);});if(slot>=2&&currentSlot<0){specialSlot=slot===2?3:2;renderSkills();}});
    if(root.document.createElement){tooltip=root.document.createElement('div');tooltip.className='growth-tooltip';tooltip.setAttribute('role','tooltip');tooltip.hidden=true;root.document.body.appendChild(tooltip);$('managed-growth').addEventListener('pointerover',event=>{const button=event.target.closest('[data-growth-tooltip]');if(button)showTooltip(button,event);});$('managed-growth').addEventListener('pointerout',event=>{if(event.target.closest('[data-growth-tooltip]'))hideTooltip();});$('managed-growth').addEventListener('focusin',event=>{const button=event.target.closest('[data-growth-tooltip]');if(button)showTooltip(button);});$('managed-growth').addEventListener('focusout',hideTooltip);root.addEventListener('scroll',hideTooltip,true);root.addEventListener('blur',hideTooltip);}
  }
  // A hidden or collapsed life tree has no viewport size. Center it only after
  // reveal, and retain the user's pan position after the first centering.
  const reveal=()=>{hideTooltip();if(tab==='life')centerLife();};
  root.ManagedGrowthUI={init,render,read,reveal,helpers};
})(typeof window==='undefined'?globalThis:window);
