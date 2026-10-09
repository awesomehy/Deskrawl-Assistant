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

from deskrawl_assistant.container_transfer import FrozenSlotTransaction, NT, slot_fields


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
