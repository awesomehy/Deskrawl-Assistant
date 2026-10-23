"""Prewrite carriage races retry; writes, rollback and thread resume stay fenced."""
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.carriage_transfer import FrozenCarriageTransaction
from deskrawl_assistant.native_memory import GameBusyError, MemoryReadError, SnapshotChangedError
from test_container_transfer_probe import FakeMemory


class CarriagePreflightTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.drop, self.target, self.item, self.save = 0x100000, 0x200000, 0x300000, 0x400000
        self.reader = SimpleNamespace(memory=self.memory, classes={'InventorySlot':77, 'LootDrop':88},
            module={'base':0x10000000}, singleton=lambda name:self.save, string=lambda value:'')
        header = bytearray(216)
        for offset, value in ((0,88), (16,0x500000), (120,self.item)):
            struct.pack_into('<Q',header,offset,value)
        struct.pack_into('<i',header,168,3)
        self.header = bytes(header)
        self.empty = struct.pack('<Q',77) + b'\0'*40
        self.memory.put(self.drop,self.header)
        self.memory.put(self.target,self.empty)
        self.memory.put(self.item+32,struct.pack('<i',6))
        self.tx = object.__new__(FrozenCarriageTransaction)
        self.tx.reader, self.tx.handle = self.reader, 123
        self.tx.write = Mock(side_effect=self.memory.put)
        self.suspend = patch('deskrawl_assistant.carriage_transfer.NT.NtSuspendProcess',return_value=0).start()
        self.resume = patch('deskrawl_assistant.carriage_transfer.NT.NtResumeProcess',return_value=0).start()
        self.addCleanup(patch.stopall)

    def collect(self, guards=()):
        return self.tx.collect_many(self.drop,self.header,[(self.target,self.empty,3)],guards)

    def assert_untouched(self):
        self.tx.write.assert_not_called()
        self.assertEqual(self.memory.read(self.save+92,1),b'\0')
        self.assertEqual(self.memory.read(self.drop+185,1),b'\0')
        self.resume.assert_called_once()

    def test_changed_container_guard_defers_then_fresh_guard_can_collect(self):
        guard = 0x600000
        self.memory.put(guard,b'new')
        with self.assertRaisesRegex(GameBusyError,'容器或唯一编号发生变化'):
            self.collect([(guard,b'old')])
        self.assert_untouched()
        result = self.collect([(guard,b'new')])
        self.assertTrue(result['save_requested'])
        self.assertEqual(self.memory.read(self.drop+185,1),b'\1')
        self.assertEqual(self.memory.u64(self.target+16),self.item)

    def test_drop_change_before_freeze_never_collects_stale_item(self):
        self.memory.put(self.drop+168,struct.pack('<i',4))
        with self.assertRaisesRegex(GameBusyError,'物品或槽位发生变化'):
            self.collect()
        self.assert_untouched()

    def test_slot_filled_between_planning_and_freeze_is_not_overwritten(self):
        def suspend(_handle):
            self.memory.put(self.target+16,struct.pack('<Q',0x700000))
            return 0
        self.suspend.side_effect = suspend
        with self.assertRaisesRegex(GameBusyError,'物品或槽位发生变化'):
            self.collect()
        self.assert_untouched()
        self.assertEqual(self.memory.u64(self.target+16),0x700000)

    def test_slow_prewrite_check_preserves_one_second_limit(self):
        with patch('deskrawl_assistant.carriage_transfer.time.perf_counter',side_effect=[10,11.01]), \
                self.assertRaisesRegex(GameBusyError,'核验耗时异常'):
            self.collect()
        self.assert_untouched()

    def test_postwrite_failure_rolls_back_and_is_not_retryable(self):
        failed = False
        def write(address, data):
            nonlocal failed
            self.memory.put(address,data)
            if address == self.target+24 and not failed:
                failed = True
                raise MemoryReadError('字段回读不一致')
        self.tx.write.side_effect = write
        with self.assertRaises(MemoryReadError) as error:
            self.collect()
        self.assertNotIsInstance(error.exception,SnapshotChangedError)
        self.assertEqual(self.memory.read(self.target,48),self.empty)
        self.assertEqual(self.memory.read(self.drop,216),self.header)
        self.assertEqual(self.memory.read(self.save+92,1),b'\0')
        self.resume.assert_called_once()


if __name__ == '__main__': unittest.main()
