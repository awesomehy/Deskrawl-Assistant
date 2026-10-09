"""Regression coverage for live registry changes; no game process writes."""
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.native_reader import NativeReader
from deskrawl_assistant.runtime_client import RuntimeClient


ROOT = 0x10000
STATIC = 0x20000
ENTRIES = 0x30000
DICT_CLASS = 0x40000
ARRAY_CLASS = 0x50000
ELEMENT_CLASS = 0x60000
GENERATED_CLASS = 0x70000
REGISTRY_CLASS = 0x80000


class RegistryMemory:
    def __init__(self):
        self.count = 2
        self.free_count = 0
        self.version = 1
        self.block = (struct.pack('<iiQQ', 1, -1, 0x90000, 0xa0000) +
                      struct.pack('<iiQQ', 2, -1, 0x90010, 0xa0010))
        self.on_block_read = None

    def read(self, address, size):
        if address == ROOT + 24:
            return struct.pack('<Qiiii', ENTRIES, self.count, -1, self.free_count, self.version)
        if address == ENTRIES + 32:
            block = self.block[:size]
            if self.on_block_read:
                hook, self.on_block_read = self.on_block_read, None
                hook()
            return block
        raise AssertionError((address, size))

    def u64(self, address):
        return {REGISTRY_CLASS + 184: STATIC, STATIC: ROOT, ROOT: DICT_CLASS,
                ENTRIES: ARRAY_CLASS, ARRAY_CLASS + 64: ELEMENT_CLASS,
                ENTRIES + 24: 2, 0xa0000: GENERATED_CLASS, 0xa0010: GENERATED_CLASS}[address]

    def i32(self, address):
        self_size = {ARRAY_CLASS + 0x104: 24}
        return self_size[address]

    def remove_second(self, *, update_version=True):
        self.free_count = 1
        if update_version:
            self.version += 1
        self.block = self.block[:24] + struct.pack('<iiQQ', -1, -1, 0, 0)


class RegistrySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.reader = object.__new__(NativeReader)
        self.reader.classes = {'gj': REGISTRY_CLASS, 'GeneratedItemData': GENERATED_CLASS}
        self.reader.memory = RegistryMemory()
        self.reader.snapshot_stamps = []
        self.reader.class_name = lambda klass: 'Dictionary`2'
        layouts = {DICT_CLASS: {'_entries': 24, '_count': 32, '_freeCount': 40, '_version': 44},
                   ELEMENT_CLASS: {'hashCode': 16, 'next': 20, 'key': 24, 'value': 32}}
        self.reader.fields = lambda klass: [{'name': n, 'offset': o} for n, o in layouts[klass].items()]
        self.reader.string = lambda address: {0x90000: 'first', 0x90010: 'second'}[address]

    def test_deleted_entries_use_copied_free_count(self):
        self.reader.memory.remove_second()
        records, stamp = self.reader.registry()
        self.assertEqual(records, {'first': 0xa0000})
        self.assertEqual(stamp['count'] - stamp['free_count'], len(records))
        self.assertIn((ROOT + 24, stamp['header']), self.reader.snapshot_stamps)

    def test_deletion_during_uid_decoding_retries_instead_of_count_error(self):
        original = self.reader.string

        def decode(address):
            if address == 0x90000:
                self.reader.memory.remove_second()
            return original(address)

        self.reader.string = decode
        with self.assertRaises(SnapshotChangedError):
            self.reader.registry()
        self.reader.string = original
        self.assertEqual(self.reader.registry()[0], {'first': 0xa0000})

    def test_change_while_copying_entries_is_transient(self):
        self.reader.memory.on_block_read = self.reader.memory.remove_second
        with self.assertRaises(SnapshotChangedError):
            self.reader.registry()

    def test_entry_change_without_version_bump_is_detected(self):
        original = self.reader.string

        def decode(address):
            self.reader.memory.block = self.reader.memory.block[:24] + struct.pack('<iiQQ', 2, -1, 0x90000, 0xa0000)
            return original(address)

        self.reader.string = decode
        with self.assertRaises(SnapshotChangedError):
            self.reader.registry()

    def test_stable_invalid_count_remains_a_hard_error(self):
        self.reader.memory.remove_second()
        self.reader.memory.free_count = 0
        with self.assertRaises(MemoryReadError) as raised:
            self.reader.registry()
        self.assertNotIsInstance(raised.exception, SnapshotChangedError)
        self.assertIn('有效条目数不一致', str(raised.exception))


class SnapshotRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = RuntimeClient()
        self.client._reader = Mock()

    def test_transient_read_retries_without_any_write(self):
        snapshot = {'complete': True}
        self.client._reader.snapshot.side_effect = [SnapshotChangedError('变化'), SnapshotChangedError('变化'), snapshot]
        with patch('deskrawl_assistant.runtime_client.time.sleep'):
            self.assertIs(self.client.call('snapshot'), snapshot)
        self.assertEqual(self.client._reader.snapshot.call_count, 3)
        self.assertEqual(len(self.client._reader.method_calls), 3)

    def test_continuously_changing_read_is_bounded(self):
        self.client._reader.snapshot.side_effect = SnapshotChangedError('变化')
        with patch('deskrawl_assistant.runtime_client.time.sleep'), self.assertRaises(SnapshotChangedError):
            self.client.call('snapshot')
        self.assertEqual(self.client._reader.snapshot.call_count, 3)

    def test_structural_error_is_not_retried(self):
        self.client._reader.snapshot.side_effect = MemoryReadError('字段布局不一致')
        with self.assertRaises(MemoryReadError):
            self.client.call('snapshot')
        self.client._reader.snapshot.assert_called_once()


if __name__ == '__main__':
    unittest.main()
