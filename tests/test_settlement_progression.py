"""Settlement recovery without native calls, spending, or real-time sleeps."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import normalise_run
from deskrawl_assistant.managed_mode import ManagedMode, run_entry_confirmed
from deskrawl_assistant.mythic import MYTHIC, floor_id
from deskrawl_assistant.world_boss import PUMPKIN
from tests.test_managed_mode import settings, observation, verified, after_rewards, equipment
from tests.test_run_receipts import campaign
from tests import test_run_receipts as receipt_tests
from tests import test_managed_service as service_tests


def finished_plan():
    return dict(id='current-plan', map_id='a', difficulty='Normal', finished=False,
                final_kill_settled=True, wave_index=10, wave_count=10,
                wave_active=False, enemy_count=0)


def popup(mythic=False):
    return {'CombatMapCompletionUI': not mythic, 'MythicCompletionUI': mythic}


class SettlementRunTests(unittest.TestCase):
    def test_dialog_finishes_a_win_even_if_auxiliary_enemy_or_wave_data_disagrees(self):
        for change in ({}, {'enemy_count': 1}, {'wave_index': 9}, {'wave_count': 11}):
            with self.subTest(change=change):
                run = {**finished_plan(), **change}
                old = normalise_run(run, {'a'})
                self.assertNotEqual(old['status'], 'success')
                fixed = normalise_run(run, {'a'}, completion_visible=popup())
                self.assertEqual(fixed['status'], 'success')
                self.assertTrue(fixed['completion_confirmed'])
                self.assertIn('completion_UI', fixed['completion_evidence'])

    def test_dialog_and_full_final_wave_can_confirm_cleared_transient_flags(self):
        run = {**finished_plan(), 'final_kill_settled': False}
        self.assertEqual(normalise_run(run, {'a'})['status'], 'running')
        self.assertEqual(normalise_run(run, {'a'}, completion_visible=popup())['status'], 'success')

    def test_dialog_never_completes_a_new_active_or_unidentified_plan(self):
        for change in ({'wave_active': True}, {'id': None}, {'retained_terminal': True},
                       {'final_kill_settled': False, 'wave_index': 1},
                       {'final_kill_settled': False, 'enemy_count': 1}):
            with self.subTest(change=change):
                result = normalise_run({**finished_plan(), **change}, {'a'}, completion_visible=popup())
                self.assertNotEqual(result['status'], 'success')
        self.assertNotEqual(normalise_run(finished_plan(), set(), completion_visible=popup())['status'], 'success')
        self.assertEqual(normalise_run(finished_plan(), {'a'}, dead=True,
                                      completion_visible=popup())['status'], 'failed')

    def test_only_corresponding_dialog_can_confirm_normal_or_mythic_run(self):
        run = finished_plan()
        self.assertEqual(normalise_run(run, {'a'}, completion_visible=popup(True))['status'], 'settling')
        run.update(map_id=floor_id(20), difficulty=MYTHIC)
        self.assertEqual(normalise_run(run, {floor_id(20)}, completion_visible=popup())['status'], 'settling')
        self.assertEqual(normalise_run(run, {floor_id(20)}, completion_visible=popup(True))['status'], 'success')

    def test_push_settlement_collects_rewards_then_enters_the_next_map(self):
        cfg = settings()
        cfg['farming']['mode'] = 'push'
        engine = ManagedMode(cfg)
        world = observation()
        world['scene']['difficulty'] = 'Normal'
        engine.start(world)
        world['scene'].update(completion_visible=popup(), run=normalise_run(
            {**finished_plan(), 'enemy_count': 1}, {'a'}, completion_visible=popup()))
        world['ground']['items'] = [equipment('boss-drop', container='ground')]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground')
        self.assertEqual(engine.target_dungeon, 'b')
        verified(engine, action)
        world['ground']['items'] = []
        world['items'] = [equipment('boss-drop')]
        action = after_rewards(engine, world)
        self.assertEqual(action['kind'], 'return_town')
        verified(engine, action)
        town = observation([], town=True)
        town['scene'].update(difficulty='Normal', run=world['scene']['run'])
        action = engine.next_action(town)
        self.assertEqual(action['kind'], 'enter_dungeon')
        self.assertEqual(action['dungeon_id'], 'b')
        self.assertEqual(action['source_run_id'], 'current-plan')
        verified(engine, action)
        world['scene'].update(dungeon_id='b', completion_visible={},
            run={'id': 'next-plan', 'status': 'running', 'difficulty': 'Normal'})
        self.assertIsNone(engine.next_action(world))
        self.assertTrue(engine.running, engine.status)


class NativeSettlementTests(unittest.TestCase):
    # The existing byte-backed fixture verifies actual UI class/flag reads.
    # Only the campaign fixture is synthetic; no dispatcher can be installed.
    read = receipt_tests.NativeRunReceiptBufferTests.read

    def setUp(self):
        receipt_tests.NativeRunReceiptBufferTests.setUp(self)
        self.stack.enter_context(patch('deskrawl_assistant.recommendation_reader.resolved_profile',
                                      return_value={'types': []}))
        active = self.fixture.object('MapData')
        self.fixture.memory.pointer(self.fixture.objects['GameManager'] + 112, active)
        self.fixture.reader.asset_name = Mock(return_value='map-a')

    def settlement(self, value=None):
        value = deepcopy(value or campaign())
        value['run'].update(finished=False, final_kill_settled=True, enemy_count=1)
        self.fixture.load_completion('CombatMapCompletionUI', visible=True)
        return value

    def test_dialog_fallback_is_bound_to_current_plan_and_preserved_as_one_receipt(self):
        value = self.settlement()
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', return_value=value['run']) as current:
            first = self.read(value)
            second = self.read(value)
        self.assertTrue(first['available'], first['reason'])
        self.assertEqual(first['run']['status'], 'success')
        self.assertEqual(first['run_history']['sequence'], 1)
        self.assertEqual(len(first['run_history']['receipts']), 1)
        self.assertEqual(first['run_history']['receipts'][0]['run']['status'], 'success')
        self.assertEqual(first['run_history'], second['run_history'])
        self.assertEqual(current.call_count, 2)

    def test_changed_plan_or_map_during_dialog_read_cannot_create_win(self):
        value = self.settlement()
        for change in ({'id': 'new-plan'}, {'map_id': 'other-map'}, {'difficulty': 'Inferno'}):
            with self.subTest(change=change), patch(
                    'deskrawl_assistant.recommendation_reader._campaign_run',
                    return_value={**value['run'], **change}):
                result = self.read(value)
                self.assertFalse(result['available'])
                self.assertTrue(result['retryable'])
                self.assertEqual(self.game._run_receipts.snapshot()['receipts'], [])

    def test_dialog_flag_changing_after_read_invalidates_fallback_receipt(self):
        value = self.settlement()
        def race(copy, *_):
            self.fixture.memory.put(self.fixture.statics['CombatMapCompletionUI'], b'\0')
            return value['run']
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', side_effect=race):
            result = self.read(value)
        self.assertFalse(result['available'])
        self.assertTrue(result['retryable'])
        self.assertEqual(self.game._run_receipts.snapshot()['receipts'], [])

    def test_current_plan_guards_are_checked_before_publishing_dialog_receipt(self):
        value = self.settlement()
        address = self.fixture.objects['GameManager'] + 104
        def race(copy, *_):
            copy.read(address, 8)
            self.fixture.memory.pointer(address, 1234)
            return value['run']
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', side_effect=race):
            result = self.read(value)
        self.assertFalse(result['available'])
        self.assertTrue(result['retryable'])
        self.assertEqual(self.game._run_receipts.snapshot()['receipts'], [])


class ClearedWorldBossSettlementTests(unittest.TestCase):
    setUp = NativeSettlementTests.setUp
    settlement = NativeSettlementTests.settlement
    read = NativeSettlementTests.read

    def boss_settlement(self):
        value = self.settlement()
        run = {**value['run'], 'map_id': PUMPKIN, 'finished': True,
               'final_kill_settled': False, 'enemy_count': 0}
        value['run'] = None
        value['profile']['completed_maps']['Normal'] = [PUMPKIN]
        self.fixture.reader.asset_name.return_value = PUMPKIN
        return value, run

    def test_cleared_manager_plan_uses_guarded_wave_plan_and_counts_only_once(self):
        value, run = self.boss_settlement()
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', return_value=run) as current:
            first = self.read(value)
            second = self.read(value)
        self.assertTrue(first['available'], first['reason'])
        self.assertEqual(first['run']['status'], 'success')
        self.assertEqual(first['run']['map_id'], PUMPKIN)
        self.assertEqual(first['run_history']['sequence'], 1)
        self.assertEqual(first['run_history'], second['run_history'])
        self.assertTrue(current.call_args.kwargs['allow_settled_world_boss'])

    def test_cleared_boss_plan_still_needs_completed_map_evidence(self):
        value, run = self.boss_settlement()
        value['profile']['completed_maps']['Normal'] = []
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', return_value=run):
            result = self.read(value)
        self.assertTrue(result['available'], result['reason'])
        self.assertNotEqual(result['run']['status'], 'success')
        self.assertEqual(result['run_history']['receipts'], [])

    def test_dead_character_cannot_turn_retained_boss_plan_into_a_win(self):
        value, run = self.boss_settlement()
        self.fixture.memory.put(self.fixture.objects['Player']+296, b'\1')
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', return_value=run):
            result = self.read(value)
        self.assertTrue(result['available'], result['reason'])
        self.assertEqual(result['run']['status'], 'failed')
        self.assertEqual(result['run_history']['receipts'], [])

    def test_cleared_plan_recovery_requires_correct_visible_popup_and_stable_combat_scene(self):
        for case in ('hidden', 'mythic_popup', 'town', 'teleporting', 'changing', 'ordinary_map'):
            with self.subTest(case=case):
                value, run = self.boss_settlement()
                self.fixture.load_completion('MythicCompletionUI', visible=False)
                mem, objs = self.fixture.memory, self.fixture.objects
                mem.put(objs['GameAreaManager']+120, b'\0\0\0\0')
                mem.put(objs['GameAreaManager']+176, b'\0')
                mem.put(objs['TownPortalSystem']+76, b'\0')
                if case in ('hidden', 'mythic_popup'):
                    mem.put(self.fixture.statics['CombatMapCompletionUI'], b'\0')
                    if case == 'mythic_popup':
                        mem.put(self.fixture.statics['MythicCompletionUI']+8, b'\1')
                elif case == 'town': mem.put(objs['GameAreaManager']+120, b'\1\0\0\0')
                elif case == 'teleporting': mem.put(objs['TownPortalSystem']+76, b'\1')
                elif case == 'changing': mem.put(objs['GameAreaManager']+176, b'\1')
                elif case == 'ordinary_map': self.fixture.reader.asset_name.return_value = 'map-a'
                with patch('deskrawl_assistant.recommendation_reader._campaign_run') as current:
                    result = self.read(value)
                current.assert_not_called()
                self.assertTrue(result['available'], result['reason'])
                self.assertIsNone(result['run'])
                self.assertEqual(result['run_history']['receipts'], [])

    def test_changed_wave_plan_or_popup_cannot_publish_a_win(self):
        value, run = self.boss_settlement()
        for changed in (None, {**run, 'map_id': 'map-a'}, {**run, 'difficulty': 'Inferno'}):
            with self.subTest(changed=changed), patch(
                    'deskrawl_assistant.recommendation_reader._campaign_run', return_value=changed):
                result = self.read(value)
                self.assertFalse(result['available'])
                self.assertTrue(result['retryable'])
                self.assertEqual(self.game._run_receipts.snapshot()['receipts'], [])
        def race(copy, *_, **__):
            self.fixture.memory.put(self.fixture.statics['CombatMapCompletionUI'], b'\0')
            return run
        with patch('deskrawl_assistant.recommendation_reader._campaign_run', side_effect=race):
            result = self.read(value)
        self.assertFalse(result['available'])
        self.assertTrue(result['retryable'])
        self.assertEqual(self.game._run_receipts.snapshot()['receipts'], [])

    def test_recovered_last_boss_collects_rewards_before_returning_to_original_map(self):
        from tests.test_campaign_repeat_counter import SettledWorldBossPlanTests
        from tests.test_world_boss import BossManagedTests, options
        copied = SettledWorldBossPlanTests().fixture()
        engine, world = BossManagedTests().engine()
        world['scene']['maps_by_difficulty'] = options(0)
        world['scene'].update(completion_visible=popup(), run=normalise_run(
            SettledWorldBossPlanTests().read(copied), {PUMPKIN}, completion_visible=popup()))
        world['ground']['items'] = [equipment('last-boss-drop', container='ground')]
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'collect_ground')
        verified(engine, action)
        world['ground']['items'] = []
        world['items'] = [equipment('last-boss-drop')]
        action = after_rewards(engine, world)
        self.assertEqual(action['kind'], 'return_town')
        verified(engine, action)
        town = observation([], town=True)
        town['scene']['difficulty'] = 'Normal'
        town['scene']['maps_by_difficulty'] = options(0)
        action = engine.next_action(town)
        self.assertEqual((action['kind'], action['dungeon_id']), ('enter_dungeon', 'a'))
        self.assertEqual(engine.pumpkin_completed, 1)
        self.assertEqual(engine.repeat_completed, 0)
        self.assertTrue(engine.running, engine.status)


class EntryConfirmationTests(unittest.TestCase):
    def test_old_settlement_is_not_new_arrival_but_fast_new_success_is(self):
        action = {'dungeon_id': 'a', 'difficulty': 'Normal', 'source_run_id': 'old-plan'}
        scene = {'available': True, 'in_town': False, 'in_dungeon': True,
                 'dungeon_id': 'a', 'difficulty': 'Normal', 'completion_visible': popup(),
                 'run': {'id': 'old-plan', 'status': 'success'}}
        self.assertFalse(run_entry_confirmed(action, scene))
        scene['run']['id'] = 'new-plan'
        self.assertTrue(run_entry_confirmed(action, scene))
        for change in ({'retained_terminal': True}, {'id': None}, {'status': 'failed'}):
            with self.subTest(change=change):
                self.assertFalse(run_entry_confirmed(action, {**scene, 'run': {**scene['run'], **change}}))
        for key in ('teleporting', 'scene_changing', 'dead'):
            self.assertFalse(run_entry_confirmed(action, {**scene, key: True}))


class OrdinaryEntryServiceTests(unittest.TestCase):
    def setUp(self):
        service_tests.ManagedServiceTests.setUp(self)
        self.client.items = []
        self.run = {'id': 'old-plan', 'status': 'success'}
        original = self.client.control_snapshot
        def control():
            value = original()
            value['run'] = {**self.run, 'dungeon_id': 'offline-map', 'difficulty': 'Normal'}
            return value
        self.client.control_snapshot = control
        cfg = service_tests.ManagedServiceTests.settings(self, farming=True)
        self.service.save_managed(cfg)
        self.service.set_managed_running(True)
        self.client.native_handler = Mock(return_value={'transition_requested': True})

    def tick(self, now):
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now):
            return self.service._managed_once()

    def test_same_map_old_settlement_does_not_confirm_normal_town_entry(self):
        self.tick(100)
        self.assertEqual(self.client.actions[-1][0], 'enter_map')
        self.client.in_town = False
        self.client.current_map = 'offline-map'
        self.tick(101)
        self.assertIsNotNone(self.service.managed_transition)
        self.assertEqual(len(self.client.actions), 1)
        self.run.update(id='new-plan', status='running')
        self.tick(102)
        self.assertIsNone(self.service.managed_transition)
        self.assertTrue(self.service.managed.running, self.service.managed.status)


if __name__ == '__main__':
    unittest.main()
