"""Verified carriage collection with atomic slot writes and native scene cleanup."""
from __future__ import annotations
import json
import hashlib
import struct
import time
from .native_memory import MemoryReadError
from .container_transfer import FrozenSlotTransaction, NT, slot_fields, GC_MODE_RVA, GC_BITMAP_RVA, GAME_MANAGER_RVA, ensure_items_writable
from .paths import RESOURCE_ROOT
from .activity_journal import operation_id
from .game_compatibility import native_rva, assert_current

class FrozenCarriageTransaction(FrozenSlotTransaction):
    def collect(self,drop,drop_header,target,target_header,guards):
        count=max(1,struct.unpack_from('<i',drop_header,168)[0])
        return self.collect_many(drop,drop_header,[(target,target_header,count)],guards)

    def collect_many(self,drop,drop_header,targets,guards):
        p=self.reader.memory
        if struct.unpack_from('<Q',drop_header)[0]!=self.reader.classes['LootDrop']:
            raise MemoryReadError('掉落类型未确认')
        item=struct.unpack_from('<Q',drop_header,120)[0]
        uid=struct.unpack_from('<Q',drop_header,136)[0]
        if not item or not struct.unpack_from('<Q',drop_header,16)[0]:
            raise MemoryReadError('掉落实体和物品编号未确认')
        item_type=p.i32(item+32)
        if item_type==1:
            raise MemoryReadError('虚拟材料需要单独处理，当前验证不收取')
        count=max(1,struct.unpack_from('<i',drop_header,168)[0])
        if not 1 <= count <= 2147483647: raise MemoryReadError('物品数量无效')
        if drop_header[185]!=0:
            raise MemoryReadError('物品已经收取或收取状态无效')
        if not targets or len({a for a,_,_ in targets})!=len(targets) or sum(n for _,_,n in targets)!=count:
            raise MemoryReadError('收取数量与目标槽位不一致')
        if len(targets)>1 and (item_type!=6 or self.reader.string(uid)):
            raise MemoryReadError('仅无唯一编号的宝石可拆分入背包')
        for target,target_header,amount in targets:
            if (len(target_header)!=48 or p.u64(target)!=self.reader.classes['InventorySlot'] or amount<=0
                    or any(any(v) for v in slot_fields(target_header).values())):
                raise MemoryReadError('目标不是完整空位')
        if NT.NtSuspendProcess(self.handle)<0: raise MemoryReadError('无法短暂冻结游戏数据更新')
        journal=[]
        started=time.perf_counter()
        try:
            if p.read(drop,len(drop_header))!=drop_header or any(p.read(a,48)!=h for a,h,_ in targets):
                raise MemoryReadError('准备时物品或槽位发生变化，未收取')
            if any(p.read(a,len(b))!=b for a,b in guards):
                raise MemoryReadError('容器或唯一编号发生变化，未收取')
            if time.perf_counter()-started>1: raise MemoryReadError('核验耗时异常，未收取')
            base=self.reader.module['base']
            if p.i32(base+native_rva(self.reader,'gc_mode_rva',GC_MODE_RVA)) not in (0,1): raise MemoryReadError('GC 屏障状态异常')
            bitmap={}
            for field in (a+off for a,_,_ in targets for off in (16,32)):
                page=(field>>12)&0x1fffff
                word=base+native_rva(self.reader,'gc_bitmap_rva',GC_BITMAP_RVA)+(page>>6)*8
                bitmap[word]=bitmap.get(word,p.u64(word))|(1<<(page&63))
            save=self.reader.singleton('SaveSystem')
            ensure_items_writable(self.reader,save)
            if p.read(save+92,1) not in (b'\0',b'\1'): raise MemoryReadError('保存状态异常')
            plan=[(a,struct.pack('<Q',bits)) for a,bits in bitmap.items()]
            for target,_,amount in targets:
                plan.extend([(target+16,struct.pack('<Q',item)),(target+24,struct.pack('<i',amount)),
                      (target+32,struct.pack('<Q',uid)),(target+40,b'\0')])
            plan.extend([(save+92,b'\1'),(drop+185,b'\1'),(drop+160,struct.pack('<f',0.00001))])
            for address,data in plan:
                original=p.read(address,len(data))
                if original==data: continue
                journal.append((address,original))
                self.write(address,data)
                if p.read(address,len(data))!=data: raise MemoryReadError('字段回读不一致')
            return {'duration_ms':round((time.perf_counter()-started)*1000,3),
                    'writes':len(journal),'save_requested':True,'cleanup':'game_expiry_sweep'}
        except Exception as exc:
            failed=[]
            for address,original in reversed(journal):
                try:
                    self.write(address,original)
                    if p.read(address,len(original))!=original: raise MemoryReadError('回滚回读失败')
                except Exception: failed.append(address)
            if failed: raise MemoryReadError('收取失败且部分字段未能恢复，请立即检查游戏') from exc
            raise
        finally:
            if NT.NtResumeProcess(self.handle)<0 and NT.NtResumeProcess(self.handle)<0:
                raise MemoryReadError('恢复游戏线程失败')


class CarriageUnavailable(MemoryReadError):
    """A normal scene/capacity condition; retry on the next cycle."""


def read_carriage(reader):
    assert_current(reader)
    p=reader.memory
    profiles=getattr(reader,'_carriage_profiles',None)
    if profiles is None:
        profiles={t['name']:t for t in json.loads((RESOURCE_ROOT/'data/carriage-types.json').read_text(encoding='utf-8'))}
    guards=[]
    def read(address,size):
        value=p.read(address,size);guards.append((address,value));return value
    def ptr(address): return struct.unpack('<Q',read(address,8))[0]
    def verify(obj,name):
        if not obj: raise CarriageUnavailable('当前场景没有马车，等待进入战斗场景。')
        klass=ptr(obj)
        actual=reader.fields(klass)
        expected=[{'name':f['name'],'offset':f['rawRegistrationOffset'],'token':int(f['token'],16)} for f in profiles[name]['fields']]
        if reader.class_name(klass)!=name or actual!=expected: raise MemoryReadError('马车对象字段与已验证版本不一致')
        if name=='LootDrop': reader.classes[name]=klass
        return obj
    klass=ptr(reader.module['base']+native_rva(reader,'game_manager_rva',GAME_MANAGER_RVA))
    manager=verify(ptr(ptr(klass+184)),'GameManager')
    carriage=verify(ptr(manager+88),'HorseCarriage')
    def objects(root,element):
        if not root or reader.class_name(p.u64(root))!='List`1': raise MemoryReadError('马车列表类型未确认')
        fields={f['name']:f['offset'] for f in reader.fields(p.u64(root))}
        if any(fields.get(k)!=v for k,v in {'_items':16,'_size':24,'_version':28}.items()): raise MemoryReadError('列表字段未确认')
        array,count,version=struct.unpack('<Qii',read(root+16,16))
        records,block=reader.array(array,element,limit=8192,stride=8)
        if not 0<=count<=len(records): raise MemoryReadError('马车列表数量异常')
        guards.append((array+32,block))
        return [struct.unpack('<Q',v)[0] for v in records[:count]]
    drops=objects(ptr(carriage+152),'LootDrop')
    world=objects(ptr(manager+216),'LootDrop')
    rows=[]
    for index,address in enumerate(drops):
        if not address: continue
        verify(address,'LootDrop')
        header=read(address,216)
        if not struct.unpack_from('<Q',header,16)[0] or header[185]: continue
        item=struct.unpack_from('<Q',header,120)[0]
        if not item: continue
        row=reader.base_item(item)
        uid=reader.string(struct.unpack_from('<Q',header,136)[0])
        count=max(1,struct.unpack_from('<i',header,164 if row['item_type']==1 else 168)[0])
        token=hashlib.sha256(f'{reader.session_id}:{address}:{item}:{uid}:{count}'.encode()).hexdigest()
        observation_id=hashlib.sha256(f'{reader.session_id}:{address}:{item}:{uid}'.encode()).hexdigest()
        row={**row,'item_uid':uid,'count':count,'selection_id':token,'observation_id':observation_id,'slot_index':index,
             'collectable':row['item_type']!=1 and world.count(address)==1,
             '_address':address,'_header':header}
        rows.append(row)
    if any(p.read(a,len(b))!=b for a,b in guards): raise CarriageUnavailable('马车正在收集物品，等待下一次读取。')
    return {'available':True,'count':len(rows),'items':rows,'guards':guards,'world':world,'manager':manager,'carriage':carriage}


def public_carriage(reader):
    try:
        value=read_carriage(reader)
        return {'available':True,'count':value['count'],'items':[{k:v for k,v in r.items() if not k.startswith('_')} for r in value['items']]}
    except CarriageUnavailable as exc:
        return {'available':False,'complete':'正在' not in str(exc),'count':0,'items':[],'reason':str(exc)}


def collect_item(reader,selection_id,validate=lambda:True,on_commit=None):
    snapshot=reader.snapshot()
    if not snapshot['complete']: raise CarriageUnavailable('物品正在变化，等待下次读取。')
    current=read_carriage(reader)
    row=next((r for r in current['items'] if r['selection_id']==selection_id),None)
    if not row or not row['collectable']: raise CarriageUnavailable('这件马车物品已经变化，等待重新读取。')
    uid=row['item_uid']
    if uid and any(r.get('item_uid')==uid for c in snapshot['containers'].values() for r in c['slots']):
        raise MemoryReadError('马车物品编号已存在于背包或仓库，已停止自动收取')
    p=reader.memory
    root=reader.singleton('Inventory');array=p.u64(root+56)
    records,block=reader.array(array,'InventorySlot',limit=2048,stride=8)
    guards=current['guards']+reader.snapshot_stamps+[(root+56,struct.pack('<Q',array)),(array+32,block)]
    maximum=row['max_stack'] if row.get('item_class')=='GemData' and not uid else row['count']
    if not 1<=maximum<=2147483647: raise MemoryReadError('宝石堆叠上限未确认')
    remaining=row['count'];targets=[]
    for index,record in enumerate(records):
        slot=struct.unpack('<Q',record)[0]
        if not slot or p.u64(slot)!=reader.classes['InventorySlot']: continue
        head=p.read(slot,48)
        if not any(any(v) for v in slot_fields(head).values()):
            amount=min(maximum,remaining);targets.append((index,slot,head,amount));remaining-=amount
            if not remaining: break
    if remaining: raise CarriageUnavailable('背包空格不足以收取整组物品，马车物品保留，等待腾出空间。')
    if not validate(): raise CarriageUnavailable('自动收取已暂停。')
    tx=FrozenCarriageTransaction(reader)
    try: result=tx.collect_many(row['_address'],row['_header'],[(a,h,n) for _,a,h,n in targets],guards)
    finally: tx.close()
    movements=[];remaining=row['count']
    for index,_,_,amount in targets:
        movements.append({'name':row['internal_name'],'identity':uid or '','observation_id':row.get('observation_id',''),
            'count':amount,'merged':False,
            'source':{'container':'carriage','slot_index':row.get('slot_index'),'count_before':remaining,'count_after':remaining-amount},
            'destination':{'container':'inventory','slot_index':index,'count_before':0,'count_after':amount}})
        remaining-=amount
    result={**result,'operation_id':operation_id(),'movements':movements,'moved':1,
            'name':row['internal_name'],'slot_index':targets[0][0],'new_slots':len(targets)}
    # Persist confirmed changes before later scene cleanup/readback can fail.
    if on_commit: on_commit(result)
    # Collected flag prevents a second pickup while Unity's expiry sweep runs.
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        try:
            if not p.u64(row['_address']+16): break
        except MemoryReadError: break
        time.sleep(.05)
    fresh=reader.snapshot()
    for index,_,_,amount in targets:
        found=next((r for r in fresh['containers']['inventory']['slots'] if r['slot_index']==index),None)
        if not found or found.get('internal_name')!=row['internal_name'] or found['count']!=amount or found.get('item_uid')!=uid:
            raise MemoryReadError('马车收取后物品回读不一致，已停止自动整理')
    return result
