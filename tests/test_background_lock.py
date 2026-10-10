"""Exercise direct locking with fake memory; no process writes in tests."""
import copy
import unittest
from unittest.mock import patch
from deskrawl_assistant.background_lock import lock_equipment, unlock_equipment, TrueBitWriter
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError

ITEM = {'container':'inventory','slot_index':1,'is_equipment':True,'item_uid':'belt-uid',
        'instance_id':'belt-instance','name_key':'LegendaryBelt3','locked':False,
        'modifiers':[{'stat':9,'value':141}], 'is_black_mist':False}
OBJECT = 0x100000
SAVE = 0x300000
REGISTRY = 0x400000


class FakeMemory:
    def __init__(self):
        self.bytes = {OBJECT+73:b'\0', SAVE+92:b'\0', SAVE+193:b'\0'}
        self.pointers = {OBJECT:0x110000, OBJECT+80:0x500000, SAVE:0x310000, REGISTRY+24:0x420000}
        self.ints = {REGISTRY+44:7}
    def read(self,address,size): return self.bytes[address]
    def u64(self,address): return self.pointers[address]
    def i32(self,address): return self.ints[address]


class FakeReader:
    pid = 777
    classes = {'GeneratedItemData':0x110000,'SaveSystem':0x310000}
    offsets = {'GeneratedItemData':{'Locked':73},'SaveSystem':{'nly':92}}
    def __init__(self):
        self.memory = FakeMemory()
        self.row = copy.deepcopy(ITEM)
        self.complete = True
    def snapshot(self):
        row = {**self.row,'locked':self.memory.read(OBJECT+73,1)==b'\1'}
        return {'complete':self.complete,'containers':{'inventory':{'slots':[row]}}}
    def registry(self):
        return {'belt-uid':OBJECT}, {'object':REGISTRY,'entries':0x420000,'version':7}
    def generated(self,address):
        return {**self.row,'locked':self.memory.read(OBJECT+73,1)==b'\1'}
    def singleton(self,name): return SAVE
    def string(self,address): return 'belt-instance'


class BackgroundLockTests(unittest.TestCase):
    def setUp(self):
        self.log = patch('deskrawl_assistant.background_lock.record')
        self.log.start()
        self.addCleanup(self.log.stop)
        self.reader = FakeReader()
        self.writes = []
        self.closed = False

    def factory(self,pid):
        test = self
        class Writer:
            def set_true(self,address):
                test.writes.append(address)
                test.reader.memory.bytes[address]=b'\1'
            def close(self): test.closed=True
            def set_bool(self,address,value):
                test.writes.append((address,value))
                test.reader.memory.bytes[address]=bytes([int(value)])
        return Writer()

    def lock(self,validate=lambda row:True,factory=None):
        return lock_equipment(self.reader,ITEM,validate,factory or self.factory)

    def test_unlocked_item_sets_only_lock_and_save_dirty(self):
        result = self.lock()
        self.assertEqual(result['status'],'locked')
        self.assertEqual(self.writes,[OBJECT+73,SAVE+92])
        self.assertTrue(self.closed)

    def test_already_locked_is_idempotent_without_write_handle(self):
        self.reader.memory.bytes[OBJECT+73]=b'\1'
        self.assertEqual(self.lock()['status'],'already_locked')
        self.assertFalse(self.writes)
        self.assertFalse(self.closed)

    def test_changed_instance_never_writes(self):
        self.reader.row['instance_id']='different'
        with self.assertRaises(MemoryReadError): self.lock()
        self.assertFalse(self.writes)

    def test_changed_rule_never_writes(self):
        with self.assertRaises(MemoryReadError): self.lock(lambda row:False)
        self.assertFalse(self.writes)

    def test_incomplete_snapshot_never_writes(self):
        self.reader.complete=False
        with self.assertRaises(MemoryReadError): self.lock()
        self.assertFalse(self.writes)

    def test_old_save_field_name_or_wrong_offset_never_writes(self):
        for fields in ({'nlo':92}, {'nly':93}):
            with self.subTest(fields=fields):
                self.reader.offsets = {'GeneratedItemData':{'Locked':73},'SaveSystem':fields}
                with self.assertRaisesRegex(MemoryReadError,'字段布局不一致'):
                    self.lock()
                self.assertFalse(self.writes)

    def test_game_item_gate_blocks_writes_including_boundary_changes(self):
        self.reader.memory.bytes[SAVE+193]=b'\1'
        with self.assertRaisesRegex(MemoryReadError,'暂时禁止'):
            self.lock()
        self.assertFalse(self.writes)
        self.reader.memory.bytes[SAVE+193]=b'\0'
        def blocked(pid):
            writer=self.factory(pid)
            self.reader.memory.bytes[SAVE+193]=b'\1'
            return writer
        with self.assertRaisesRegex(MemoryReadError,'暂时禁止'):
            self.lock(factory=blocked)
        self.assertFalse(self.writes)
        self.assertTrue(self.closed)

    def test_verified_renamed_save_field_is_used(self):
        self.reader.compatibility_bindings={'save_dirty_field':'zzz','lock_field':'Locked'}
        self.reader.offsets={'GeneratedItemData':{'Locked':73},'SaveSystem':{'zzz':92}}
        self.assertEqual(self.lock()['status'],'locked')
        self.assertEqual(self.writes,[OBJECT+73,SAVE+92])

    def test_file_change_guard_prevents_opening_write_handle(self):
        def changed():raise MemoryReadError('游戏文件已变化')
        self.reader.check_game_files=changed
        with self.assertRaisesRegex(MemoryReadError,'文件已变化'):self.lock()
        self.assertFalse(self.writes);self.assertFalse(self.closed)

    def test_registry_change_at_write_boundary_cancels_action(self):
        def changed(pid):
            writer=self.factory(pid)
            self.reader.memory.ints[REGISTRY+44]=8
            return writer
        with self.assertRaises(SnapshotChangedError): self.lock(factory=changed)
        self.assertFalse(self.writes)
        self.assertTrue(self.closed)

    def test_black_mist_is_not_written(self):
        self.reader.row['is_black_mist']=True
        with self.assertRaises(MemoryReadError): self.lock()
        self.assertFalse(self.writes)

    def test_unlock_changes_only_lock_to_false_and_save_to_true(self):
        self.reader.memory.bytes[OBJECT+73]=b'\1'
        result=unlock_equipment(self.reader,ITEM,writer_factory=self.factory)
        self.assertEqual(result['status'],'unlocked')
        self.assertEqual(self.writes,[(OBJECT+73,False),SAVE+92])
        self.assertTrue(self.closed)

    def test_already_unlocked_does_not_open_writer(self):
        result=unlock_equipment(self.reader,ITEM,writer_factory=self.factory)
        self.assertEqual(result['status'],'already_unlocked')
        self.assertFalse(self.writes)

    def test_unlock_changed_identity_never_writes(self):
        self.reader.memory.bytes[OBJECT+73]=b'\1'
        self.reader.row['instance_id']='new-instance'
        with self.assertRaises(MemoryReadError): unlock_equipment(self.reader,ITEM,writer_factory=self.factory)
        self.assertFalse(self.writes)

    def test_unlock_incomplete_snapshot_never_writes(self):
        self.reader.memory.bytes[OBJECT+73]=b'\1'
        self.reader.complete=False
        with self.assertRaises(MemoryReadError): unlock_equipment(self.reader,ITEM,writer_factory=self.factory)
        self.assertFalse(self.writes)

    def test_boolean_writer_rejects_non_boolean_before_any_write(self):
        writer=object.__new__(TrueBitWriter)
        with self.assertRaises(MemoryReadError): writer.set_bool(OBJECT+73,0)


if __name__=='__main__': unittest.main()
