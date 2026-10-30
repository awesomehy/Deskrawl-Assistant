"""Read-only loot previews reject mixed runs and never reuse stale plans."""
import json
import math
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError
from deskrawl_assistant.loot_plan_reader import (
    _LootCopy, _labels, _profile, describe_item, empty_preview, read_loot_plan,
)


class FakeMemory:
    def __init__(self):
        self.data = {}
        self.reads = []
        self.after_read = None

    def write_fixture(self, address, raw):
        self.data.update({address + index: value for index, value in enumerate(raw)})

    def read(self, address, size):
        self.reads.append((address, size))
        raw = bytes(self.data.get(address + index, 0) for index in range(size))
        if self.after_read:
            self.after_read(address, size)
        return raw


class Fixture:
    """Artificial objects with real verified layouts, no process or save data."""
    def __init__(self):
        self.memory = FakeMemory()
        self.next_address = 0x20000
        self.classes, self.names, self.fields = {}, {}, {}
        profile = _profile()
        for row in profile['types']:
            address = self.allocate(0x200)
            self.classes[row['name']] = address
            self.names[address] = row['name']
            self.fields[address] = [{'name': f['name'], 'offset': f['rawRegistrationOffset'],
                                      'token': int(f['token'], 16)} for f in row['fields']]
        string = self.allocate(0x200)
        self.classes['String'] = string
        self.names[string] = 'String'
        self.manager = self.object('GameManager', 320)
        self.area = self.object('GameAreaManager', 224)
        self.save = self.object('SaveSystem', 512)
        self.wave_manager = self.object('WaveManager', 336)
        self.map = self.object('MapData', 304)
        self.plan = self.object('RunPlan', 112)
        native = self.allocate(128)
        self.ptr(self.map + 16, native)
        self.memory.write_fixture(native + 56, b'RedForest2\0' + bytes(21))
        self.integer(self.manager + 176, 1)
        self.integer(self.area + 120, 0)
        self.integer(self.save + 32, 1)
        self.ptr(self.manager + 104, self.wave_manager)
        self.ptr(self.manager + 112, self.map)
        self.ptr(self.manager + 120, self.plan)
        self.ptr(self.wave_manager + 40, self.map)
        self.ptr(self.wave_manager + 88, self.plan)
        self.integer(self.wave_manager + 48, 1)
        self.boolean(self.wave_manager + 96, True)
        self.text(self.plan + 16, 'private-run-identity')
        self.text(self.plan + 32, 'RedForest2')
        self.text(self.plan + 56, 'Normal')
        self.reader = SimpleNamespace(memory=self.memory, profile=profile,
            resolved_profiles={'recommendation-types.json': {'types': []}},
            native_layout={'game_manager_rva': 0x1234, 'game_area_manager_rva': 0x5678},
            module={'base': 0x100000}, session_id='private-local-session',
            class_name=lambda address: self.names.get(address, 'invalid'),
            fields=lambda address: self.fields[address],
            singleton=lambda name: self.save if name == 'SaveSystem' else None,
            check_game_files=lambda: None)
        for rva, name, obj in ((0x1234, 'GameManager', self.manager),
                               (0x5678, 'GameAreaManager', self.area)):
            klass = self.classes[name]
            self.ptr(0x100000 + rva, klass)
            static = self.allocate(8)
            self.ptr(klass + 184, static)
            self.ptr(static, obj)
        self.set_waves([[], []])

    def allocate(self, size):
        address = self.next_address
        self.next_address += (size + 31) & ~7
        return address

    def ptr(self, address, value):
        self.memory.write_fixture(address, struct.pack('<Q', value))

    def integer(self, address, value):
        self.memory.write_fixture(address, struct.pack('<i', value))

    def boolean(self, address, value):
        self.memory.write_fixture(address, bytes([value]))

    def object(self, name, size):
        address = self.allocate(size)
        self.ptr(address, self.classes[name])
        return address

    def text(self, field, value):
        encoded = value.encode('utf-16-le')
        address = self.object('String', 24 + len(encoded))
        self.integer(address + 16, len(value))
        self.memory.write_fixture(address + 20, encoded)
        self.ptr(field, address)
        return address

    def array(self, name, pointers):
        klass = self.allocate(0x200)
        self.names[klass] = name + '[]'
        self.ptr(klass + 64, self.classes[name])
        self.integer(klass + 0x104, 8)
        address = self.allocate(32 + len(pointers) * 8)
        self.ptr(address, klass)
        self.ptr(address + 24, len(pointers))
        for index, pointer in enumerate(pointers):
            self.ptr(address + 32 + index * 8, pointer)
        return address

    def item(self, key='CommonBelt3', qty=0, rarity='Common', modifiers=()):
        obj = self.object('PlanItem', 88)
        self.text(obj + 16, 'private-drop-' + str(obj))
        self.text(obj + 24, key)
        self.text(obj + 32, 'Belt')
        self.text(obj + 40, rarity)
        self.integer(obj + 48, 48)
        self.boolean(obj + 52, True)
        self.boolean(obj + 53, False)
        self.integer(obj + 64, 2)
        self.integer(obj + 68, qty)
        self.integer(obj + 72, 1000 if key == 'TreasureChest1' else 0)
        pointers = []
        for stat, kind, value in modifiers:
            mod = self.object('PlanModifier', 48)
            self.text(mod + 16, stat)
            self.text(mod + 24, kind)
            self.memory.write_fixture(mod + 32, struct.pack('<f', value))
            pointers.append(mod)
        self.ptr(obj + 56, self.array('PlanModifier', pointers))
        return obj

    def set_waves(self, item_groups):
        self.wave_objects = []
        self.loot_objects = []
        for index, items in enumerate(item_groups, 1):
            wave = self.object('RunWave', 64)
            loot = self.object('WaveLoot', 40)
            self.integer(wave + 16, index)
            self.ptr(wave + 48, loot)
            self.integer(loot + 16, 100 * index)
            self.integer(loot + 20, index)
            self.ptr(loot + 24, self.array('PlanItem', items))
            self.wave_objects.append(wave)
            self.loot_objects.append(loot)
        self.waves_array = self.array('RunWave', self.wave_objects)
        self.ptr(self.plan + 104, self.waves_array)


class LootPlanReaderTests(unittest.TestCase):
    def test_active_whole_plan_and_exact_local_labels(self):
        f = Fixture()
        equipment = f.item(modifiers=[('CritChance', 'Flat', .05), ('Strength', 'PercentAdd', .15)])
        chest = f.item('TreasureChest1', qty=0)
        f.set_waves([[equipment], [chest]])
        result = read_loot_plan(f.reader)
        self.assertTrue(result['available'], result['reason'])
        self.assertEqual(result['phase'], 'active')
        self.assertEqual(result['totals'], {'items': 2, 'equipment': 1, 'gold': 300, 'potion_charges': 3})
        self.assertEqual(result['run']['map_id'], 'RedForest2')
        self.assertEqual(result['run']['current_wave'], 1)
        self.assertEqual([w['state'] for w in result['waves']], ['current', 'upcoming'])
        item = result['waves'][0]['items'][0]
        self.assertEqual((item['quantity'], item['raw_qty'], item['level'], item['sockets']), (1, 0, 48, 2))
        self.assertEqual(item['rarity_label'], '普通')
        self.assertTrue(item['ancient'])
        self.assertIn('暴击几率 +5%', item['modifiers'][0]['text'])
        self.assertIn('力量 +15%', item['modifiers'][1]['text'])
        self.assertEqual(result['waves'][1]['items'][0]['chest_gold'], 1000)
        serialized = json.dumps(result)
        self.assertNotIn('private-run', serialized)
        self.assertNotIn('private-drop', serialized)
        self.assertNotIn('private-local', serialized)

    def test_town_ignores_retained_previous_plan(self):
        f = Fixture()
        f.set_waves([[f.item()]])
        f.integer(f.area + 120, 1)
        result = read_loot_plan(f.reader)
        self.assertFalse(result['available'])
        self.assertEqual(result['phase'], 'waiting')
        self.assertEqual(result['waves'], [])
        self.assertIsNone(result['run'])
        self.assertNotIn((f.manager + 120, 8), f.memory.reads)

    def test_zero_items_is_a_valid_plan_and_gold_not_inferred(self):
        f = Fixture()
        result = read_loot_plan(f.reader)
        self.assertTrue(result['available'])
        self.assertEqual(result['totals']['items'], 0)
        self.assertEqual(result['totals']['gold'], 300)
        self.assertEqual(len(result['waves']), 2)

    def test_offline_and_pending_plan_clear_previous_preview(self):
        for field, value in (('mode', 0), ('enabled', 0), ('plan', 0)):
            f = Fixture()
            if field == 'mode':
                f.integer(f.save + 32, value)
            elif field == 'enabled':
                f.boolean(f.wave_manager + 96, value)
            else:
                f.ptr(f.manager + 120, value)
            result = read_loot_plan(f.reader)
            self.assertFalse(result['available'])
            self.assertEqual(result['phase'], 'waiting')
            self.assertEqual(result['waves'], [])

    def test_map_or_run_identity_mismatch_never_returns_mixed_loot(self):
        for target in ('plan', 'map', 'map_id'):
            f = Fixture()
            if target == 'plan':
                f.ptr(f.wave_manager + 88, f.plan + 8)
            elif target == 'map':
                f.ptr(f.wave_manager + 40, f.map + 8)
            else:
                f.text(f.plan + 32, 'OtherMap')
            result = read_loot_plan(f.reader)
            self.assertFalse(result['available'])
            self.assertEqual(result['waves'], [])
            self.assertIsNone(result['run'])

    def test_array_changes_during_every_copy_fail_closed_after_three_attempts(self):
        f = Fixture()
        def mutate(address, size):
            if address == f.waves_array + 32:
                # The copied first wave no longer agrees with the live array.
                current = struct.unpack('<Q', bytes(f.memory.data.get(address + i, 0) for i in range(8)))[0]
                f.ptr(address, f.wave_objects[1] if current == f.wave_objects[0] else f.wave_objects[0])
        f.memory.after_read = mutate
        result = read_loot_plan(f.reader)
        self.assertFalse(result['available'])
        self.assertEqual(result['waves'], [])

    def test_retry_recovers_from_one_transition_and_same_map_new_run_changes_key(self):
        f = Fixture()
        first = read_loot_plan(f.reader)
        f.text(f.plan + 16, 'different-private-run')
        second = read_loot_plan(f.reader)
        self.assertNotEqual(first['run']['key'], second['run']['key'])
        with patch('deskrawl_assistant.loot_plan_reader._snapshot',
                   side_effect=[SnapshotChangedError('changing'), second]) as snapshot:
            self.assertEqual(read_loot_plan(f.reader), second)
            self.assertEqual(snapshot.call_count, 2)

    def test_version_gate_rejects_before_read_and_unknown_fields_reject_all(self):
        f = Fixture()
        f.reader.profile = {**f.reader.profile, 'gameAssemblySha256': 'unknown'}
        result = read_loot_plan(f.reader)
        self.assertEqual(result['phase'], 'unsupported')
        self.assertEqual(f.memory.reads, [])
        f = Fixture()
        f.fields[f.classes['RunPlan']].append({'name': 'newField', 'offset': 96, 'token': 123})
        result = read_loot_plan(f.reader)
        self.assertFalse(result['available'])
        self.assertEqual(result['waves'], [])

    def test_unknown_item_rarity_and_affix_are_explicit_without_fabricated_values(self):
        f = Fixture()
        f.set_waves([[f.item('NewServerItem', qty=9, rarity='NewRarity',
                              modifiers=[('NewStat', 'NewType', .5)])]])
        result = read_loot_plan(f.reader)
        self.assertTrue(result['available'], result['reason'])
        item = result['waves'][0]['items'][0]
        self.assertEqual(item['kind'], 'unknown')
        self.assertIsNone(item['quantity'])
        self.assertIsNone(item['icon'])
        self.assertIn('未知物品', item['name'])
        self.assertIn('未知品质', item['rarity_label'])
        self.assertIn('未确认词条', item['modifiers'][0]['text'])
        self.assertGreater(len(result['warnings']), 1)

    def test_kind_specific_quantity_matches_verified_native_consumption(self):
        labels = _labels()
        material = 'SoulShard'
        f = Fixture()
        f.set_waves([[f.item(material, qty=9), f.item(material, qty=0),
                      f.item('GemRuby1', qty=9), f.item('TreasureChest1', qty=9),
                      f.item('HunterSetRune1_1', qty=9)]])
        result = read_loot_plan(f.reader)
        self.assertTrue(result['available'], result['reason'])
        self.assertEqual([i['quantity'] for i in result['waves'][0]['items']], [9, 1, 1, 1, 1])
        self.assertEqual(result['waves'][0]['items'][0]['name'], '灵魂碎片')
        self.assertEqual(result['totals']['items'], 5)
        self.assertEqual([i['kind'] for i in result['waves'][0]['items']][-3:], ['gem', 'chest', 'rune'])

    def test_nonfinite_modifier_and_excessive_array_clear_all(self):
        f = Fixture()
        f.set_waves([[f.item(modifiers=[('Strength', 'Flat', math.inf)])]])
        self.assertFalse(read_loot_plan(f.reader)['available'])
        f = Fixture()
        f.ptr(f.waves_array + 24, 513)
        result = read_loot_plan(f.reader)
        self.assertFalse(result['available'])
        self.assertEqual(result['waves'], [])

    def test_each_wave_phase_and_completed_clear_end(self):
        f = Fixture()
        f.integer(f.wave_manager + 48, 2)
        result = read_loot_plan(f.reader)
        self.assertEqual([w['state'] for w in result['waves']], ['completed', 'current'])
        f.boolean(f.wave_manager + 248, True)
        self.assertEqual([w['state'] for w in read_loot_plan(f.reader)['waves']], ['completed', 'completed'])
        f.integer(f.wave_manager + 48, 0)
        f.boolean(f.wave_manager + 248, False)
        self.assertEqual([w['state'] for w in read_loot_plan(f.reader)['waves']], ['upcoming', 'upcoming'])

    def test_changed_game_file_invalidates_without_memory_read(self):
        f = Fixture()
        f.reader.check_game_files = lambda: (_ for _ in ()).throw(MemoryReadError('changed'))
        result = read_loot_plan(f.reader)
        self.assertFalse(result['available'])
        self.assertEqual(f.memory.reads, [])


if __name__ == '__main__':
    unittest.main()
