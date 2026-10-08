"""Verify multi-field transfer rollback before any live test."""
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from verify_container_transfer import FrozenSlotTransaction
from verify_carriage_transfer import FrozenCarriageTransaction


class FakeMemory:
    def __init__(self): self.bytes={}
    def put(self,address,value): self.bytes.update({address+i:b for i,b in enumerate(value)})
    def read(self,address,size): return bytes(self.bytes.get(address+i,0) for i in range(size))
    def u64(self,address): return struct.unpack('<Q',self.read(address,8))[0]
    def i32(self,address): return struct.unpack('<i',self.read(address,4))[0]


class TransactionTests(unittest.TestCase):
    def make(self):
        p=FakeMemory()
        source,target,klass,save=0x100000,0x200000,0x300000,0x400000
        head=bytearray(48)
        struct.pack_into('<Q',head,0,klass)
        empty=bytes(head)
        struct.pack_into('<Q',head,16,0x500000)
        struct.pack_into('<i',head,24,1)
        struct.pack_into('<Q',head,32,0x600000)
        head[40]=1
        full=bytes(head)
        p.put(source,full); p.put(target,empty)
        reader=SimpleNamespace(memory=p,classes={'InventorySlot':klass},module={'base':0x10000000},singleton=lambda name:save)
        tx=object.__new__(FrozenSlotTransaction)
        tx.reader=reader; tx.handle=123
        tx.write=p.put
        return p,tx,source,target,full,empty

    @patch('verify_container_transfer.NT.NtSuspendProcess',return_value=0)
    @patch('verify_container_transfer.NT.NtResumeProcess',return_value=0)
    def test_move_preserves_slot_headers_and_identity(self,resume,suspend):
        p,tx,source,target,full,empty=self.make()
        result=tx.move(source,target,full,empty,[])
        self.assertEqual(p.read(source,48),empty)
        self.assertEqual(p.read(target,48),full)
        self.assertTrue(result['save_requested'])
        page=((target+32)>>12)&0x1fffff
        self.assertTrue(p.u64(0x10000000+0x3c08200+(page>>6)*8)&(1<<(page&63)))
        resume.assert_called_once()

    @patch('verify_container_transfer.NT.NtSuspendProcess',return_value=0)
    @patch('verify_container_transfer.NT.NtResumeProcess',return_value=0)
    def test_mid_write_failure_restores_both_slots(self,resume,suspend):
        p,tx,source,target,full,empty=self.make()
        calls=0
        def fail_once(address,data):
            nonlocal calls
            calls+=1
            p.put(address,data)
            if calls==4: raise RuntimeError('partial write')
        tx.write=fail_once
        with self.assertRaisesRegex(RuntimeError,'partial write'):
            tx.move(source,target,full,empty,[])
        self.assertEqual(p.read(source,48),full)
        self.assertEqual(p.read(target,48),empty)
        resume.assert_called_once()

    @patch('verify_container_transfer.NT.NtSuspendProcess',return_value=0)
    @patch('verify_container_transfer.NT.NtResumeProcess',return_value=0)
    def test_changed_cell_prevents_any_writes(self,resume,suspend):
        p,tx,source,target,full,empty=self.make()
        p.put(target+24,b'\1\0\0\0')
        changed=p.read(target,48)
        writes=[]
        tx.write=lambda address,data:writes.append(address)
        with self.assertRaisesRegex(RuntimeError,'槽位在准备期间发生变化'):
            tx.move(source,target,full,empty,[])
        self.assertFalse(writes)
        self.assertEqual(p.read(target,48),changed)
        resume.assert_called_once()

    @patch('verify_carriage_transfer.NT.NtSuspendProcess',return_value=0)
    @patch('verify_carriage_transfer.NT.NtResumeProcess',return_value=0)
    def test_carriage_copies_uid_and_requests_native_scene_cleanup(self,resume,suspend):
        p,tx,source,target,full,empty=self.make()
        drop=0x700000
        header=bytearray(216)
        struct.pack_into('<Q',header,0,0x800000)
        struct.pack_into('<Q',header,16,0x900000)
        struct.pack_into('<Q',header,120,0x500000)
        struct.pack_into('<Q',header,136,0x600000)
        struct.pack_into('<i',header,168,1)
        p.put(drop,header)
        tx.reader.classes['LootDrop']=0x800000
        collector=object.__new__(FrozenCarriageTransaction)
        collector.reader=tx.reader; collector.handle=123; collector.write=p.put
        result=collector.collect(drop,bytes(header),target,empty,[])
        self.assertEqual(p.u64(target+16),0x500000)
        self.assertEqual(p.u64(target+32),0x600000)
        self.assertEqual(p.i32(target+24),1)
        self.assertEqual(p.read(drop+185,1),b'\1')
        self.assertGreater(struct.unpack('<f',p.read(drop+160,4))[0],0)
        self.assertEqual(p.u64(drop+16),0x900000)
        self.assertEqual(result['cleanup'],'game_expiry_sweep')
        resume.assert_called_once()

    @patch('verify_carriage_transfer.NT.NtSuspendProcess',return_value=0)
    @patch('verify_carriage_transfer.NT.NtResumeProcess',return_value=0)
    def test_carriage_failure_leaves_original_drop_and_empty_target(self,resume,suspend):
        p,tx,source,target,full,empty=self.make()
        drop=0x700000
        header=bytearray(216)
        struct.pack_into('<Q',header,0,0x800000)
        struct.pack_into('<Q',header,16,0x900000)
        struct.pack_into('<Q',header,120,0x500000)
        struct.pack_into('<Q',header,136,0x600000)
        struct.pack_into('<i',header,168,1)
        p.put(drop,header)
        tx.reader.classes['LootDrop']=0x800000
        collector=object.__new__(FrozenCarriageTransaction)
        collector.reader=tx.reader; collector.handle=123
        calls=0
        def fail_once(address,data):
            nonlocal calls
            calls+=1
            p.put(address,data)
            if calls==3: raise RuntimeError('partial write')
        collector.write=fail_once
        with self.assertRaisesRegex(RuntimeError,'partial write'):
            collector.collect(drop,bytes(header),target,empty,[])
        self.assertEqual(p.read(drop,216),bytes(header))
        self.assertEqual(p.read(target,48),empty)
        resume.assert_called_once()


if __name__=='__main__': unittest.main()
