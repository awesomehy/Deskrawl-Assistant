'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function page(){
  const ids=new Map(['steam-profile','steam-profile-name','steam-profile-detail','steam-profile-avatar','steam-profile-fallback'].map(id=>[id,{
    textContent:'',title:'',dataset:{},hidden:id==='steam-profile-avatar',events:{},src:'',
    addEventListener(name,handler){this.events[name]=handler;},removeAttribute(name){this[name]='';}
  }]));
  let now=0;
  const context=vm.createContext({document:{getElementById:id=>ids.get(id)},window:{},Date:{now:()=>now}});
  const source=fs.readFileSync(path.join(__dirname,'../deskrawl_assistant/web/steam-profile.js'),'utf8');
  vm.runInContext(source,context);
  return {ids,render:packet=>context.window.SteamClientUI.render(packet),advance:ms=>{now+=ms;}};
}
const url='/api/steam-client/avatar?v='+'a'.repeat(64);

test('all local Steam states explain presence without confusing market login',()=>{
  const p=page();
  for(const [status,name,detail] of [
    ['loading','正在识别 Steam…','正在读取昵称和头像…'],
    ['online','Steam 已登录','Steam · 已登录'],
    ['logged_out','未登录 Steam','请在 Steam 客户端登录'],
    ['not_running','Steam 未运行','启动后自动识别'],
    ['profile_unavailable','Steam 已登录','账号信息暂不可用'],
    ['unknown','Steam 状态未确认','稍后自动重试']
  ]){
    p.render({status});
    assert.equal(p.ids.get('steam-profile-name').textContent,name);
    assert.equal(p.ids.get('steam-profile-detail').textContent,detail);
    assert.match(p.ids.get('steam-profile').title,/与 Steam 市场登录状态独立/);
  }
  p.render();assert.equal(p.ids.get('steam-profile').dataset.status,'unknown');
});

test('nickname stays complete and safe in text and hover title, including long names',()=>{
  const p=page(),nickname='<img src=x onerror=evil()>超长昵称'.repeat(12)+'😀';
  p.render({status:'online',nickname});
  assert.equal(p.ids.get('steam-profile-name').textContent,nickname);
  assert.equal(p.ids.get('steam-profile-name').title,nickname);
  assert.ok(p.ids.get('steam-profile').title.includes(nickname));
  const css=fs.readFileSync(path.join(__dirname,'../deskrawl_assistant/web/steam-profile.css'),'utf8');
  assert.match(css,/\.steam-profile-name\{[^}]*overflow:hidden;[^}]*text-overflow:ellipsis;[^}]*white-space:nowrap/);
});

test('avatar failure keeps nickname, retries later, and never reloads on ordinary polling',()=>{
  const p=page(),image=p.ids.get('steam-profile-avatar'),fallback=p.ids.get('steam-profile-fallback');
  p.render({status:'online',nickname:'名字',avatar_url:url});
  image.events.load();assert.equal(image.hidden,false);assert.equal(fallback.hidden,true);
  p.render({status:'online',nickname:'名字',avatar_url:url});assert.equal(image.hidden,false);
  image.events.error();assert.equal(image.hidden,true);assert.equal(fallback.hidden,false);
  assert.equal(p.ids.get('steam-profile-name').textContent,'名字');
  p.advance(29000);p.render({status:'online',nickname:'名字',avatar_url:url});assert.equal(image.hidden,true);
  p.advance(1000);p.render({status:'online',nickname:'名字',avatar_url:url});image.events.load();assert.equal(image.hidden,false);
});

test('signout clears avatar and stale nickname, invalid URLs never load',()=>{
  const p=page(),image=p.ids.get('steam-profile-avatar');
  p.render({status:'online',nickname:'上个账号',avatar_url:url});image.events.load();
  p.render({status:'logged_out',nickname:'stale',avatar_url:url});
  assert.equal(image.src,'');assert.equal(image.hidden,true);
  assert.equal(p.ids.get('steam-profile-name').textContent,'未登录 Steam');
  for(const value of ['https://example.com/avatar','file:///private.png','/api/steam-client/avatar?v=../../file']){
    p.render({status:'online',nickname:'name',avatar_url:value});assert.equal(image.src,'');
  }
});
