"""Pumpkin settlement replays conserve drops, quota and the ordinary route."""
from contextlib import ExitStack
from copy import deepcopy
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import GameActions, _Read
from deskrawl_assistant.native_memory import GameBusyError, MemoryReadError, SnapshotChangedError
from tests import test_world_boss as boss_fixture
from tests.test_world_boss import options, PUMPKIN
from tests.test_managed_mode import after_rewards, verified, equipment, loot
from tests.test_farming_cycles import scene
import test_managed_service as service_fixture
from test_progression import state as growth_state


def settled():
    engine, world = boss_fixture.BossManagedTests().engine(remaining=5)
    world['scene']['run'].update(id='boss-win', status='success')
    world['scene']['completion_visible'] = {'CombatMapCompletionUI': True}
    return engine, world


class PumpkinReplayTests(unittest.TestCase):
    def test_replay_waits_for_new_run_and_counts_each_win_only_once(self):
        engine, world = settled()
        action = after_rewards(engine, world)
        self.assertEqual((action['kind'], action['reason_code'], action['source_run_id']),
                         ('enter_dungeon', 'pumpkin_replay', 'boss-win'))
        verified(engine, action)
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.pumpkin_completed, 1)
        world['scene']['run'].update(id='next-run', status='running')
        world['scene']['completion_visible'] = {'CombatMapCompletionUI': False}
        self.assertIsNone(engine.next_action(world))
        world['scene']['run'].update(status='success')
        world['scene']['completion_visible'] = {'CombatMapCompletionUI': True}
        self.assertEqual(after_rewards(engine, world)['reason_code'], 'pumpkin_replay')
        self.assertEqual((engine.pumpkin_completed, engine.repeat_completed, engine.repeat_index), (2, 0, 0))

    def test_delayed_boss_gold_equipment_and_chest_are_received_before_replay(self):
        engine, world = settled()
        world['scene']['run'].update(status='settling', completion_confirmed=True, finished=False)
        world['scene']['completion_visible'] = {}
        with patch('deskrawl_assistant.combat_rewards.time.monotonic', return_value=1):
            self.assertIsNone(engine.next_action(world))
        rows = [loot('box', 'chest', 'WorldBossPumpkinChest', container='ground'),
                equipment('boss-gear', container='ground'),
                loot('coins', 'currency', 'GoldDrop', container='ground', item_type=1)]
        world['scene']['run'].update(status='success', finished=True)
        world['scene']['completion_visible'] = {'CombatMapCompletionUI': True}
        for expected in ('coins', 'boss-gear', 'box'):
            world['ground']['items'] = deepcopy(rows)
            action = engine.next_action(world)
            self.assertEqual((action['kind'], action['item']['selection_id']), ('collect_ground', expected))
            verified(engine, action)
            row = next(r for r in rows if r['selection_id'] == expected)
            rows.remove(row)
            if expected != 'coins':
                world['items'].append(dict(row, container='inventory'))
                world['counts']['inventory']['occupied'] += 1
        world['ground']['items'] = []
        self.assertEqual(after_rewards(engine, world)['reason_code'], 'pumpkin_replay')

    def test_return_triggers_still_override_replay(self):
        for trigger in ('quota', 'chests', 'slots', 'merchant', 'stop', 'event_closed'):
            with self.subTest(trigger=trigger):
                engine, world = settled()
                if trigger == 'quota': world['scene']['maps_by_difficulty'] = options(0)
                elif trigger == 'chests': world['items'] = [loot('boxes', 'chest', 'TreasureChest3', count=20)]
                elif trigger == 'slots': world['counts']['inventory']['occupied'] = 37
                elif trigger == 'merchant':
                    engine.settings['merchant']['enabled'] = True
                    world['progression'] = growth_state()
                    world['progression']['busy'] = False
                    world['progression']['soul_shards'].update(current=100, maximum=100)
                elif trigger == 'stop': engine.finishing = True
                else: world['scene']['maps_by_difficulty'] = options(5, world_boss={'available':True,'remaining':5,'open':False})
                self.assertEqual(after_rewards(engine, world)['kind'], 'return_town')

    def test_unknown_quota_incomplete_ground_and_missing_popup_do_not_replay(self):
        for condition in ('quota', 'ground', 'popup'):
            with self.subTest(condition=condition):
                engine, world = settled()
                if condition == 'quota': world['scene']['maps_by_difficulty'] = {}
                elif condition == 'ground': world['ground']['complete'] = False
                else: world['scene']['completion_visible'] = {}
                self.assertIsNone(after_rewards(engine, world))
                self.assertTrue(engine.running)

    def test_full_bag_does_not_leave_boss_equipment_behind(self):
        engine, world = settled()
        world['counts']['inventory']['occupied'] = 40
        world['ground']['items'] = [equipment('unreceived', container='ground')]
        self.assertIsNone(engine.next_action(world))
        self.assertEqual(engine.phase, 'paused')
        self.assertIn('未切图', engine.status)


class PumpkinReplayServiceTests(unittest.TestCase):
    setUp = service_fixture.ManagedServiceTests.setUp

    def test_fresh_replay_rechecks_returns_late_drops_and_source_run(self):
        for change in ('none', 'quota', 'slots', 'chests', 'merchant', 'late_drop', 'source'):
            with self.subTest(change=change):
                engine, world = settled()
                action = after_rewards(engine, world)
                self.service.managed = engine
                if change == 'quota': world['scene']['maps_by_difficulty'] = options(0)
                elif change == 'slots': world['counts']['inventory']['occupied'] = 40
                elif change == 'chests': world['items'] = [loot('boxes', 'chest', 'TreasureChest3', count=20)]
                elif change == 'merchant':
                    engine.settings['merchant']['enabled'] = True
                    world['progression'] = growth_state()
                    world['progression']['soul_shards'].update(current=100, maximum=100)
                elif change == 'late_drop': world['ground']['items'] = [equipment('late', container='ground')]
                elif change == 'source': world['scene']['run'].update(id='native-next', status='running')
                self.client.actions.clear()
                with patch.object(self.service, '_managed_observation', return_value=world):
                    result = self.service._managed_execute(action, world)
                if change == 'none':
                    self.assertEqual(self.client.actions[0][0], 'enter_map')
                    self.assertTrue(result['verified'])
                else:
                    self.assertTrue(result['retryable'])
                    self.assertEqual(self.client.actions, [])


class PumpkinReplayNativeTests(unittest.TestCase):
    def test_live_dialog_lookup_rejects_stale_ambiguous_and_changing_instances(self):
        for case in ('single', 'multicast', 'old_map', 'ambiguous', 'missing', 'layout', 'changed'):
            with self.subTest(case=case):
                reader = Mock(); reader.module = {'base': 0}
                game = GameActions(reader, dispatcher=Mock())
                root = game.profile['statics']['completion_events']
                pointers = {root:1000, 1184:2000, 2424:3000, 3000:4000, 4088:5000,
                            5088:6000, 3120:7000 if case in ('multicast','ambiguous') else 0,
                            3032:8000 if case != 'missing' else 0, 8000:9000,
                            8208:99 if case == 'old_map' else 100, 8224:0,
                            3100:4000, 3132:8100, 8100:9000}
                memory = {a:struct.pack('<Q', v) for a,v in pointers.items()}
                memory[8200] = b'\1'
                reader.memory.read.side_effect = lambda a,n: memory[a]
                reader.class_name.side_effect = {4000:'Action`2', 5000:'MulticastDelegate',
                    6000:'Delegate', 9000:'CombatMapCompletionUI'}.__getitem__
                reader.fields.side_effect = lambda k: [{'name':'delegates','offset':119 if case == 'layout' else 120}] if k == 5000 else [{'name':'m_target','offset':32}]
                read = _Read(reader, game.profile)
                read.klass = Mock(side_effect=lambda address,name: address)
                read.verify = Mock(side_effect=lambda address,name: address)
                read.pointers = Mock(return_value=[3000,3100] if case == 'ambiguous' else [3000])
                if case in ('single', 'multicast', 'changed'):
                    self.assertEqual(game._pumpkin_replay_ui(read, 100), 8000)
                    read.finish()
                    if case == 'changed':
                        memory[8224] = struct.pack('<Q', 77)
                        with self.assertRaises(SnapshotChangedError): read.finish()
                else:
                    with self.assertRaises((GameBusyError, MemoryReadError, SnapshotChangedError)):
                        game._pumpkin_replay_ui(read, 100)
                game.dispatcher.invoke.assert_not_called()

    def test_replay_uses_original_replay_and_native_material_gates(self):
        read, reader, dispatcher = Mock(), Mock(), Mock()
        read.guards = []; read.boolean.return_value = False
        read.singleton.return_value = 3
        dispatcher.invoke.return_value = {}
        game = GameActions(reader, dispatcher=dispatcher, allow_unverified=True)
        control = {'available':True, 'difficulty':'Normal', 'map_id':PUMPKIN, 'maps':options()['Normal']}
        with ExitStack() as stack:
            for name, value in (('capabilities', {'compatible':True}), ('_context', (read, 1, 2, 0)),
                                ('control_snapshot', control), ('_map_objects', {PUMPKIN:1000})):
                stack.enter_context(patch.object(game, name, return_value=value))
            empty = stack.enter_context(patch.object(game, '_require_empty_town'))
            guard = stack.enter_context(patch.object(game, '_require_pumpkin_replay'))
            dialog = stack.enter_context(patch.object(game, '_pumpkin_replay_ui', return_value=2000))
            stack.enter_context(patch.object(game, '_require_travel_ready'))
            stack.enter_context(patch('deskrawl_assistant.world_boss.read_entry', return_value={'can_enter':True}))
            game.execute('enter_map', {'map_id':PUMPKIN, 'difficulty':'Normal', 'reason_code':'pumpkin_replay', 'source_run_id':'win'})
        empty.assert_not_called(); guard.assert_called_once()
        dialog.assert_called_once_with(read, 1000)
        calls = dispatcher.invoke.call_args.args[0]
        self.assertEqual([c.method for c in calls], ['GameManager.travel_busy', 'TownPortalSystem.entry_materials', 'CombatMapCompletionUI.replay'])
        self.assertEqual(calls[-1].instance, 2000)
        self.assertTrue(calls[0].require_false); self.assertTrue(calls[1].require_true)

    def test_replay_guard_rejects_native_repeat_race_late_drop_and_unsettled_run(self):
        for change in ('none', 'plan', 'late_drop', 'unfinished', 'popup', 'map', 'dead'):
            with self.subTest(change=change), ExitStack() as stack:
                read, copy = Mock(), Mock()
                read.guards = []; copy.guards = [('source-plan', b'guard')]
                read.boolean.return_value = False
                game = GameActions(Mock(), dispatcher=Mock(), allow_unverified=True)
                state = {'map_id':PUMPKIN, 'difficulty':'Normal', 'run':{'id':'win', 'status':'success'}}
                raw = {'id':'win', 'finished':True, 'enemy_count':0}
                if change == 'plan': raw['id'] = 'native-next'
                elif change == 'unfinished': raw['finished'] = False
                elif change == 'map': state['map_id'] = 'other'
                elif change == 'dead': state['dead'] = True
                stack.enter_context(patch('deskrawl_assistant.recommendation_reader._Copy', return_value=copy))
                stack.enter_context(patch('deskrawl_assistant.recommendation_reader._campaign_run', return_value=raw))
                stack.enter_context(patch.object(game, '_completion_state', return_value={'CombatMapCompletionUI': change != 'popup'}))
                stack.enter_context(patch.object(game, '_ground', return_value=[{}] if change == 'late_drop' else []))
                payload = {'map_id':PUMPKIN, 'source_run_id':'win'}
                if change == 'none':
                    game._require_pumpkin_replay(read, 1, 0, payload, state)
                    self.assertIn(('source-plan', b'guard'), read.guards)
                else:
                    with self.assertRaises((GameBusyError, SnapshotChangedError)):
                        game._require_pumpkin_replay(read, 1, 0, payload, state)
                game.dispatcher.invoke.assert_not_called()
