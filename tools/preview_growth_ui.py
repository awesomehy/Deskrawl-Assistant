"""Offline growth UI fixture using the production HTTP/CSP handler.

No game client or AssistantService is created. All interactive requests below
are handled in browser-local fixture data and never reach a game process.
"""
import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deskrawl_assistant.web_server import make_handler, WEB

FIXTURE_JS = """'use strict';
window.addEventListener('DOMContentLoaded',async()=>{
 const catalog=await fetch('/fixture.json').then(r=>r.json());
 document.querySelectorAll('main > section').forEach(n=>n.hidden=n.id!=='managed-page');
 const settings=ManagedModeUI.helpers.normalizeSettings();
 const packet={settings,phase:'idle',connected:true,warehouse_ready:true,can_start:true,
  current_difficulty:'Normal',difficulties:['Normal','Nightmare','Inferno'].map(id=>({id,unlocked:true})),
  automatic_start:{dungeon_id:'b',difficulty:'Inferno'},
  maps_by_difficulty:Object.fromEntries(['Normal','Nightmare','Inferno'].map(tier=>[tier,['a','b','c'].map((id,i)=>({id,name:['古林边境','阴影废墟','腐蚀洞穴'][i],unlocked:i<2,automation_supported:i<2}))])),
  capabilities:Object.fromEntries(['transfer','lock','equip','travel','difficulty','sell','salvage','chests','ground_pickup','merchant','talents','life_skills','skills'].map(k=>[k,{available:true,test_enabled:true,native_verified:false}])),
  warehouse_pages:[1,2,3,4,5].map(index=>({index,capacity:50,free:50})),
  progression_catalog:catalog,progression_state:{available:true,class_mask:4,level:70,gold:100000,
   soul_shards:{current:1800,maximum:3000},talents:{ranks:{},unspent:10},life_skills:{ranks:{}},
   merchant:{unlock_known:true,unlocked:true,unlock_requirements:catalog.shop_unlock,stock:catalog.shop.map(row=>({...row,price:row.base_price*7}))},
   skills:{equipped:[null,null,null,null],available:catalog.heroes['4'].abilities.map(s=>s.id)}}};
 ManagedModeUI.init({catalog:{equipment:[],equipped_slot_options:[]},onDirty:()=>{},
  toast:()=>{},confirmAction:async()=>true,api:async(route,payload)=>{
   if(route==='managed/settings')packet.settings=payload.settings;
   if(route==='managed/run')packet.running=payload.enabled;
   return packet;}});
 ManagedModeUI.applyPacket(packet);ManagedModeUI.setConnection(true);
 const controls=document.createElement('nav');controls.id='fixture-controls';
 controls.innerHTML='<button type="button" data-fixture-class="4">模拟猎人连接</button><button type="button" data-fixture-class="8">模拟武僧连接</button><button type="button" data-fixture-disconnect>模拟断开</button><button type="button" data-fixture-pending>模拟读取职业</button><button type="button" data-fixture-merchant-locked>模拟商人未解锁</button><button type="button" data-fixture-prices>模拟价格更新</button>';
 controls.style.position='fixed';controls.style.right='12px';controls.style.top='12px';controls.style.zIndex='200';
 controls.style.background='#14191f';controls.style.padding='8px';controls.style.border='1px solid #5c5340';document.body.appendChild(controls);
 controls.addEventListener('click',event=>{
  if(event.target.closest('[data-fixture-disconnect]')){packet.connected=false;ManagedModeUI.setConnection(false);return;}
  if(event.target.closest('[data-fixture-merchant-locked]'))packet.progression_state.merchant.unlocked=false;
  else if(event.target.closest('[data-fixture-prices]')){
   const reference=!packet.progression_state.merchant.preview?.[0]?.price_reference;
   packet.progression_state.merchant.preview=catalog.shop.map(row=>({...row,price:row.base_price*7+(reference?0:1),price_reference:reference}));
  }
  else if(event.target.closest('[data-fixture-pending]')){packet.connected=true;packet.progression_state={available:false,reason:'正在读取职业'};}
  else if(event.target.dataset.fixtureClass){
   const mask=Number(event.target.dataset.fixtureClass);packet.connected=true;
   packet.progression_state={available:true,class_mask:mask,level:70,gold:100000,
    soul_shards:{current:1800,maximum:3000},talents:{ranks:{},unspent:10},life_skills:{ranks:{}},
    merchant:{unlock_known:true,unlocked:true,unlock_requirements:catalog.shop_unlock,stock:catalog.shop.map(row=>({...row,price:row.base_price*7}))},
    skills:{equipped:[null,null,null,null],available:catalog.heroes[String(mask)].abilities.map(s=>s.id)}};
  }else return;
  ManagedModeUI.applyPacket(packet);ManagedModeUI.setConnection(true);
 });
 const menu=document.querySelector('#managed-shop-weights');let rebuilds=0;menu.dataset.fixtureRebuilds='0';
 new MutationObserver(records=>{rebuilds+=records.filter(row=>row.target===menu&&row.type==='childList').length;menu.dataset.fixtureRebuilds=String(rebuilds);}).observe(menu,{childList:true});
 document.querySelector('#footer-state').textContent='离线模拟 · 使用正式窗口的加载策略 · 未连接游戏';
});
"""


class Handler(make_handler(SimpleNamespace(), 'offline-fixture')):
    def do_GET(self):
        if not self.valid_host():return self.reply({'error':'仅限本机访问。'},403)
        if self.path=='/':
            html=(WEB/'index.html').read_text(encoding='utf-8')
            html=re.sub(r'<script src="/(?!automation-tools.js|managed-growth.js|auction-ui.js|managed-mode.js)[^"]+" defer></script>','',html)
            html=html.replace('__APP_VERSION__','1.2.6 · 离线布局检查')
            return self.reply(html.replace('</body>','<script src="/fixture.js" defer></script></body>'),content_type='text/html; charset=utf-8')
        if self.path=='/fixture.js':return self.reply(FIXTURE_JS,content_type='text/javascript; charset=utf-8')
        if self.path=='/fixture.json':
            return self.reply(json.loads((ROOT/'data/progression-catalog.json').read_text(encoding='utf-8')))
        if self.path.startswith('/api/'):
            return self.reply({'error':'离线页面没有游戏操作接口。'},404)
        return super().do_GET()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18926)
    args=parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
