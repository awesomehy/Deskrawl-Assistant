"""Collect one verified golden chest or equipment item without code injection.

Experimental feasibility probe, not part of the released application. Copies
the existing item's references to an empty slot, then asks the game's existing
expiry sweep to destroy the old scene object. It never opens or sells the chest.
"""
from __future__ import annotations
import ctypes as C
from datetime import datetime,timezone
import json
from pathlib import Path
import struct
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deskrawl_assistant.native_memory import MemoryReadError,game_pids
from probe_transfer_state import TransferReader
from verify_container_transfer import FrozenSlotTransaction,NT,slot_fields


class FrozenCarriageTransaction(FrozenSlotTransaction):
    def collect(self,drop,drop_header,target,target_header,guards):
        p=self.reader.memory
        if struct.unpack_from('<Q',drop_header)[0]!=self.reader.classes['LootDrop']:
            raise MemoryReadError('掉落类型未确认')
        item=struct.unpack_from('<Q',drop_header,120)[0]
        uid=struct.unpack_from('<Q',drop_header,136)[0]
        if not item or not uid or not struct.unpack_from('<Q',drop_header,16)[0]:
            raise MemoryReadError('掉落实体和物品编号未确认')
        if drop_header[185] or struct.unpack_from('<i',drop_header,168)[0]!=1:
            raise MemoryReadError('只验证单件、尚未收取的物品')
        if p.u64(target)!=self.reader.classes['InventorySlot'] or any(any(v) for v in slot_fields(target_header).values()):
            raise MemoryReadError('目标不是完整空位')
        if NT.NtSuspendProcess(self.handle)<0: raise MemoryReadError('无法短暂冻结游戏数据更新')
        journal=[]
        started=time.perf_counter()
        try:
            if p.read(drop,len(drop_header))!=drop_header or p.read(target,48)!=target_header:
                raise MemoryReadError('准备时物品或槽位发生变化，未收取')
            if any(p.read(a,len(b))!=b for a,b in guards):
                raise MemoryReadError('容器或唯一编号发生变化，未收取')
            base=self.reader.module['base']
            if p.i32(base+0x3bae7b0) not in (0,1): raise MemoryReadError('GC 屏障状态异常')
            bitmap={}
            for field in (target+16,target+32):
                page=(field>>12)&0x1fffff
                word=base+0x3c08200+(page>>6)*8
                bitmap[word]=bitmap.get(word,p.u64(word))|(1<<(page&63))
            save=self.reader.singleton('SaveSystem')
            if p.read(save+92,1) not in (b'\0',b'\1'): raise MemoryReadError('保存状态异常')
            plan=[(target+16,struct.pack('<Q',item)),(target+24,struct.pack('<i',1)),
                  (target+32,struct.pack('<Q',uid)),(target+40,b'\0')]
            plan.extend((a,struct.pack('<Q',bits)) for a,bits in bitmap.items())
            plan.extend([(save+92,b'\1'),(drop+185,b'\1'),(drop+160,struct.pack('<f',0.00001))])
            for address,data in plan:
                original=p.read(address,len(data))
                if original==data: continue
                journal.append((address,original))
                self.write(address,data)
                if p.read(address,len(data))!=data: raise MemoryReadError('字段回读不一致')
            return {'duration_ms':round((time.perf_counter()-started)*1000,3),
                    'writes':len(journal),'save_requested':True,'cleanup':'game_expiry_sweep'}
        except Exception:
            for address,original in reversed(journal): self.write(address,original)
            raise
        finally:
            if NT.NtResumeProcess(self.handle)<0: raise MemoryReadError('恢复游戏线程失败')


def main():
    destination=sys.argv[1] if len(sys.argv)>1 else 'inventory'
    equipment_test='--equipment' in sys.argv[2:]
    if destination not in {'inventory','storage'}: raise ValueError('invalid destination')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    state=json.load(opener.open('http://127.0.0.1:18741/api/state',timeout=5))
    if state['monitoring'] or state['busy']: raise RuntimeError('助手正在操作装备，暂不验证')
    reader=TransferReader(game_pids()[0])
    writer=None
    report={'started_at_utc':datetime.now(timezone.utc).isoformat(),'pid':reader.pid,'destination':destination}
    file=ROOT/f"data/runtime/carriage-{'equipment-' if equipment_test else ''}{destination}-verification.json"
    try:
        p=reader.memory
        before=reader.transfer_state()
        if not before['snapshot']['complete']: raise MemoryReadError('容器快照不完整')
        if equipment_test:
            candidates=[d for d in before['drops'] if d.get('is_equipment') and d.get('instance_id') and not d.get('is_black_mist') and not d.get('locked')]
            chosen=min(candidates,key=lambda d:(d['rarity'],d['item_level']))
        else:
            chosen=next(d for d in before['drops'] if d.get('internal_name')=='TreasureChest2' and d.get('instance_id'))
        drop=chosen['drop_address']
        drop_header=p.read(drop,216)
        uid=reader.string(p.u64(drop+136))
        assert uid==chosen['uid_136']
        registry,_=reader.registry()
        generated=registry[uid]
        original_item=reader.generated(generated)
        if any(row.get('item_uid')==uid for c in before['snapshot']['containers'].values() for row in c['slots']):
            raise MemoryReadError('这个物品已在容器中，未收取')
        root=before['containers'][destination]['root']
        array=before['containers'][destination]['array']
        candidates=before['containers'][destination]['empty_indices']
        if destination=='storage': candidates=[n for n in candidates if n<50]
        index=candidates[0]
        target=p.u64(array+32+index*8)
        target_header=p.read(target,48)
        manager=before['game_manager']
        objects,world_stamp=reader.list_objects(p.u64(manager+216),'LootDrop')
        if objects.count(drop)!=1: raise MemoryReadError('掉落实体在场景列表中不唯一')
        carriage_stamp=before['carriage_list']
        guards=[(root+(56 if destination=='inventory' else 48),struct.pack('<Q',array)),
                (array+32+index*8,struct.pack('<Q',target)),(generated,p.read(generated,128)),
                (carriage_stamp['list']+16,bytes.fromhex(carriage_stamp['header'])),
                (world_stamp['list']+16,bytes.fromhex(world_stamp['header']))]
        report.update({'item_uid':uid,'instance_id':original_item['instance_id'],'name':chosen['internal_name'],
                       'equipment_test':equipment_test,
                       'target_slot':index,'active_map':reader.asset_name(p.u64(manager+112)),
                       'before_carriage_count':carriage_stamp['count'],'before':before})
        file.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        writer=FrozenCarriageTransaction(reader)
        report['transaction']=writer.collect(drop,drop_header,target,target_header,guards)
        # Only the game destroys its own Unity object, which invokes OnDestroy
        # and then carriage Update removes the destroyed object reference.
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            try:
                current_carriage,_=reader.list_objects(p.u64(before['carriage']+152),'LootDrop')
                current_world,_=reader.list_objects(p.u64(manager+216),'LootDrop')
                if drop not in current_carriage and drop not in current_world:
                    break
            except MemoryReadError: pass
            time.sleep(.05)
        else: raise MemoryReadError('原掉落实体尚未由游戏完整清理，验证未通过')
        after=reader.transfer_state()
        if not after['snapshot']['complete']: raise MemoryReadError('收取后快照不完整')
        found=[row for c in after['snapshot']['containers'].values() for row in c['slots'] if row.get('item_uid')==uid]
        assert len(found)==1 and found[0]['container']==destination and found[0]['slot_index']==index
        assert found[0]['count']==1 and found[0]['internal_name']==chosen['internal_name']
        fresh_registry,_=reader.registry()
        assert fresh_registry[uid]==generated
        assert reader.generated(generated)==original_item
        assert all(d.get('uid_136')!=uid for d in after['drops'])
        report.update({'success':True,'original_scene_drop_cleaned':True,'unique_item_verified':True,
                       'after_carriage_count':after['carriage_list']['count'],'game_running':p.is_running(),'after':after})
    finally:
        file.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if writer: writer.close()
        reader.close()
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','after'}},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
