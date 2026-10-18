"""Count short native wins while the synthetic executor awaits other work."""
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.native_dispatcher import NativeActionError, REJECTED
from tests import test_stargazing_service as stargazing_tests


class RepeatServiceRegressionTests(unittest.TestCase):
    def setUp(self):
        # Reuse the fake client and thread-free service fixture without
        # inheriting its existing tests or starting any native/game process.
        stargazing_tests.StargazingServiceTests.setUp(self)
        self.run_changes = {}
        self.scene_changes = {}
        self.incomplete_inventory = False
        control = self.client.control_snapshot

        def current_control():
            value = control()
            value.update(self.scene_changes)
            if value['run']:
                value['run'].update(self.run_changes)
            return value

        self.client.control_snapshot = current_control
        observe = self.service._managed_observation

        def current_observation(*args, **kwargs):
            value = observe(*args, **kwargs)
            if self.incomplete_inventory:
                value['complete'] = False
            return value

        self.service._managed_observation = current_observation

    def tick(self, now):
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now):
            return self.service._managed_once()

    def confirm_win(self):
        self.run_changes.update(status='settling', completion_confirmed=True)

    def begin_next_run(self, dungeon='offline-map'):
        self.run_id = 'native-next-run'
        self.client.current_map = dungeon
        self.run_changes.clear()

    def quota_for_first_map(self):
        engine = self.service.managed
        return engine.repeat_completed if engine.repeat_index == 0 else engine.repeat_counts.get(0, 0)

    def request_combat_entry(self):
        self.client.native_handler = Mock(return_value={'transition_requested': True})
        self.tick(100)
        self.assertEqual(self.client.actions[-1][0], 'enter_map')
        self.assertIsNotNone(self.service.managed_transition)

    def test_win_after_combat_entry_submission_is_counted_before_arrival(self):
        self.request_combat_entry()
        self.confirm_win()
        self.tick(101)
        self.tick(102)
        self.assertEqual(self.quota_for_first_map(), 1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'], 0)
        self.assertEqual(len(self.client.actions), 1)

        self.begin_next_run('other-map')
        self.tick(103)
        self.assertEqual(self.quota_for_first_map(), 1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'], 0)
        self.assertNotIn('offline-run', self.service.managed.stargazing_skipped)

    def test_unfinished_combat_entry_still_does_not_count_a_win(self):
        self.request_combat_entry()
        self.tick(101)
        self.assertEqual(self.quota_for_first_map(), 0)
        self.begin_next_run('other-map')
        self.tick(102)
        self.assertEqual(self.quota_for_first_map(), 0)
        self.assertEqual(self.service.managed.repeat_completed, 0)
        self.assertEqual(self.service.managed.stargazing_state['skipped'], 1)

    def test_entry_first_observed_as_success_confirms_arrival_and_counts_new_map(self):
        self.request_combat_entry()
        self.begin_next_run('other-map')
        self.run_changes.update(status='success', completion_confirmed=True,
            completion_counter={'id': 'other-map-counter', 'count': 1})
        self.tick(101)
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual(self.service.managed.target_dungeon, 'other-map')
        self.tick(102)
        self.assertEqual(self.service.managed.repeat_completed, 1)
        self.assertEqual(self.quota_for_first_map(), 0)

    def test_delayed_skip_arrival_does_not_overwrite_both_maps_completed_quotas(self):
        engine = self.service.managed
        for target in engine.repeat_maps:
            target['runs'] = 1
        self.run_changes['completion_counter'] = {'id': 'source-counter', 'count': 0}
        self.request_combat_entry()
        # The source wins after entry was accepted and already fills its quota.
        self.confirm_win()
        self.run_changes['completion_counter']['count'] = 1
        self.tick(101)
        self.assertEqual((engine.target_dungeon, engine.repeat_completed), ('other-map', 0))
        # The first destination observation is also a confirmed win. It fills
        # the second quota before the outstanding entry is marked complete.
        self.begin_next_run('other-map')
        self.run_changes.update(status='success', completion_confirmed=True,
            completion_counter={'id': 'destination-counter', 'count': 1})
        self.tick(102)
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual((engine.target_dungeon, engine.repeat_completed), ('offline-map', 0))
        engine.observe_runs(self.service._managed_observation())
        self.assertEqual((engine.target_dungeon, engine.repeat_completed), ('offline-map', 0))

    def assert_retry_does_not_lose_win(self, gate):
        self.client.native_handler = Mock(side_effect=NativeActionError('明确拒绝', state=REJECTED))
        self.tick(100)
        self.assertIsNotNone(self.service.managed_retry)
        self.confirm_win()
        if gate == 'complete':
            self.incomplete_inventory = True
        else:
            self.scene_changes[gate] = True
        self.tick(100.1)
        self.tick(100.2)
        self.begin_next_run()
        self.scene_changes.clear()
        self.incomplete_inventory = False
        self.tick(100.3)
        self.assertEqual(self.quota_for_first_map(), 1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'], 0)
        self.assertEqual(len(self.client.actions), 1)

    def test_retry_backoff_scene_change_does_not_lose_win(self):
        self.assert_retry_does_not_lose_win('scene_changing')

    def test_retry_backoff_teleport_does_not_lose_win(self):
        self.assert_retry_does_not_lose_win('teleporting')

    def test_retry_backoff_incomplete_inventory_does_not_lose_win(self):
        self.assert_retry_does_not_lose_win('complete')

    def test_pending_return_teleport_does_not_lose_final_wave_win(self):
        self.service.managed.stargazing_return = {
            'source_run_id': self.run_id, 'next_repeat_index': 1,
        }
        self.client.native_handler = Mock(return_value={'transition_requested': True})
        self.tick(100)
        self.assertEqual(self.client.actions[-1][0], 'return_town')

        self.confirm_win()
        self.scene_changes['teleporting'] = True
        self.tick(101)
        self.assertEqual(self.quota_for_first_map(), 1)

        self.client.in_town = True
        self.client.current_map = None
        self.scene_changes.clear()
        self.tick(102)
        self.assertEqual(self.quota_for_first_map(), 1)
        self.assertEqual(self.service.managed.stargazing_state['skipped'], 0)
        self.assertEqual(len(self.client.actions), 1)


if __name__ == '__main__':
    unittest.main()
