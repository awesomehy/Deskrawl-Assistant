"""Cold IL2CPP completion types must not be interpreted as Class pointers.

The in-memory fixtures exercise the real native Class/FieldInfo reader. They
never open a game process, install a dispatcher, or invoke a game function.
"""
import copy
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read
from deskrawl_assistant.native_dispatcher import load_profile
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError, GameBusyError
from deskrawl_assistant.native_reader import NativeReader


BASE = 0x180000000
TOKENS = {
    # Deskrawl 1.1.0 / Steam 26310087: copied from each TypeInfo slot in
    # GameAssembly before IL2CPP resolves the encoded type reference.
    'CombatMapCompletionUI': 0x200096A5,
    'MythicCompletionUI': 0x2000CB69,
}
VISIBILITY_OFFSETS = {'CombatMapCompletionUI': 0, 'MythicCompletionUI': 8}


class ByteMemory:
    def __init__(self):
        self.bytes = {}
        self.reads = []
        self.after_read = None
        self.next_address = 0x100000

    def put(self, address, raw):
        self.bytes.update((address + i, byte) for i, byte in enumerate(raw))

    def allocate(self, size):
        address = self.next_address
        self.next_address += ((max(size, 1) + 255) // 256) * 256
        self.put(address, bytes(size))
        return address

    def pointer(self, address, value):
        self.put(address, struct.pack('<Q', value))

    def text(self, value):
        raw = value.encode('utf-8') + b'\0'
        address = self.allocate(len(raw))
        self.put(address, raw)
        return address

    def read(self, address, size):
        self.reads.append((address, size))
        try:
            raw = bytes(self.bytes[address + i] for i in range(size))
        except KeyError as exc:
            raise MemoryReadError(f'fixture address is unreadable: {address:#x}') from exc
        if self.after_read:
            self.after_read(address, size, raw)
        return raw

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def cstring(self, address, max_length=256):
        raw = bytearray()
        for offset in range(max_length):
            value = self.read(address + offset, 1)[0]
            if not value:
                return raw.decode('utf-8')
            raw.append(value)
        raise MemoryReadError('fixture string is unterminated')


class CompletionFixture:
    def __init__(self):
        self.profile = copy.deepcopy(load_profile())
        self.profile['uninitialized_type_tokens'].update(TOKENS)
        self.memory = ByteMemory()
        # Bypass construction, which is the only step that opens a process.
        self.reader = NativeReader.__new__(NativeReader)
        self.reader.memory = self.memory
        self.reader.module = {'base': BASE}
        self.reader.field_cache = {}
        self.reader.check_game_files = Mock()
        self.dispatcher = Mock()
        self.dispatcher.installed = False
        with patch('deskrawl_assistant.game_actions.load_profile', return_value=self.profile):
            self.game = GameActions(self.reader, dispatcher=self.dispatcher)
        self.classes = {}
        self.tables = {}
        self.statics = {}
        self.objects = {}
        for name, token in TOKENS.items():
            self.memory.pointer(self.slot(name), token)

    def slot(self, name):
        return BASE + self.profile['statics'][name]

    def read(self):
        return _Read(self.reader, self.profile)

    def make_class(self, name):
        if name in self.classes:
            return self.classes[name]
        klass = self.memory.allocate(0x140)
        self.classes[name] = klass
        self.memory.pointer(klass + 16, self.memory.text(name))
        fields = self.profile['types'].get(name, {}).get('fields', [])
        table = self.memory.allocate(32 * len(fields))
        self.tables[name] = table
        self.memory.pointer(klass + 128, table)
        self.memory.put(klass + 0x124, struct.pack('<H', len(fields)))
        for index, field in enumerate(fields):
            raw = struct.pack('<QQQiI', self.memory.text(field['name']), 0, klass,
                              field['rawRegistrationOffset'], int(field['token'], 16))
            self.memory.put(table + 32 * index, raw)
        return klass

    def load_completion(self, name, visible=False, *, null_static=False):
        klass = self.make_class(name)
        static = 0 if null_static else self.memory.allocate(16)
        self.statics[name] = static
        self.memory.pointer(klass + 184, static)
        if static:
            self.memory.put(static + VISIBILITY_OFFSETS[name], bytes((visible,)))
        self.memory.pointer(self.slot(name), klass)
        return klass

    def object(self, name, size=320):
        obj = self.memory.allocate(size)
        self.memory.pointer(obj, self.make_class(name))
        self.objects[name] = obj
        return obj

    def singleton(self, name):
        obj = self.object(name)
        klass = self.classes[name]
        static = self.memory.allocate(8)
        self.memory.pointer(static, obj)
        self.memory.pointer(klass + 184, static)
        self.memory.pointer(BASE + self.profile['singletons'][name], klass)
        return obj

    def context(self, *, dead=False):
        manager = self.singleton('GameManager')
        area = self.singleton('GameAreaManager')
        self.singleton('SaveSystem')
        portal = self.singleton('TownPortalSystem')
        combat = self.object('PlayerCombatController')
        player = self.object('Player')
        self.memory.put(manager + 176, struct.pack('<i', 1))
        self.memory.put(area + 120, struct.pack('<i', 0))
        self.memory.pointer(manager + 64, combat)
        self.memory.pointer(combat + 32, player)
        self.memory.put(player + 296, bytes((dead,)))
        if dead: self.singleton('DeathUI')
        return manager, area, portal


class CompletionTypeInitializationTests(unittest.TestCase):
    def test_release_profile_records_the_two_verified_exact_tokens(self):
        self.assertEqual({key:load_profile()['uninitialized_type_tokens'][key] for key in TOKENS}, TOKENS)

    def test_exact_cold_tokens_are_bound_to_the_revalidated_1_1_0_build(self):
        profile = load_profile()
        self.assertEqual(profile['build'], 'Deskrawl 1.1.0 / Steam 26310087')
        self.assertEqual(profile['sha256']['GameAssembly.dll'],
                         '72c9bd3a42790516c18c4728a25d6eff481bf8232288a2bfe60f550f7bfdbe26')
        self.assertEqual({name:profile['statics'][name] for name in TOKENS},
                         {'CombatMapCompletionUI': 61589520, 'MythicCompletionUI': 61589512})

    def test_both_cold_tokens_are_absent_without_being_dereferenced(self):
        fixture = CompletionFixture()
        read = fixture.read()
        self.assertEqual(fixture.game._completion_state(read),
                         {name: False for name in TOKENS})
        read.finish()
        self.assertEqual(set(fixture.memory.reads),
                         {(fixture.slot(name), 8) for name in TOKENS})
        self.assertEqual(set(read.guards),
                         {(fixture.slot(name), struct.pack('<Q', token))
                          for name, token in TOKENS.items()})
        fixture.dispatcher.invoke.assert_not_called()

    def test_each_loaded_ui_is_checked_while_the_other_type_is_cold(self):
        for name in TOKENS:
            with self.subTest(name=name):
                fixture = CompletionFixture()
                klass = fixture.load_completion(name, True)
                read = fixture.read()
                result = fixture.game._completion_state(read)
                read.finish()
                self.assertEqual(result, {key: key == name for key in TOKENS})
                self.assertIn(klass, fixture.reader.field_cache)
                self.assertIn((klass + 0x124, 2), fixture.memory.reads)

    def test_loaded_visibility_flags_keep_their_distinct_static_offsets(self):
        for combat, mythic in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(combat=combat, mythic=mythic):
                fixture = CompletionFixture()
                fixture.load_completion('CombatMapCompletionUI', combat)
                fixture.load_completion('MythicCompletionUI', mythic)
                read = fixture.read()
                result = fixture.game._completion_state(read)
                read.finish()
                self.assertEqual(result, {'CombatMapCompletionUI': combat,
                                          'MythicCompletionUI': mythic})

    def test_loaded_class_without_static_storage_is_not_visible(self):
        fixture = CompletionFixture()
        for name in TOKENS:
            fixture.load_completion(name, null_static=True)
        read = fixture.read()
        self.assertEqual(fixture.game._completion_state(read),
                         {name: False for name in TOKENS})
        read.finish()
        self.assertEqual(len(fixture.reader.field_cache), 2)

    def test_unknown_encoded_tokens_and_null_class_remain_errors(self):
        # The previous build's exact tokens must also fail closed.
        for value in (0, 0x20009581, 0x2000CA09, 0x10000001, 0x20009589, 0x2000CA01):
            with self.subTest(value=hex(value)):
                fixture = CompletionFixture()
                fixture.memory.pointer(fixture.slot('CombatMapCompletionUI'), value)
                with self.assertRaises(MemoryReadError) as raised:
                    fixture.game._completion_state(fixture.read())
                self.assertNotIsInstance(raised.exception, SnapshotChangedError)

    def test_token_for_the_other_class_is_not_treated_as_cold(self):
        for name in TOKENS:
            with self.subTest(name=name):
                fixture = CompletionFixture()
                other = next(key for key in TOKENS if key != name)
                fixture.memory.pointer(fixture.slot(name), TOKENS[other])
                with self.assertRaises(MemoryReadError):
                    fixture.game._completion_state(fixture.read())

    def test_missing_profile_token_never_turns_unknown_pointer_into_false(self):
        fixture = CompletionFixture()
        fixture.profile.pop('uninitialized_type_tokens')
        # A missing build-profile key is fail-closed just like an invalid
        # pointer; control_snapshot reports either exception as unavailable.
        with self.assertRaises((MemoryReadError, KeyError)):
            fixture.game._completion_state(fixture.read())

    def test_resolved_class_with_wrong_type_is_rejected(self):
        fixture = CompletionFixture()
        klass = fixture.load_completion('CombatMapCompletionUI', True)
        fixture.memory.pointer(klass + 16, fixture.memory.text('UnrelatedUI'))
        with self.assertRaisesRegex(MemoryReadError, '字段布局'):
            fixture.game._completion_state(fixture.read())

    def test_resolved_field_offset_or_token_mismatch_is_rejected(self):
        for offset, raw in ((24, struct.pack('<i', 999)),
                            (28, struct.pack('<I', 0x0400FFFF))):
            with self.subTest(offset=offset):
                fixture = CompletionFixture()
                fixture.load_completion('CombatMapCompletionUI', True)
                fixture.memory.put(fixture.tables['CombatMapCompletionUI'] + offset, raw)
                with self.assertRaisesRegex(MemoryReadError, '字段布局'):
                    fixture.game._completion_state(fixture.read())

    def test_unreadable_resolved_static_storage_is_rejected(self):
        fixture = CompletionFixture()
        klass = fixture.load_completion('CombatMapCompletionUI', True)
        fixture.memory.pointer(klass + 184, 0xDEADBEEF)
        with self.assertRaises(MemoryReadError):
            fixture.game._completion_state(fixture.read())

    def test_invalid_loaded_visibility_boolean_is_rejected(self):
        for name in TOKENS:
            with self.subTest(name=name):
                fixture = CompletionFixture()
                fixture.load_completion(name, 2)
                with self.assertRaisesRegex(MemoryReadError, '布尔值'):
                    fixture.game._completion_state(fixture.read())

    def test_cold_token_resolving_during_copy_invalidates_the_snapshot(self):
        fixture = CompletionFixture()
        name = 'CombatMapCompletionUI'
        klass = fixture.load_completion(name, True)
        fixture.memory.pointer(fixture.slot(name), TOKENS[name])
        def initialise_after_read(address, size, _raw):
            if address == fixture.slot(name):
                fixture.memory.after_read = None
                fixture.memory.pointer(address, klass)
        fixture.memory.after_read = initialise_after_read
        read = fixture.read()
        self.assertFalse(fixture.game._completion_state(read)[name])
        with self.assertRaises(SnapshotChangedError):
            read.finish()
        retry = fixture.read()
        self.assertTrue(fixture.game._completion_state(retry)[name])
        retry.finish()

    def test_loaded_flag_changing_during_copy_invalidates_the_snapshot(self):
        fixture = CompletionFixture()
        name = 'MythicCompletionUI'
        fixture.load_completion(name, False)
        flag = fixture.statics[name] + VISIBILITY_OFFSETS[name]
        def show_after_read(address, size, _raw):
            if address == flag:
                fixture.memory.after_read = None
                fixture.memory.put(flag, b'\1')
        fixture.memory.after_read = show_after_read
        read = fixture.read()
        self.assertFalse(fixture.game._completion_state(read)[name])
        with self.assertRaises(SnapshotChangedError):
            read.finish()

    def test_living_player_with_cold_completion_types_keeps_channelled_return(self):
        fixture = CompletionFixture()
        manager, _, portal = fixture.context()
        read = fixture.read()
        call = fixture.game._return_town_call(read, manager, portal)
        read.finish()
        self.assertEqual(call.method, 'TownPortalSystem.frk')
        self.assertEqual(call.instance, portal)
        self.assertTrue(call.require_true)
        fixture.dispatcher.invoke.assert_not_called()

    def test_loaded_completion_and_death_keep_the_accepted_return_button(self):
        for name, dead in (('CombatMapCompletionUI', False),
                           ('MythicCompletionUI', False), (None, True)):
            with self.subTest(name=name, dead=dead):
                fixture = CompletionFixture()
                manager, _, portal = fixture.context(dead=dead)
                if name:
                    fixture.load_completion(name, True)
                read = fixture.read()
                call = fixture.game._return_town_call(read, manager, portal)
                read.finish()
                self.assertEqual(call.method, 'DeathUI.return_to_town' if dead else 'TownPortalSystem.frl')
                self.assertEqual(call.require_true, not dead)
                if dead: self.assertEqual(call.instance,fixture.objects['DeathUI'])
                fixture.dispatcher.invoke.assert_not_called()

    def test_cold_death_ui_waits_without_dereferencing_encoded_type(self):
        fixture=CompletionFixture();manager,_,portal=fixture.context(dead=True)
        address=BASE+fixture.profile['singletons']['DeathUI']
        token=fixture.profile['uninitialized_type_tokens']['DeathUI']
        fixture.memory.pointer(address,token)
        with self.assertRaises(GameBusyError):
            fixture.game._return_town_call(fixture.read(),manager,portal)
        self.assertFalse(any(token<=a<token+0x140 for a,_ in fixture.memory.reads))
        fixture.dispatcher.invoke.assert_not_called()

    def test_death_ui_static_or_instance_not_loaded_waits_instead_of_stopping(self):
        for null_static in (True,False):
            with self.subTest(null_static=null_static):
                fixture=CompletionFixture();manager,_,portal=fixture.context(dead=True)
                klass=fixture.classes['DeathUI'];static=fixture.memory.u64(klass+184)
                fixture.memory.pointer(klass+184 if null_static else static,0)
                with self.assertRaises(GameBusyError):
                    fixture.game._return_town_call(fixture.read(),manager,portal)
                fixture.dispatcher.invoke.assert_not_called()

    def test_auto_revive_submitted_during_plan_invalidates_death_return(self):
        fixture=CompletionFixture();manager,_,portal=fixture.context(dead=True)
        address=fixture.objects['DeathUI']+73
        def changed(a,size,raw):
            if a==address: fixture.memory.put(address,b'\1')
        fixture.memory.after_read=changed
        read=fixture.read();call=fixture.game._return_town_call(read,manager,portal)
        self.assertEqual(call.method,'DeathUI.return_to_town')
        with self.assertRaises(SnapshotChangedError):read.finish()
        fixture.dispatcher.invoke.assert_not_called()

    def test_complete_control_snapshot_remains_available_before_ui_initialization(self):
        fixture = CompletionFixture()
        fixture.context()
        recommendation = {'available': True, 'run': None, 'profile': {
            'character_id': 'fixture-character', 'difficulty': 'Normal',
            'unlocked_maps': {'Normal': []}, 'completed_maps': {'Normal': []},
            'entry_items': {},
            'mythic_min_difficulty':'Inferno','mythic_unlocked':[],'mythic_completed':[],'is_demo':False,
        }}
        difficulty = ('Normal', [{'id': 'Normal', 'value': 0, 'unlocked': True}])
        with patch.object(fixture.game, 'capabilities', return_value={'compatible': True}), \
                patch.object(fixture.game, '_difficulty_state', return_value=difficulty), \
                patch.object(fixture.game, '_map_objects', return_value={}), \
                patch.object(fixture.game, '_ground', return_value=[]), \
                patch.object(fixture.game, '_carriage', return_value={
                    'available': True, 'complete': True, 'items': []}), \
                patch('deskrawl_assistant.recommendation_reader._catalog', return_value={'maps': []}), \
                patch('deskrawl_assistant.game_loot_ledger.read_loot_ledger', return_value={'available': True}), \
                patch('deskrawl_assistant.game_panel_log.read_panel_log', return_value={'available': True}), \
                patch('deskrawl_assistant.statistics_reader.read_gold_ledger', return_value={'available': False}):
            result = fixture.game.control_snapshot(recommendation)
        self.assertTrue(result['available'], result['reason'])
        self.assertEqual(result['scene'], 'combat')
        self.assertEqual(result['completion_visible'], {name: False for name in TOKENS})
        self.assertTrue(result['ground_complete'])
        fixture.dispatcher.invoke.assert_not_called()


if __name__ == '__main__':
    unittest.main()
