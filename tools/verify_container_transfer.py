"""Controlled live feasibility test: move one equipment item and move it back.

Not imported by the released assistant. Requires the verified build and an
empty slot in storage page 1. No code injection or game method invocation.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import struct
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deskrawl_assistant.native_memory import K, MemoryReadError, game_pids
from probe_transfer_state import TransferReader

NT=C.WinDLL('ntdll')
for name in ('NtSuspendProcess','NtResumeProcess'):
    fn=getattr(NT,name)
    fn.argtypes=(W.HANDLE,)
    fn.restype=C.c_long
K.WriteProcessMemory.argtypes=(W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t))
K.WriteProcessMemory.restype=W.BOOL


def slot_fields(header):
    return {16:header[16:24],24:header[24:28],32:header[32:40],40:header[40:41]}


class FrozenSlotTransaction:
    """Commit both slots while game threads cannot mutate them; undo on error."""
    def __init__(self,reader):
        self.reader=reader
        self.handle=K.OpenProcess(0x800|0x20|0x8,False,reader.pid)
        if not self.handle: raise MemoryReadError('无法打开搬运验证所需权限')

    def write(self,address,data):
        value=C.create_string_buffer(data)
        written=C.c_size_t()
        if not K.WriteProcessMemory(self.handle,address,value,len(data),C.byref(written)) or written.value!=len(data):
            raise MemoryReadError('搬运数据写入未完整确认')

    def move(self,source,target,source_header,target_header,guards):
        p=self.reader.memory
        if p.u64(source)!=self.reader.classes['InventorySlot'] or p.u64(target)!=self.reader.classes['InventorySlot']:
            raise MemoryReadError('槽位类型未确认')
        if not struct.unpack_from('<Q',source_header,16)[0] or struct.unpack_from('<i',source_header,24)[0]!=1:
            raise MemoryReadError('仅验证单件装备')
        if any(any(value) for value in slot_fields(target_header).values()):
            raise MemoryReadError('目标槽位不是完整空位')
        started=time.monotonic()
        if NT.NtSuspendProcess(self.handle)<0: raise MemoryReadError('无法短暂冻结游戏数据更新，未搬运')
        journal=[]
        try:
            if p.read(source,48)!=source_header or p.read(target,48)!=target_header:
                raise MemoryReadError('槽位在准备期间发生变化，未搬运')
            if any(p.read(address,len(expected))!=expected for address,expected in guards):
                raise MemoryReadError('对象或装备身份发生变化，未搬运')
            if time.monotonic()-started>1: raise MemoryReadError('验证耗时异常，未搬运')
            plan=[]
            # Mirror the four fields exchanged by Storage.fnw, retaining
            # every slot object, array pointer, and generated item object.
            for address,new_header in ((target,source_header),(source,target_header)):
                plan.extend((address+offset,data) for offset,data in slot_fields(new_header).items())
            # The verified game's reference writes dirty this exact GC page
            # bitmap. Mark the same cards while all game threads are paused.
            base=self.reader.module['base']
            if p.i32(base+0x3bae7b0) not in (0,1):
                raise MemoryReadError('GC 写屏障状态未确认，未搬运')
            bitmap={}
            for address in (source+16,source+32,target+16,target+32):
                page=(address>>12)&0x1fffff
                word=base+0x3c08200+(page>>6)*8
                bitmap[word]=bitmap.get(word,p.u64(word))|(1<<(page&63))
            plan.extend((address,struct.pack('<Q',bits)) for address,bits in bitmap.items())
            save=self.reader.singleton('SaveSystem')
            if p.read(save+92,1) not in (b'\0',b'\1'): raise MemoryReadError('保存状态异常，未搬运')
            plan.append((save+92,b'\1'))
            for address,data in plan:
                original=p.read(address,len(data))
                if original==data: continue
                journal.append((address,original))
                self.write(address,data)
                if p.read(address,len(data))!=data:
                    raise MemoryReadError('搬运字段回读不一致')
            return {'duration_ms':round((time.monotonic()-started)*1000,3),'writes':len(journal),
                    'gc_cards_marked':len(bitmap),'save_requested':True}
        except Exception:
            for address,original in reversed(journal): self.write(address,original)
            raise
        finally:
            if NT.NtResumeProcess(self.handle)<0:
                raise MemoryReadError('恢复游戏线程失败，需要立即检查游戏')

    def close(self):
        if self.handle:
            K.CloseHandle(self.handle)
            self.handle=None


def equipment(snapshot):
    return {row['item_uid']:row for c in snapshot['containers'].values() for row in c['slots'] if row.get('is_equipment')}


def main():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    state=json.load(opener.open('http://127.0.0.1:18741/api/state',timeout=5))
    if state['monitoring'] or state['busy']: raise RuntimeError('当前助手正在操作或监控装备，暂不执行验证')
    reader=TransferReader(game_pids()[0])
    writer=None
    moved=False
    report={'started_at_utc':datetime.now(timezone.utc).isoformat(),'pid':reader.pid}
    target_file=ROOT/'data/runtime/container-transfer-verification.json'
    try:
        before=reader.snapshot()
        if not before['complete']: raise MemoryReadError('装备读取不完整')
        candidates=[r for r in before['containers']['inventory']['slots'] if r.get('is_equipment') and r['count']==1 and not r.get('is_black_mist') and not r.get('locked')]
        item=min(candidates,key=lambda r:(r.get('rarity',99),r.get('item_level',999999),r['slot_index']))
        p=reader.memory
        inv=reader.singleton('Inventory')
        storage=reader.singleton('Storage')
        inv_array=p.u64(inv+56)
        storage_array=p.u64(storage+48)
        source=p.u64(inv_array+32+item['slot_index']*8)
        empty=[]
        for index in range(min(50,p.u64(storage_array+24))):
            address=p.u64(storage_array+32+index*8)
            header=p.read(address,48)
            if p.u64(address)==reader.classes['InventorySlot'] and not any(any(v) for v in slot_fields(header).values()):
                empty.append((index,address,header))
        target_index,target,target_header=empty[0]
        source_header=p.read(source,48)
        registry,_=reader.registry()
        generated=registry[item['item_uid']]
        generated_header=p.read(generated,128)
        guards=[(inv+56,struct.pack('<Q',inv_array)),(storage+48,struct.pack('<Q',storage_array)),
                (inv_array+32+item['slot_index']*8,struct.pack('<Q',source)),
                (storage_array+32+target_index*8,struct.pack('<Q',target)),(generated,generated_header)]
        manager=reader.singleton('GameManager')
        report.update({'name':item['internal_name'],'item_uid':item['item_uid'],'instance_id':item['instance_id'],
                       'source_slot':item['slot_index'],'storage_slot':target_index,
                       'active_map':reader.asset_name(p.u64(manager+112)),
                       'combat_flag_172':bool(p.read(manager+172,1)[0]),'before':before})
        target_file.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        backup=ROOT/'build/transfer-backup'
        backup.mkdir(parents=True,exist_ok=True)
        save_file=reader.memory.path.parent/'Data/save.json'
        if save_file.is_file(): shutil.copyfile(save_file,backup/'save-before-transfer.dds2')
        writer=FrozenSlotTransaction(reader)
        report['outbound']=writer.move(source,target,source_header,target_header,guards)
        moved=True
        after=reader.snapshot()
        if not after['complete']: raise MemoryReadError('转存后的快照读取不完整')
        found=[row for c in after['containers'].values() for row in c['slots'] if row.get('item_uid')==item['item_uid']]
        assert len(found)==1 and found[0]['container']=='storage' and found[0]['slot_index']==target_index
        for key in ('instance_id','modifiers','locked','item_level','upgrade_level','socket_count'):
            assert found[0][key]==item[key],key
        assert equipment(before).keys()==equipment(after).keys()
        report['after']=after
        report['confirmed']=True
        # Keep the live transition short and restore the original arrangement.
        time.sleep(.3)
        report['return']=writer.move(target,source,p.read(target,48),p.read(source,48),guards)
        moved=False
        restored=reader.snapshot()
        assert restored['complete']
        found=[row for c in restored['containers'].values() for row in c['slots'] if row.get('item_uid')==item['item_uid']]
        assert len(found)==1 and found[0]['container']=='inventory' and found[0]['slot_index']==item['slot_index']
        assert equipment(restored).keys()==equipment(before).keys()
        report['restored']=True
        report['game_running']=p.is_running()
        report['final_snapshot']=restored
        report['success']=True
    finally:
        if moved and writer:
            # Never overwrite a cell filled by a new pickup; move() rejects it.
            try:
                report['emergency_return']=writer.move(target,source,p.read(target,48),p.read(source,48),guards)
                report['restored']=True
            except Exception as exc: report['restore_error']=str(exc)
        target_file.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if writer: writer.close()
        reader.close()
    print(json.dumps({k:v for k,v in report.items() if k not in {'before','after','final_snapshot'}},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
