"""Settlement recovery without native calls, spending, or real-time sleeps."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import normalise_run
from deskrawl_assistant.managed_mode import ManagedMode, run_entry_confirmed
from deskrawl_assistant.mythic import MYTHIC, floor_id
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
