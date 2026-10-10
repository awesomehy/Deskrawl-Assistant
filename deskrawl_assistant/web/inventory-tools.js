(function(root){
  'use strict';
  function position(a,b){return (a.container==='inventory'?0:1)-(b.container==='inventory'?0:1)||a.slot_index-b.slot_index||String(a.selection_id).localeCompare(String(b.selection_id));}
  function select(items,filters={},currentClass=0){
    const f=filters, query=String(f.search||'').trim().toLowerCase(), classMask=f.class==='current'?currentClass:Number(f.class||0);
    const view=items.filter(i=>{
      if(f.container&&i.container!==f.container||f.kind&&i.kind!==f.kind)return false;
      if(Array.isArray(f.containers)&&!f.containers.includes(i.container))return false;
      if(Array.isArray(f.locks)&&(!i.is_equipment||!f.locks.some(v=>v==='locked'?i.locked===true:i.locked===false)))return false;
      if(Array.isArray(f.rules)&&(!i.is_equipment||!f.rules.some(v=>v==='matches'?i.matches:v==='perfect'?i.primary_perfect:!i.matches&&!i.review)))return false;
      if(query&&![i.name,i.en,i.internal_name].some(v=>String(v||'').toLowerCase().includes(query)))return false;
      if(f.lock&&(!i.is_equipment||(f.lock==='locked'?i.locked!==true:f.lock==='unlocked'?i.locked!==false:f.lock==='matches'?!i.matches:f.lock==='perfect'?!i.primary_perfect:f.lock==='review'?!i.review:i.matches||i.review)))return false;
      const equipmentFilter=f.class||f.slot||f.rarity||f.ancient||f.mist||f.divine||f.sockets||f.upgrade||f.levelMin||f.levelMax||f.power;
      if(equipmentFilter&&!i.is_equipment)return false;
      if(f.class&&(!classMask||!(i.class_mask&classMask)))return false;
      if(f.slot&&i.slot!==f.slot||f.rarity&&i.rarity!==Number(f.rarity))return false;
      if(f.ancient&&i.is_ancient!==true||f.mist&&i.is_black_mist!==true||f.divine&&i.is_divine!==true)return false;
      if(f.sockets&&(i.sockets==null||(f.sockets==='none'?i.sockets!==0:i.sockets<Number(f.sockets))))return false;
      if(f.upgrade&&(i.upgrade==null||i.upgrade<Number(f.upgrade)))return false;
      if(f.levelMin&&(i.level==null||i.level<Number(f.levelMin))||f.levelMax&&(i.level==null||i.level>Number(f.levelMax)))return false;
      if(f.power){const p=i.power;if(f.power==='unavailable'){if(p?.available)return false;}else if(!p?.available||(f.power==='up'?p.delta<=0:f.power==='down'?p.delta>=0:p.delta!==0))return false;}
      return true;
    });
    const sort=f.sort||'position';
    const getters={position:i=>i.slot_index,level:i=>i.level,upgrade:i=>i.is_equipment?i.upgrade:null,sockets:i=>i.is_equipment?i.sockets:null,count:i=>i.count,
      power:i=>i.power?.available?i.power.delta:null,percent:i=>i.power?.available?i.power.percent:null};
    view.sort((a,b)=>{
      if(sort==='position')return position(a,b);
      if(sort==='name')return a.name.localeCompare(b.name,'zh-CN')||position(a,b);
      const [key,direction]=sort.split('-'),get=getters[key];
      if(!get)return position(a,b);
      const x=get(a),y=get(b),valid=v=>typeof v==='number'&&Number.isFinite(v);
      if(!valid(x)||!valid(y))return Number(valid(y))-Number(valid(x))||position(a,b);
      return (direction==='asc'?x-y:y-x)||position(a,b);
    });
    return view;
  }
  const api={select};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.InventoryTools=api;
})(typeof globalThis==='object'?globalThis:this);
