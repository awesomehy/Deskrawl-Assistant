"""Build-verified, atomic transfers between physical inventory/storage slots.

Mirrors 1.0.2 Storage.fqi's four slot fields. Virtual currencies (Inventory.nyl) are
separate from physical slots and are never modified. No injected code or calls.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import struct
import time
from .native_memory import K, MemoryReadError
from .paths import RESOURCE_ROOT

NT = C.WinDLL('ntdll')
GC_MODE_RVA = 0x3bc782c
GC_BITMAP_RVA = 0x3bda8e0
GAME_MANAGER_RVA = 0x3a18bf8
CLOUD_CLIENT_RVA = 0x3a1f820
for _name in ('NtSuspendProcess', 'NtResumeProcess'):
    _fn = getattr(NT, _name)
    _fn.argtypes = (W.HANDLE,)
    _fn.restype = C.c_long
K.WriteProcessMemory.argtypes = (W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t))
K.WriteProcessMemory.restype = W.BOOL


def slot_fields(header):
    return {16:header[16:24],24:header[24:28],32:header[32:40],40:header[40:41]}


def replace_fields(header, fields):
    result = bytearray(header)
    for offset,data in fields.items(): result[offset:offset+len(data)] = data
    return bytes(result)


def plan_transfers(sources, targets):
    """Allocate entire selections before writing, filling matching gem stacks.

    Stacking matches the exact ItemData reference, preserving type and grade.
    Generated items never merge. InventorySlot.Locked is OR'ed as in fnw.
    """
    addresses = [s['address'] for s in sources]+[t['address'] for t in targets]
    if not sources or len(set(addresses))!=len(addresses):
        raise MemoryReadError('移动槽位重复或为空')
    original = {r['address']:r['header'] for r in (*sources,*targets)}
    staged = dict(original)
    unique_uids = {r['address']:r.get('has_unique_uid',bool(struct.unpack_from('<Q',r['header'],32)[0])) for r in (*sources,*targets)}
    destinations = []
    stacked_count = 0
    new_slots = set()
    for source in sources:
        address,header = source['address'],source['header']
        item = struct.unpack_from('<Q',header,16)[0]
        count = struct.unpack_from('<i',header,24)[0]
        uid = struct.unpack_from('<Q',header,32)[0]
        if not item or not 1<=count<=2147483647 or header[40] not in (0,1):
            raise MemoryReadError('来源物品或数量无效')
        stackable = source.get('stackable',False) and not unique_uids[address]
        maximum = source.get('max_stack',1)
        if stackable and (not isinstance(maximum,int) or isinstance(maximum,bool) or not 1<=count<=maximum<=2147483647):
            raise MemoryReadError('宝石数量或堆叠上限异常')
        remaining = count
        if stackable and maximum>1:
            for target in targets:
                current = staged[target['address']]
                if struct.unpack_from('<Q',current,16)[0]!=item or unique_uids[target['address']]: continue
                present = struct.unpack_from('<i',current,24)[0]
                if not 1<=present<=maximum or current[40] not in (0,1):
                    raise MemoryReadError('目标宝石数量或锁定状态异常')
                amount = min(remaining,maximum-present)
                if not amount: continue
                staged[target['address']] = replace_fields(current,{24:struct.pack('<i',present+amount),40:bytes([int(bool(current[40] or header[40]))])})
                destinations.append({'slot_index':target['slot_index'],'name':source['name'],'count':amount,'merged':True})
                stacked_count += amount
                remaining -= amount
                if not remaining: break
        if remaining:
            empty = next((t for t in targets if not any(any(v) for v in slot_fields(staged[t['address']]).values())),None)
            if empty is None:
                raise MemoryReadError('目标堆叠容量及可用空格不足；未移动任何物品')
            values = slot_fields(header)
            values[24] = struct.pack('<i',remaining)
            staged[empty['address']] = replace_fields(staged[empty['address']],values)
            unique_uids[empty['address']] = unique_uids[address]
            destinations.append({'slot_index':empty['slot_index'],'name':source['name'],'count':remaining,'merged':False})
            new_slots.add(empty['address'])
        staged[address] = replace_fields(header,{16:b'\0'*8,24:b'\0'*4,32:b'\0'*8,40:b'\0'})
    changes = [(a,original[a],new) for a,new in staged.items() if new!=original[a]]
    return {'changes':changes,'destinations':destinations,'stacked_count':stacked_count,'new_slots':len(new_slots)}


def access_state(reader):
    """Read the page gates verified in 1.0.2 Storage.fpl and SaveSystem."""
    p = reader.memory
    profiles = getattr(reader, '_transfer_profiles', None)
    if profiles is None:
        profiles = {t['name']:t for t in json.loads((RESOURCE_ROOT/'data/container-transfer-types.json').read_text(encoding='utf-8'))}
        reader._transfer_profiles = profiles
        reader._transfer_verified = set()
    guards = []

    def observe(address, size):
        data = p.read(address,size)
        guards.append((address,data))
        return data

    def pointer(address):
        return struct.unpack('<Q',observe(address,8))[0]

    def verify(obj, name):
        if not obj: raise MemoryReadError('角色或仓库权限尚未载入，请稍后刷新')
        klass = pointer(obj)
        if (name,klass) not in reader._transfer_verified:
            if reader.class_name(klass) != name:
                raise MemoryReadError('仓库权限对象类型不一致')
            expected = [{'name':f['name'],'offset':f['rawRegistrationOffset'],'token':int(f['token'],16)} for f in profiles[name]['fields']]
            if reader.fields(klass) != expected:
                raise MemoryReadError('仓库权限字段布局不一致')
            reader._transfer_verified.add((name,klass))
        return obj

    def singleton(rva,name):
        klass = pointer(reader.module['base']+rva)
        obj = pointer(pointer(klass+184))
        return verify(obj,name)

    manager = singleton(GAME_MANAGER_RVA,'GameManager')
    controller = verify(pointer(manager+64),'PlayerCombatController')
    player = verify(pointer(controller+32),'Player')
    pages = struct.unpack('<i',observe(player+616,4))[0]
    if not 0 <= pages <= 3: raise MemoryReadError('仓库开放页数尚未核验')
    save = reader.singleton('SaveSystem')
    # Guard singleton roots as well as the page flags against character changes.
    save_static = pointer(reader.classes['SaveSystem']+184)
    if pointer(save_static) != save: raise MemoryReadError('角色保存对象发生变化')
    mode = struct.unpack('<i',observe(save+32,4))[0]
    if mode not in (0,1): raise MemoryReadError('当前角色模式未适配搬运')
    if mode == 1:
        cloud = singleton(CLOUD_CLIENT_RVA,'CloudClient')
        dlc = pointer(cloud+408)
        owned = observe(verify(dlc,'DlcState')+16,1) if dlc else b'\0'
    else:
        dlc = pointer(save+216)
        owned = observe(verify(dlc,'LocalDlcEntitlement')+28,1) if dlc else b'\0'
    if owned not in (b'\0',b'\1'): raise MemoryReadError('仓库扩展页状态异常')
    allowed = list(range((pages+1)*50))
    if owned == b'\1': allowed.extend(range(200,250))
    return {'available':True,'storage_indices':allowed,'storage_pages':pages+1+(owned==b'\1')}, guards


class FrozenSlotTransaction:
    """Briefly stop updates, check guards, commit the entire batch or undo it."""
    def __init__(self,reader):
        self.reader = reader
        self.handle = K.OpenProcess(0x800|0x20|0x8,False,reader.pid)
        if not self.handle: raise MemoryReadError('无法取得移动物品所需权限')

    def write(self,address,data):
        value = C.create_string_buffer(data)
        written = C.c_size_t()
        if not K.WriteProcessMemory(self.handle,address,value,len(data),C.byref(written)) or written.value != len(data):
            raise MemoryReadError('物品移动写入未完整确认')

    def move(self,source,target,source_header,target_header,guards):
        return self.move_many([(source,target,source_header,target_header)],guards)

    def move_many(self,pairs,guards):
        p = self.reader.memory
        addresses = [a for src,dst,_,_ in pairs for a in (src,dst)]
        if not pairs or len(set(addresses)) != len(addresses):
            raise MemoryReadError('移动槽位重复或为空')
        for source,target,head,empty in pairs:
            if len(head)!=48 or len(empty)!=48 or any(p.u64(a)!=self.reader.classes['InventorySlot'] for a in (source,target)):
                raise MemoryReadError('物品槽位类型未确认')
            if not struct.unpack_from('<Q',head,16)[0] or not 1<=struct.unpack_from('<i',head,24)[0]<=2147483647:
                raise MemoryReadError('来源物品或数量无效')
            if any(any(v) for v in slot_fields(empty).values()):
                raise MemoryReadError('目标槽位不是完整空位')
        changes = []
        for src,dst,head,empty in pairs:
            changes.extend([(dst,empty,replace_fields(empty,slot_fields(head))),
                            (src,head,replace_fields(head,slot_fields(empty)))])
        return self.apply_slots(changes,guards,moved=len(pairs))

    def apply_slots(self,changes,guards,*,moved):
        """Commit a conserved set of slot changes, including partial merges."""
        p = self.reader.memory
        addresses = [a for a,_,_ in changes]
        if not changes or len(set(addresses))!=len(addresses):
            raise MemoryReadError('移动槽位重复或为空')
        totals = {}
        identities = {}
        uid_values = {}
        guards = list(guards)
        for address,before,after in changes:
            if len(before)!=48 or len(after)!=48 or p.u64(address)!=self.reader.classes['InventorySlot']:
                raise MemoryReadError('物品槽位类型未确认')
            if replace_fields(before,slot_fields(after))!=after:
                raise MemoryReadError('只能修改物品槽位内容')
            for header,sign in ((before,-1),(after,1)):
                item = struct.unpack_from('<Q',header,16)[0]
                count = struct.unpack_from('<i',header,24)[0]
                uid = struct.unpack_from('<Q',header,32)[0]
                if header[40] not in (0,1) or (item and count<=0) or (not item and (count or uid or header[40])):
                    raise MemoryReadError('槽位物品或数量无效')
                if item: totals[item] = totals.get(item,0)+count*sign
                if uid:
                    if uid not in uid_values:
                        uid_values[uid] = self.reader.string(uid)
                        guards.append((uid,p.read(uid,20+p.i32(uid+16)*2)))
                    # String.Empty is a placeholder shared by ordinary stacks,
                    # not a generated-item identity. Keep its reference intact.
                    if uid_values[uid]: identities[(uid,item)] = identities.get((uid,item),0)+sign
        if any(totals.values()) or any(identities.values()):
            raise MemoryReadError('移动前后数量或物品编号不一致')
        started = time.monotonic()
        if NT.NtSuspendProcess(self.handle)<0:
            raise MemoryReadError('无法暂停游戏数据更新，未移动物品')
        journal = []
        try:
            for address,before,_ in changes:
                if p.read(address,48)!=before:
                    raise MemoryReadError('槽位在准备期间发生变化，未移动物品')
            if any(p.read(a,len(v))!=v for a,v in guards):
                raise MemoryReadError('物品或仓库权限发生变化，请刷新后重试')
            if time.monotonic()-started>1: raise MemoryReadError('验证耗时异常，未移动物品')
            base = self.reader.module['base']
            if p.i32(base+GC_MODE_RVA) not in (0,1): raise MemoryReadError('GC 写屏障状态未确认')
            bitmap = {}
            for slot in addresses:
                for offset in (16,32):
                    page = ((slot+offset)>>12)&0x1fffff
                    word = base+GC_BITMAP_RVA+(page>>6)*8
                    bitmap[word] = bitmap.get(word,p.u64(word)) | (1<<(page&63))
            # Dirty cards before publishing reference fields; threads are paused.
            plan = [(a,struct.pack('<Q',bits)) for a,bits in bitmap.items()]
            for address,_,after in changes:
                plan.extend((address+off,data) for off,data in slot_fields(after).items())
            save = self.reader.singleton('SaveSystem')
            if p.read(save+92,1) not in (b'\0',b'\1'): raise MemoryReadError('保存状态异常')
            plan.append((save+92,b'\1'))
            for address,data in plan:
                original = p.read(address,len(data))
                if original == data: continue
                journal.append((address,original))
                self.write(address,data)
                if p.read(address,len(data))!=data: raise MemoryReadError('物品移动回读不一致')
            return {'moved':moved,'save_requested':True,'duration_ms':round((time.monotonic()-started)*1000,3)}
        except Exception as exc:
            failed = []
            for address,original in reversed(journal):
                try:
                    self.write(address,original)
                    if p.read(address,len(original))!=original: raise MemoryReadError('回滚回读失败')
                except Exception: failed.append(address)
            if failed: raise MemoryReadError('移动失败且部分字段未能恢复，请立即停止游戏操作并检查存档') from exc
            raise
        finally:
            if NT.NtResumeProcess(self.handle)<0:
                # Retry once even if the first resume fails; never silently leave frozen.
                if NT.NtResumeProcess(self.handle)<0:
                    raise MemoryReadError('恢复游戏运行失败，需要立即检查游戏')

    def close(self):
        if self.handle:
            K.CloseHandle(self.handle)
            self.handle = None


def move_items(reader,expected,target,validate=lambda:True):
    if target not in ('inventory','storage') or not expected:
        raise MemoryReadError('请选择物品和移动方向')
    snapshot = reader.snapshot()
    if not snapshot['complete']: raise MemoryReadError('物品正在变化，请刷新后重试')
    access,guards = access_state(reader)
    allowed = set(access['storage_indices'])
    source_name = 'storage' if target=='inventory' else 'inventory'
    current = {r.get('transfer_key'):r for r in snapshot['containers'][source_name]['slots']}
    keys = [r.get('transfer_key') for r in expected]
    if any(not k or k not in current for k in keys) or len(set(keys))!=len(keys):
        raise MemoryReadError('所选物品已变化或移动，请刷新后重新选择')
    p = reader.memory
    arrays = {}
    for name,klass,offset in [('inventory','Inventory',56),('storage','Storage',48)]:
        root = reader.singleton(klass)
        array = p.u64(root+offset)
        entries,block = reader.array(array,'InventorySlot',limit=2048,stride=8)
        arrays[name] = [struct.unpack('<Q',v)[0] for v in entries]
        guards.extend([(root+offset,struct.pack('<Q',array)),(array+32,block)])
    targets = []
    for index,address in enumerate(arrays[target]):
        if target=='storage' and index not in allowed: continue
        if not address or p.u64(address)!=reader.classes['InventorySlot']: continue
        header = p.read(address,48)
        targets.append({'slot_index':index,'address':address,'header':header,
                        'has_unique_uid':bool(reader.string(struct.unpack_from('<Q',header,32)[0]))})
    sources = []
    for key in keys:
        row = current[key]
        if source_name=='storage' and row['slot_index'] not in allowed:
            raise MemoryReadError('所选物品位于尚未开放的仓库页')
        if row.get('item_type')==1: raise MemoryReadError('虚拟材料不属于可移动的实体物品')
        source = arrays[source_name][row['slot_index']]
        head = p.read(source,48)
        # Rebuild the same fingerprint; never accept an address from the caller.
        if reader.transfer_key(source_name,row['slot_index'],source,head,row)!=key:
            raise MemoryReadError('来源物品发生变化，请刷新后重试')
        stackable = row.get('item_type')==6 and row.get('item_class')=='GemData' and not row.get('item_uid') and not row.get('is_equipment')
        item = struct.unpack_from('<Q',head,16)[0]
        maximum = p.i32(item+136) if stackable else 1
        if stackable: guards.append((item+136,struct.pack('<i',maximum)))
        sources.append({'address':source,'header':head,'name':row['internal_name'],
                        'stackable':stackable,'max_stack':maximum,'has_unique_uid':bool(row.get('item_uid'))})
    allocation = plan_transfers(sources,targets)
    guards.extend(reader.snapshot_stamps)
    if not validate(): raise MemoryReadError('移动已取消，未更改物品')
    tx = FrozenSlotTransaction(reader)
    try: result = tx.apply_slots(allocation['changes'],guards,moved=len(expected))
    finally: tx.close()
    return {**result,'destinations':[{'container':target,**d} for d in allocation['destinations']],
            'stacked_count':allocation['stacked_count'],'new_slots':allocation['new_slots']}
