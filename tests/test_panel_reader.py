import struct
import unittest
from types import SimpleNamespace

from deskrawl_assistant.game_actions import _Read
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.panel_reader import panel_active_self


class PanelReaderTests(unittest.TestCase):
    def fixture(self, *, active=False, managed_flag=True):
        base, panel, component, game_object = 0x100000, 0x200000, 0x300000, 0x400000
        data = {
            base + 0xBA520: bytes.fromhex('40534883ec20488b5120488d4c2430'),
            base + 0xBCFB0: bytes.fromhex('0fb64146c3'),
            panel + 16: struct.pack('<Q', component),
            panel + 40: bytes([managed_flag]),
            component: struct.pack('<Q', base + 0x100),
            component + 32: struct.pack('<Q', game_object),
            game_object: struct.pack('<Q', base + 0x200),
            game_object + 70: bytes([active]),
        }
        memory = SimpleNamespace(
            modules={'unityplayer.dll': {'base': base, 'size': 0x100000}},
            read=lambda address, size: data[address][:size])
        read = _Read(SimpleNamespace(memory=memory), {})
        return read, panel, data

    def test_already_hidden_panel_with_stale_managed_flag_is_closed(self):
        read, panel, _ = self.fixture(active=False, managed_flag=True)
        self.assertIs(panel_active_self(read, panel), False)
        read.finish()
        self.assertNotIn(panel + 40, [address for address, _ in read.guards])

    def test_visible_panel_with_unset_managed_flag_is_open(self):
        read, panel, _ = self.fixture(active=True, managed_flag=False)
        self.assertIs(panel_active_self(read, panel), True)
        read.finish()

    def test_changed_engine_accessor_never_uses_unverified_offsets(self):
        for address in (0x1BA520, 0x1BCFB0):
            read, panel, data = self.fixture()
            data[address] = bytes(len(data[address]))
            with self.assertRaisesRegex(MemoryReadError, '需要维护适配'):
                panel_active_self(read, panel)
            self.assertNotIn(panel + 16, [address for address, _ in read.guards])

    def test_missing_or_foreign_native_objects_fail_closed(self):
        for address, pointer in ((0x200010, 0), (0x300000, 0),
                                 (0x300020, 0), (0x400000, 0x500000)):
            read, panel, data = self.fixture()
            data[address] = struct.pack('<Q', pointer)
            with self.assertRaises(MemoryReadError):
                panel_active_self(read, panel)

    def test_unknown_boolean_and_unloaded_engine_are_not_treated_as_closed(self):
        read, panel, data = self.fixture()
        data[0x400046] = b'\x02'
        with self.assertRaises(MemoryReadError):
            panel_active_self(read, panel)
        read.reader.memory.modules.clear()
        with self.assertRaises(MemoryReadError):
            panel_active_self(read, panel)

    def test_visibility_and_pointer_changes_remain_in_action_guards(self):
        for address, replacement in ((0x400046, b'\x01'),
                                     (0x300020, struct.pack('<Q', 0x500000))):
            read, panel, data = self.fixture()
            self.assertFalse(panel_active_self(read, panel))
            data[address] = replacement
            with self.assertRaises(SnapshotChangedError):
                read.finish()


if __name__ == '__main__':
    unittest.main()
