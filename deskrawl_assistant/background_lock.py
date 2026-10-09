"""Set verified equipment lock booleans and request normal game saving.

No code injection, method invocation, keyboard input or arbitrary value editor.
The reader continues to use a separate read/query-only handle.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import struct

from .native_memory import K, MemoryReadError, SnapshotChangedError
from .action_log import record

K.WriteProcessMemory.argtypes = (W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t))
K.WriteProcessMemory.restype = W.BOOL


class TrueBitWriter:
    def __init__(self, pid):
        self.handle = K.OpenProcess(0x20 | 0x8, False, pid)
        if not self.handle:
            raise MemoryReadError("无法打开装备锁定写入权限")

    def set_true(self, address):
        self.set_bool(address, True)

    def set_bool(self, address, locked):
        if not isinstance(locked, bool):
            raise MemoryReadError("锁状态必须为布尔值")
        if isinstance(address, bool) or not isinstance(address, int) or not 0x10000 <= address < 0x7fffffffffff:
            raise MemoryReadError("锁定标记地址无效")
        value = C.c_ubyte(int(locked))
        written = C.c_size_t()
        if not K.WriteProcessMemory(self.handle, address, C.byref(value), 1, C.byref(written)) or written.value != 1:
            raise MemoryReadError("锁定标记写入未完整确认")

    def close(self):
        if self.handle:
            K.CloseHandle(self.handle)
            self.handle = None


def identity(row):
    return tuple(row.get(key) for key in ('item_uid', 'instance_id', 'name_key'))


def lock_equipment(reader, expected, validate=lambda row: True, writer_factory=TrueBitWriter):
    """Resolve afresh, revalidate and set true; never toggle or unlock."""
    return _set_equipment_locked(reader, expected, True, validate, writer_factory)


def unlock_equipment(reader, expected, validate=lambda row: True, writer_factory=TrueBitWriter):
    """Explicit manual unlock; retains the same identity and save guards."""
    return _set_equipment_locked(reader, expected, False, validate, writer_factory)


def _set_equipment_locked(reader, expected, locked, validate, writer_factory):
    status = 'locked' if locked else 'unlocked'
    required = identity(expected)
    if not all(isinstance(value, str) and value for value in required) or expected.get('is_equipment') is not True:
        raise MemoryReadError("装备身份不完整，未写入")
    fresh = reader.snapshot()
    if not fresh.get('complete'):
        raise MemoryReadError("当前装备读取不完整，未写入")
    matches = [row for container in fresh['containers'].values() for row in container.get('slots', [])
               if identity(row) == required and row.get('is_equipment') is True]
    if len(matches) != 1:
        raise MemoryReadError("装备已移出背包和仓库或身份不唯一，未写入")
    actual = matches[0]
    if actual.get('locked') is locked:
        return {'status':'already_'+status, 'item_uid':required[0], 'instance_id':required[1]}
    if actual.get('locked') is not (not locked) or not validate(actual):
        raise MemoryReadError("锁状态未确认或最新词条不符合规则，未写入")
    registry, stamp = reader.registry()
    address = registry.get(required[0])
    if not address:
        raise MemoryReadError("装备注册表已变化，未写入")
    latest = reader.generated(address)
    if latest.get('instance_id') != required[1] or latest.get('locked') is not (not locked):
        raise MemoryReadError("装备实例或锁状态已经改变，未写入")
    # The copied modifiers must still describe the same rule observation.
    if latest.get('modifiers') != actual.get('modifiers') or (locked and latest.get('is_black_mist')):
        raise MemoryReadError("装备词条已变化或未揭示，未写入")
    p = reader.memory
    save = reader.singleton('SaveSystem')
    if reader.offsets['GeneratedItemData'].get('Locked') != 73 or reader.offsets['SaveSystem'].get('nlo') != 92:
        raise MemoryReadError("锁定或保存字段布局不一致，未写入")
    if p.read(save + 92, 1) not in (b'\0', b'\1'):
        raise MemoryReadError("游戏保存状态异常，未写入")
    writer = writer_factory(reader.pid)
    try:
        # Re-read through the still-live original process handle immediately
        # before the one-byte mutation; no cached screen or object addresses.
        if (p.u64(address) != reader.classes['GeneratedItemData'] or
            reader.string(p.u64(address + 80)) != required[1] or
            p.u64(save) != reader.classes['SaveSystem']):
            raise MemoryReadError("写入前装备或保存对象发生变化，未写入")
        if (p.i32(stamp['object'] + 44) != stamp['version'] or
            p.u64(stamp['object'] + 24) != stamp['entries'] or
            ('header' in stamp and p.read(stamp['object'] + 24, 24) != stamp['header'])):
            raise SnapshotChangedError("写入前装备注册表发生变化，未写入")
        desired = bytes([int(locked)])
        if p.read(address + 73, 1) == desired:
            return {'status':'already_'+status,'item_uid':required[0],'instance_id':required[1]}
        if p.read(address + 73, 1) != bytes([int(not locked)]):
            raise MemoryReadError("锁状态异常，未写入")
        record('background_'+status+'_started', item_uid=required[0], instance_id=required[1], name_key=required[2])
        if locked:
            writer.set_true(address + 73)
        else:
            writer.set_bool(address + 73, False)
        if p.read(address + 73, 1) != desired or reader.string(p.u64(address + 80)) != required[1]:
            raise MemoryReadError("锁状态写入未通过回读，操作停止")
        # SaveSystem.eho/ehp/ehq/ehr/ehs/eht set nlo=true in game 1.0.2.
        # Its normal town autosave then copies GeneratedItemData.Locked into
        # SavedRegistryEntry.Locked; we do not edit encrypted save files.
        writer.set_true(save + 92)
        record('background_'+status+'_confirmed', item_uid=required[0], instance_id=required[1], save_requested=True)
        return {'status':status, 'backend':'direct_set_bool', 'item_uid':required[0], 'instance_id':required[1],
                'container':actual.get('container'), 'slot_index':actual.get('slot_index'), 'save_requested':True}
    finally:
        writer.close()
