"""Confirmed native-repeat wins survive unrelated planner wait gates."""
from copy import deepcopy
import unittest

from deskrawl_assistant.managed_mode import ManagedMode
from tests.test_managed_mode import equipment, observation, settings


class RepeatCountRegressionTests(unittest.TestCase):
    def native_repeat(self):
        config = settings()
        config['farming']['repeat_maps'] = [
            {'dungeon_id': 'a', 'difficulty': 'Normal', 'runs': 300},
            {'dungeon_id': 'b', 'difficulty': 'Normal', 'runs': 300},
        ]
        world = observation()
        world['scene'].update(difficulty='Normal')
        world['scene']['run'].update(dungeon_id='a', difficulty='Normal')
        engine = ManagedMode(config)
        engine.start(world)
        return engine, world

    def assert_short_win_survives_wait(self, gate):
        engine, world = self.native_repeat()
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        if gate == 'complete':
            world['complete'] = False
        elif gate == 'await_effect':
            item = equipment('pending-lock')
            world['items'] = [deepcopy(item)]
            engine.await_effect = {'kind': 'lock', 'item': item}
        else:
            world['scene'][gate] = True

        # Only this observation contains the finished run. The game's own
        # repeat flow replaces its plan before the planner's wait is over.
        engine.next_action(world)
        for _ in range(2):
            engine.next_action(world)

        world['complete'] = True
        world['scene'].update(scene_changing=False, teleporting=False)
        if gate == 'await_effect':
            world['items'][0]['locked'] = True
        world['scene']['run'].update(id='native-next-run', status='running',
                                     completion_confirmed=False)
        for _ in range(3):
            engine.next_action(world)
        self.assertTrue(engine.running)
        self.assertEqual(engine.repeat_completed, 1)
        self.assertEqual(engine.repeat_index, 0)
        self.assertEqual(engine.target_dungeon, 'a')

    def test_incomplete_inventory_does_not_lose_confirmed_scene_win(self):
        self.assert_short_win_survives_wait('complete')

    def test_scene_change_wait_does_not_lose_confirmed_win(self):
        self.assert_short_win_survives_wait('scene_changing')

    def test_teleport_wait_does_not_lose_confirmed_win(self):
        self.assert_short_win_survives_wait('teleporting')

    def test_pending_item_effect_does_not_lose_confirmed_win(self):
        self.assert_short_win_survives_wait('await_effect')

    def assert_metadata_can_be_corrected(self, key, wrong):
        engine, world = self.native_repeat()
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        expected = world['scene']['run'][key]
        world['scene']['run'][key] = wrong
        engine.next_action(world)
        self.assertEqual(engine.repeat_completed, 0)

        world['scene']['run'][key] = expected
        for _ in range(3):
            engine.next_action(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_unmatched_map_does_not_consume_success_deduplication(self):
        self.assert_metadata_can_be_corrected('dungeon_id', 'unrelated-map')

    def test_unmatched_difficulty_does_not_consume_success_deduplication(self):
        self.assert_metadata_can_be_corrected('difficulty', 'Nightmare')

    def test_revived_run_can_win_after_failed_observation_with_same_id(self):
        engine, world = self.native_repeat()
        world['scene'].update(dead=True)
        world['scene']['run'].update(status='failed')
        action = engine.next_action(world)
        self.assertEqual(action['kind'], 'return_town')
        self.assertEqual(engine.repeat_completed, 0)

        # The death recovery gate rejected the portal before any submission.
        # Auto-revival keeps the run ID and permits this same run to complete.
        engine.pending = None
        world['scene'].update(dead=False)
        world['scene']['run'].update(status='running')
        engine.next_action(world)
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        for _ in range(3):
            engine.next_action(world)
        self.assertEqual(engine.repeat_completed, 1)
        self.assertEqual(engine.repeat_index, 0)

    def test_start_baselines_retained_win_instead_of_importing_a_previous_round(self):
        engine, world = self.native_repeat()
        engine.stop()
        world['character_id'] = 'fixture-character'
        world['scene'].update(in_town=True, in_dungeon=False)
        world['scene']['run'].update(status='success', completion_confirmed=True,
            retained_terminal=True, completion_counter={'id': 'native-counter', 'count': 10})
        world['run_history'] = {'session': world['session'], 'character_id': 'fixture-character',
                                'sequence': 10, 'receipts': []}
        engine.start(world)
        for _ in range(3):
            engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 0)

    def test_resume_does_not_count_a_win_observed_while_paused(self):
        engine, world = self.native_repeat()
        engine.pause()
        world['scene']['run'].update(status='success', completion_confirmed=True,
            retained_terminal=True, completion_counter={'id': 'native-counter', 'count': 1})
        engine.resume(world)
        for _ in range(3):
            engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 0)

    def test_fallback_win_then_first_counter_observation_is_counted_once(self):
        engine, world = self.native_repeat()
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        world['scene']['run']['completion_counter'] = {'id': 'native-counter', 'count': 1}
        for _ in range(3):
            engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_fallback_win_then_recovered_counter_delta_is_not_counted_twice(self):
        engine, world = self.native_repeat()
        world['scene']['run']['completion_counter'] = {'id': 'native-counter', 'count': 0}
        engine.observe_runs(world)
        world['scene']['run'].pop('completion_counter')
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        world['scene']['run']['completion_counter'] = {'id': 'native-counter', 'count': 1}
        for _ in range(3):
            engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)

    def test_same_native_counter_without_a_new_win_does_not_add_rounds(self):
        engine, world = self.native_repeat()
        world['scene']['run']['completion_counter'] = {'id': 'native-counter', 'count': 0}
        engine.observe_runs(world)
        world['scene']['run'].update(status='settling', completion_confirmed=True)
        world['scene']['run']['completion_counter']['count'] = 1
        engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)
        world['scene']['run'].update(id='native-next-run', status='running', completion_confirmed=False)
        for _ in range(3):
            engine.observe_runs(world)
        self.assertEqual(engine.repeat_completed, 1)


if __name__ == '__main__':
    unittest.main()
