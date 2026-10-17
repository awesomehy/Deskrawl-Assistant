"""Optional black-mist reveal reads remain independent of inventory safety."""
import struct
import unittest
from unittest.mock import Mock

from deskrawl_assistant.native_memory import MemoryReadError
from deskrawl_assistant.native_reader import NativeReader


class BlackMistRevealReadTests(unittest.TestCase):
    def reader(self):
        reader = object.__new__(NativeReader)
        reader.memory = Mock(); reader.snapshot_stamps = []
        reader.class_name = Mock(return_value='BlackMistRevealState')
        reader.fields = Mock(return_value=[{'name': 'Slots', 'offset': 16}, {'name': 'Finalized', 'offset': 24}])
        return reader

    def header(self, pointer=1000, black=True):
        raw = bytearray(128); raw[105] = int(black)
        struct.pack_into('<Q', raw, 112, pointer)
        return raw

    def test_nonblack_item_never_reads_optional_object(self):
        reader = self.reader()
        self.assertEqual(reader._black_mist_state(self.header(black=False)),
                         {'black_mist_revealed': None, 'black_mist_finalized': None})
        reader.memory.read.assert_not_called()

    def test_absent_reveal_object_blocks_equip_without_hiding_item(self):
        reader = self.reader()
        self.assertEqual(reader._black_mist_state(self.header(0)),
                         {'black_mist_revealed': False, 'black_mist_finalized': False})
        reader.memory.read.assert_not_called()

    def test_object_presence_alone_is_not_finalization(self):
        for finalized in (False, True):
            reader = self.reader(); raw = bytearray(32)
            struct.pack_into('<Q', raw, 0, 2000); raw[24] = int(finalized)
            reader.memory.read.return_value = bytes(raw)
            result = reader._black_mist_state(self.header())
            self.assertIs(result['black_mist_revealed'], True)
            self.assertIs(result['black_mist_finalized'], finalized)
            self.assertEqual(reader.snapshot_stamps, [(1000, bytes(raw))])

    def test_unreadable_changed_wrong_type_and_invalid_boolean_fail_closed(self):
        for scenario in ('read_error', 'changed', 'wrong_type', 'invalid_boolean'):
            with self.subTest(scenario=scenario):
                reader = self.reader(); raw = bytearray(32); struct.pack_into('<Q', raw, 0, 2000)
                raw[24] = 2 if scenario == 'invalid_boolean' else 1
                if scenario == 'read_error':
                    reader.memory.read.side_effect = MemoryReadError('unreadable')
                elif scenario == 'changed':
                    reader.memory.read.side_effect = [bytes(raw), bytes(32)]
                else:
                    reader.memory.read.return_value = bytes(raw)
                if scenario == 'wrong_type':
                    reader.class_name.return_value = 'OtherObject'
                self.assertEqual(reader._black_mist_state(self.header()),
                                 {'black_mist_revealed': None, 'black_mist_finalized': None})
                self.assertEqual(reader.snapshot_stamps, [])


if __name__ == '__main__':
    unittest.main()
