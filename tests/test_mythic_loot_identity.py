"""Real loot reader layouts feed stargazing, without process access or actions."""
from copy import deepcopy
import struct
import unittest

from deskrawl_assistant.loot_plan_reader import read_loot_plan
from deskrawl_assistant.mythic import MYTHIC, floor_id
from deskrawl_assistant.stargazing import DEFAULT_STARGAZING, decide
from tests.test_loot_plan_reader import Fixture


def mythic_fixture(floor=20, difficulty='Inferno1'):
    f = Fixture()
    native = struct.unpack('<Q', f.memory.read(f.map + 16, 8))[0]
    f.memory.write_fixture(native + 56, b'MythicDungeon\0' + bytes(18))
    f.text(f.plan + 32, 'MythicDungeon')
    f.text(f.plan + 56, difficulty)
    f.integer(f.map + 32, 1)
    f.integer(f.plan + 72, floor)
    f.integer(f.wave_manager + 304, floor)
    return f


class MythicLootIdentityTests(unittest.TestCase):
    def world(self, preview):
        preview['session'] = 'synthetic-session'
        return {'session': 'synthetic-session', 'scene': {
            'dungeon_id': floor_id(20), 'difficulty': MYTHIC,
            'world_difficulty': 'Inferno', 'run': {'id': preview['run']['id']}}}

    def test_rift_reader_plan_keeps_selected_reward_and_preserves_raw_capture(self):
        f = mythic_fixture()
        f.set_waves([[f.item('GemRuby1')]])
        raw = {}
        preview = read_loot_plan(f.reader, raw_sink=raw)
        self.assertTrue(preview['available'], preview['reason'])
        self.assertEqual(preview['run']['map_id'], floor_id(20))
        self.assertEqual(preview['run']['difficulty'], MYTHIC)
        self.assertEqual(preview['run']['world_difficulty'], 'Inferno')
        self.assertEqual(preview['run']['mythic_floor'], 20)
        self.assertEqual(raw['mapId'], 'MythicDungeon')
        self.assertEqual(raw['difficulty'], 'Inferno1')
        self.assertEqual(raw['mythicTier'], 20)
        self.assertEqual(decide(preview, self.world(preview), DEFAULT_STARGAZING)['verdict'], 'keep')

    def test_rift_complete_empty_plan_can_skip(self):
        preview = read_loot_plan(mythic_fixture().reader)
        self.assertEqual(decide(preview, self.world(preview), DEFAULT_STARGAZING)['verdict'], 'skip')

    def test_different_run_floor_world_difficulty_or_session_never_authorizes_skip(self):
        preview = read_loot_plan(mythic_fixture().reader)
        for key, value in [('run', {'id': 'other-run'}), ('dungeon_id', floor_id(19)),
                           ('world_difficulty', 'Nightmare'), ('world_difficulty', None)]:
            with self.subTest(key=key, value=value):
                world = self.world(preview)
                world['scene'][key] = value
                self.assertEqual(decide(preview, world, DEFAULT_STARGAZING)['verdict'], 'unknown')
        world = self.world(preview)
        world['session'] = 'other-session'
        self.assertEqual(decide(preview, world, DEFAULT_STARGAZING)['verdict'], 'unknown')
        broken = deepcopy(preview)
        broken['run']['mythic_floor'] = 19
        self.assertEqual(decide(broken, self.world(preview), DEFAULT_STARGAZING)['verdict'], 'unknown')

    def test_mismatched_or_invalid_native_floor_clears_plan_and_capture(self):
        for floor in (19, 0, 101):
            with self.subTest(floor=floor):
                f = mythic_fixture(floor)
                f.integer(f.wave_manager + 304, 20 if floor == 19 else floor)
                raw = {'previous': 'must disappear'}
                preview = read_loot_plan(f.reader, raw_sink=raw)
                self.assertFalse(preview['available'])
                self.assertIsNone(preview['run'])
                self.assertEqual(raw, {})

    def test_floor_changes_while_copying_never_leaks_old_plan(self):
        f = mythic_fixture()
        def mutate(address, size):
            if address == f.wave_manager + 304:
                floor = struct.unpack('<i', bytes(f.memory.data.get(address + i, 0) for i in range(4)))[0]
                f.integer(address, 21 if floor == 20 else 20)
        f.memory.after_read = mutate
        preview = read_loot_plan(f.reader)
        self.assertFalse(preview['available'])
        self.assertEqual(preview['waves'], [])

    def test_normal_map_keeps_existing_identity(self):
        preview = read_loot_plan(Fixture().reader)
        self.assertEqual(preview['run']['map_id'], 'RedForest2')
        self.assertEqual(preview['run']['difficulty'], 'Normal')
        self.assertEqual(preview['run']['world_difficulty'], 'Normal')
        self.assertIsNone(preview['run']['mythic_floor'])
