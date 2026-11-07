"""Orphan recovery never claims a live owner or replays unfinished commands."""
import os
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import (
    MainThreadDispatcher, NativeActionError, Invocation, compile_mailbox,
    build_trampoline, load_profile, OWNER_MAGIC, OWNER_OFFSET, HEADER_SIZE,
    IDLE, QUEUED, RUNNING, DONE, CANCELLED, GUARD_FAILED, EXCEPTION, REJECTED, INVALID,
)


class DispatcherRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.entry, self.mailbox, self.image = 0x700000, 0x800000, 0x900000
        self.reader = Mock()
        self.reader.module = {'base': 0x180000000}
        self.reader.classes = {'Inventory': 0xa00000}
        self.reader.memory.modules = {'user32.dll': {'base': 0x100000, 'size': 0x10000}}
        self.profile = load_profile()
        self.header = bytearray(compile_mailbox(self.mailbox, self.reader.module['base'], self.profile,
            self.image, 0x100500, 1234, [Invocation('TownPortalSystem.frj', 1)], []))[:HEADER_SIZE]
        struct.pack_into('<I', self.header, 0, DONE)
        struct.pack_into('<IQ8s', self.header, OWNER_OFFSET, 4321, 999, OWNER_MAGIC)
        self.code = build_trampoline(self.mailbox)
        self.reader.memory.read.side_effect = self.read
        self.reader.memory.u64.side_effect = lambda address: self.image if address == 0xa00000 else self.entry
        self.dispatcher = MainThreadDispatcher(self.reader, profile=self.profile)
        self.dispatcher.thread_id = 1234
        self.dispatcher.iat = 0xb00000
        self.dispatcher._write = Mock()

    def read(self, address, size):
        return {self.entry: self.code, self.mailbox: bytes(self.header)}[address][:size]

    def resident(self):
        return self.dispatcher._resident_dispatcher(self.entry)

    def test_exact_code_and_complete_build_context_required(self):
        self.assertEqual(self.resident()['owner_pid'], 4321)
        for position, value in ((48, 5555), (52, 1000), (56, 1000), (88, 1),
                                (136, 1), (152, 1), (160, 1), (168, 2), (172, 0), (176, 0)):
            original = bytes(self.header)
            with self.subTest(position=position):
                struct.pack_into('<I' if position in (48, 52, 56, 168, 172) else '<Q', self.header, position, value)
                self.assertIsNone(self.resident())
            self.header[:] = original
        self.code = self.code[:-1] + b'\x90'
        self.assertIsNone(self.resident())
        self.dispatcher._write.assert_not_called()

    def test_legacy_and_unknown_hooks_cannot_be_automatically_claimed(self):
        self.header[184:192] = b'\0' * 8
        self.assertIsNone(self.resident())
        # Expected memory-read failures remain a refusal rather than an overwrite.
        from deskrawl_assistant.native_memory import MemoryReadError
        self.reader.memory.read.side_effect = MemoryReadError('unreadable')
        self.assertIsNone(self.resident())
        self.dispatcher._write.assert_not_called()

    def test_live_owner_and_access_denied_are_never_recovered(self):
        resident = self.resident()
        for value in (999, NativeActionError('cannot query owner')):
            with self.subTest(value=value), patch('deskrawl_assistant.native_dispatcher._process_start_time') as owner:
                if isinstance(value, Exception):
                    owner.side_effect = value
                else:
                    owner.return_value = value
                self.dispatcher._iat_write = Mock()
                with self.assertRaises(NativeActionError):
                    self.dispatcher._recover_resident(resident)
                self.dispatcher._iat_write.assert_not_called()
                self.dispatcher._write.assert_not_called()

    def test_incomplete_or_uncertain_commands_cannot_be_replayed(self):
        for state, complete, submitted in ((QUEUED, 6, 7), (RUNNING, 6, 7), (EXCEPTION, 7, 7),
                                           (REJECTED, 7, 7), (INVALID, 7, 7), (DONE, 6, 7), (IDLE, 6, 7)):
            with self.subTest(state=state), patch('deskrawl_assistant.native_dispatcher._process_start_time', return_value=None):
                struct.pack_into('<I', self.header, 0, state)
                struct.pack_into('<QQ', self.header, 8, complete, submitted)
                self.dispatcher._iat_write = Mock()
                with self.assertRaises(NativeActionError) as caught:
                    self.dispatcher._recover_resident(self.resident())
                self.assertTrue(caught.exception.uncertain)
                self.dispatcher._iat_write.assert_not_called()
                self.dispatcher._write.assert_not_called()

    def test_dead_owner_or_reused_pid_restores_original_without_freeing_or_invoking(self):
        for created in (None, 1000):
            for state in (IDLE, DONE, CANCELLED, GUARD_FAILED):
                with self.subTest(created=created, state=state), \
                        patch('deskrawl_assistant.native_dispatcher._process_start_time', return_value=created), \
                        patch('deskrawl_assistant.native_dispatcher.record') as log:
                    struct.pack_into('<I', self.header, 0, state)
                    self.dispatcher._write.reset_mock()
                    self.dispatcher._iat_write = Mock(side_effect=lambda value, before_write: before_write())
                    self.dispatcher._recover_resident(self.resident())
                    self.dispatcher._iat_write.assert_called_once()
                    self.assertEqual(self.dispatcher._iat_write.call_args.args, (0x100500,))
                    self.assertEqual(self.dispatcher._write.call_count, 2)
                    self.assertEqual(self.dispatcher._write.call_args_list[0].args, (self.mailbox + 168, b'\0' * 4))
                    self.assertEqual(self.dispatcher._write.call_args_list[1].args, (self.mailbox + 4, struct.pack('<I', 1)))
                    self.assertEqual((self.dispatcher.code, self.dispatcher.mailbox), (0, 0))
                    self.assertFalse(self.dispatcher.installed)
                    log.assert_called_once()

    def test_changed_code_mailbox_or_owner_during_restore_causes_no_writes(self):
        for change in ('code', 'header', 'live_owner'):
            original_code, original_header = self.code, bytes(self.header)
            resident = self.resident()
            self.dispatcher._write.reset_mock()
            def changed(value, before_write):
                if change == 'code':
                    self.code = self.code[:-1] + b'\x90'
                elif change == 'header':
                    struct.pack_into('<I', self.header, 0, QUEUED)
                before_write()
            self.dispatcher._iat_write = Mock(side_effect=changed)
            with self.subTest(change=change), patch('deskrawl_assistant.native_dispatcher._process_start_time',
                    side_effect=[None, 999] if change == 'live_owner' else [None]):
                with self.assertRaises(NativeActionError):
                    self.dispatcher._recover_resident(resident)
                self.dispatcher._write.assert_not_called()
            self.code, self.header[:] = original_code, original_header

    def test_each_invocation_preserves_owner_metadata(self):
        self.dispatcher.install = Mock()
        self.dispatcher.mailbox, self.dispatcher.code = self.mailbox, self.entry
        self.reader.check_game_files = Mock()
        struct.pack_into('<QQ', self.header, 8, 1, 1)
        self.dispatcher.invoke([Invocation('TownPortalSystem.frj', 1)])
        for call in self.dispatcher._write.call_args_list:
            address, raw = call.args
            self.assertTrue(address + len(raw) <= self.mailbox + OWNER_OFFSET or address >= self.mailbox + HEADER_SIZE)

    def install_environment(self, entry):
        self.current_entry = entry
        self.reader.memory.modules['unityplayer.dll'] = {'base': 0x200000}
        self.reader.memory.u64.side_effect = lambda address: (
            self.image if address == self.reader.classes['Inventory'] else
            1 if address == self.mailbox + 72 else self.current_entry)
        self.dispatcher.verify = Mock()
        self.dispatcher.K = Mock()
        self.dispatcher.K.VirtualAllocEx.side_effect = [self.mailbox, self.entry]
        self.dispatcher._api = Mock()
        self.dispatcher._protect = Mock()
        self.dispatcher._iat_write = Mock()

    def test_install_records_owner_before_publishing_hook(self):
        self.install_environment(0x100500)
        with patch('deskrawl_assistant.native_dispatcher._process_start_time', return_value=987654321):
            self.dispatcher.install()
        address, raw = self.dispatcher._write.call_args_list[0].args
        self.assertEqual(address, self.mailbox)
        self.assertEqual(struct.unpack_from('<IQ8s', raw, OWNER_OFFSET), (os.getpid(), 987654321, OWNER_MAGIC))
        self.assertTrue(self.dispatcher.installed)
        self.dispatcher._iat_write.assert_called_once_with(self.entry)

    def test_install_does_not_publish_if_own_identity_is_unavailable(self):
        self.install_environment(0x100500)
        with patch('deskrawl_assistant.native_dispatcher._process_start_time', return_value=None):
            with self.assertRaisesRegex(NativeActionError, '无法记录'):
                self.dispatcher.install()
        self.dispatcher._iat_write.assert_not_called()
        self.dispatcher._write.assert_not_called()
        self.assertFalse(self.dispatcher.installed)

    def test_install_recovers_only_dead_owned_hook_before_replacing(self):
        self.install_environment(self.entry)
        def publish(value, **options):
            options.get('before_write', lambda: None)()
            self.current_entry = value
        self.dispatcher._iat_write.side_effect = publish
        with patch('deskrawl_assistant.native_dispatcher._process_start_time', side_effect=[None, None, 987654321]), \
                patch('deskrawl_assistant.native_dispatcher.record'):
            self.dispatcher.install()
        self.assertEqual([call.args[0] for call in self.dispatcher._iat_write.call_args_list], [0x100500, self.entry])
        self.assertTrue(self.dispatcher.installed)

    def test_install_never_overwrites_live_owned_hook(self):
        self.install_environment(self.entry)
        with patch('deskrawl_assistant.native_dispatcher._process_start_time', return_value=999):
            with self.assertRaisesRegex(NativeActionError, '仍由另一个助手'):
                self.dispatcher.install()
        self.dispatcher.K.VirtualAllocEx.assert_not_called()
        self.dispatcher._iat_write.assert_not_called()
        self.dispatcher._write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
