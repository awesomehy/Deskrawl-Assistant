"""Death recovery regressions using only synthetic snapshots and native calls.

The shared fixture disables worker threads and stores configuration in a
temporary directory. Import the helper module without importing its TestCase
into this module's globals, so discovery does not run its entire suite twice.
"""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

import test_managed_service as helpers
from deskrawl_assistant import native_memory


class ManagedDeathRecoveryTests(unittest.TestCase):
    setUp = helpers.ManagedServiceTests.setUp
    settings = helpers.ManagedServiceTests.settings

    def start_dead(self, *, merchant=False):
        self.client.items = []
        self.client.in_town = False
        self.client.current_map = 'offline-map'
        self.client.run_status = 'failed'
        self.dead = True
        self.run_id = 'dead-run'
        self.control_unavailable = False
        self.inventory_incomplete = False
        self.saving = False
        self.teleporting = False
        self.death_busy = True
        self.busy_error_type = native_memory.DeathRecoveryBusyError
        control_snapshot = self.client.control_snapshot
        inventory_snapshot = self.client.snapshot

        def control():
            if self.control_unavailable:
                return {'available': False, 'reason': '角色、场景或掉落正在变化，等待下一轮',
                        'ground_complete': False, 'ground': [], 'maps': []}
            value = control_snapshot()
            value.update(dead=self.dead, online_saving=self.saving,
                         teleporting=self.teleporting)
            if value.get('run'):
                value['run']['id'] = self.run_id
            return value

        def inventory():
            value = inventory_snapshot()
            if self.inventory_incomplete:
                value['complete'] = False
            return value

        self.client.control_snapshot = control
        self.client.snapshot = inventory
        config = self.settings(farming=True, auto_equip=False)
        config['farming']['repeat_maps'] = [
            {'dungeon_id': 'offline-map', 'difficulty': 'Normal', 'runs': 100}]
        if merchant:
            import test_progression
            growth = test_progression.state()
            growth['merchant'].update(unlock_known=True, unlocked=True)
            self.client.progression_snapshot = Mock(side_effect=lambda **_kwargs: deepcopy(growth))
            self.client.capabilities['merchant'] = {'available': True, 'native_verified': True}
            config['merchant']['enabled'] = True
            config['merchant']['weights']['2'] = 1
        self.service._managed_refresh_control()
        self.service.save_managed(config)
        self.service.set_managed_running(True)
        self.service.managed.repeat_completed = 7
        self.native = Mock(return_value={'transition_requested': True})
        self.client.native_handler = self.native
        execute = self.service._managed_execute

        def preflight(action, observation):
            if action['kind'] == 'return_town' and self.death_busy:
                raise self.busy_error_type(
                    '死亡后的复活或回城正在处理，等待游戏完成。')
            return execute(action, observation)

        self.preflight = Mock(side_effect=preflight)
        self.service._managed_execute = self.preflight

    def tick(self, now):
        with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=now), \
                patch('deskrawl_assistant.managed_service.record'):
            self.service._work('managed', {})

    def assert_unsubmitted_wait(self):
        self.assertTrue(self.service.managed.running)
        self.assertFalse(self.service.managed_can_resume)
        self.assertIsNone(self.service.managed_transition)
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertIsNone(self.service.managed.pending)
        self.native.assert_not_called()
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.service.managed.repeat_completed, 7)

    def assert_long_read_wait(self, attribute):
        self.start_dead()
        self.tick(100)
        self.assertIsNone(self.service.managed_retry)
        setattr(self, attribute, True)
        for now in (103, 221, 701):
            self.tick(now)
            self.assert_unsubmitted_wait()
        self.assertEqual(self.preflight.call_count, 0)

    def test_typed_busy_is_a_prewrite_game_busy_error(self):
        self.assertTrue(issubclass(native_memory.DeathRecoveryBusyError,
                                  native_memory.GameBusyError))

    def test_unsubmitted_death_wait_survives_unreadable_control(self):
        self.assert_long_read_wait('control_unavailable')

    def test_unsubmitted_death_wait_survives_incomplete_inventory(self):
        self.assert_long_read_wait('inventory_incomplete')

    def test_unsubmitted_death_wait_survives_online_saving(self):
        self.assert_long_read_wait('saving')

    def test_native_revive_wait_does_not_pause_or_submit_after_two_minutes(self):
        self.start_dead()
        for now in (100, 103, 108, 121, 221, 701):
            self.tick(now)
            self.assert_unsubmitted_wait()
        self.assertEqual(self.preflight.call_count, 0)
        self.assertIsNone(self.service.managed_retry)

    def test_known_dead_never_attempts_even_a_generic_prewrite(self):
        self.start_dead()
        self.busy_error_type = native_memory.GameBusyError
        for now in (100, 103, 221, 701):
            self.tick(now)
            self.assert_unsubmitted_wait()
        self.assertIsNone(self.service.managed_retry)
        self.preflight.assert_not_called()

    def test_living_generic_prewrite_busy_keeps_its_existing_pause_budget(self):
        self.start_dead()
        self.dead = False
        self.busy_error_type = native_memory.GameBusyError
        for now in (100, 103, 300):
            self.tick(now)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.native.assert_not_called()

    def test_natural_town_arrival_does_not_replay_the_unsubmitted_return(self):
        self.start_dead()
        self.tick(100)
        self.client.in_town = True
        self.client.current_map = None
        self.dead = False
        self.tick(101)  # Detect town even before the old backoff expires.
        self.assertTrue(self.service.managed.running)
        # An empty synthetic town needs no cleanup writes and may immediately
        # enter its target map. It must never replay the old death return.
        self.assertIn(self.service.managed.phase, ('cleanup', 'travel'))
        self.assertIsNone(self.service.managed_retry)
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertTrue(all(action[0] != 'return_town' for action in self.client.actions))
        self.assertTrue(all(call.args[0] != 'return_town' for call in self.native.call_args_list))
        self.assertEqual(self.service.managed.repeat_completed, 7)

    def test_native_repeat_new_live_run_cancels_old_death_wait_without_a_win(self):
        self.start_dead()
        self.tick(100)
        self.control_unavailable = True
        self.tick(221)
        self.assert_unsubmitted_wait()
        self.control_unavailable = False
        self.dead = False
        self.client.run_status = 'running'
        self.run_id = 'native-repeat-new-run'
        self.tick(222)
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed.phase, 'farming')
        self.assertIsNone(self.service.managed_retry)
        self.assertIsNone(self.service.managed.pending)
        self.assertEqual(self.preflight.call_count, 0)
        self.native.assert_not_called()
        self.assertEqual(self.service.managed.repeat_completed, 7)
        self.assertIsNone(self.service.managed.last_success)
        # A death can revive into a successful run with this same identity.
        self.assertNotIn('dead-run', self.service.managed.handled_runs)
        self.assertNotIn('native-repeat-new-run', self.service.managed.handled_runs)

    def test_still_dead_never_submits_return_even_when_native_return_is_available(self):
        self.start_dead()
        self.tick(100)
        self.tick(221)
        self.assert_unsubmitted_wait()
        self.death_busy = False
        self.tick(300)
        self.tick(701)
        self.assert_unsubmitted_wait()
        self.preflight.assert_not_called()

    def test_existing_accepted_return_keeps_the_two_minute_receipt_fence(self):
        self.start_dead()
        self.dead = False
        self.death_busy = False
        self.tick(100)
        self.native.assert_called_once()
        self.assertIsNotNone(self.service.managed_transition)
        self.dead = True
        self.teleporting = True
        for now in (101, 160, 219):
            self.tick(now)
            self.assertTrue(self.service.managed.running)
            self.native.assert_called_once()
        self.tick(221)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.native.assert_called_once()

    def test_death_after_confirmed_cancelled_return_waits_for_native_revive(self):
        self.start_dead()
        self.dead = False
        self.death_busy = False
        for now in (100, 101, 109):
            self.tick(now)
        self.native.assert_called_once()
        self.assertIsNone(self.service.managed_transition)
        self.assertEqual(self.service.managed_return_recovery['started'], 100)
        self.dead = True
        self.death_busy = True
        self.tick(112)
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_return_recovery)
        self.tick(221)
        self.assertTrue(self.service.managed.running)
        self.assertIsNone(self.service.managed_transition)
        self.native.assert_called_once()

    def test_recovered_live_run_preserves_the_need_to_finish(self):
        self.start_dead()
        self.tick(100)
        self.service.finish_managed()
        self.dead = False
        self.death_busy = False
        self.client.run_status = 'running'
        self.run_id = 'new-live-run'
        for now in (103, 106, 110):
            self.tick(now)
            if self.native.called:
                break
        self.assertTrue(self.service.managed.finishing)
        self.native.assert_called_once()
        self.assertEqual(self.client.actions[0][0], 'return_town')
        self.assertEqual(self.service.managed.repeat_completed, 7)

    def test_recovery_preserves_merchant_intent_and_purchase_counts(self):
        self.start_dead(merchant=True)
        self.service.managed.merchant_pending = True
        self.service.managed.merchant_counts = {'2': 4}
        self.tick(100)
        self.dead = False
        self.death_busy = False
        self.client.run_status = 'running'
        self.run_id = 'new-live-run'
        for now in (103, 106, 110):
            self.tick(now)
            if self.native.called:
                break
        self.assertTrue(self.service.managed.merchant_pending)
        self.assertEqual(self.service.managed.merchant_counts, {'2': 4})
        self.native.assert_called_once()
        self.assertEqual(self.client.actions[0][0], 'return_town')
        self.assertEqual(self.service.managed.repeat_completed, 7)

    def test_user_pause_cancels_unsubmitted_death_retries_and_keeps_statistics(self):
        self.start_dead()
        self.tick(100)
        statistics = deepcopy(self.service.managed.metrics.state())
        self.service.stop_managed(pause=True)
        self.tick(701)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.assertIsNone(self.service.managed_retry)
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertEqual(self.preflight.call_count, 0)
        self.native.assert_not_called()
        self.assertEqual(self.service.managed.metrics.state(), statistics)

    def test_session_change_during_unsubmitted_death_wait_pauses_before_any_write(self):
        self.start_dead()
        self.tick(100)
        snapshot = self.client.snapshot

        def changed_session():
            value = snapshot()
            value['bridge_session'] = 'new-session'
            return value

        self.client.snapshot = changed_session
        self.tick(101)  # Identity checking must precede the old retry backoff.
        self.tick(701)
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.service.managed.phase, 'paused')
        self.assertIn('游戏连接已改变', self.service.managed.status)
        self.assertTrue(self.service.managed_can_resume)
        self.assertEqual(self.preflight.call_count, 0)
        self.assertIsNone(self.service.managed_transition)
        self.assertIsNone(self.service.managed_return_recovery)
        self.native.assert_not_called()
        self.assertEqual(self.client.actions, [])
        self.assertEqual(self.service.managed.repeat_completed, 7)

    def test_user_stop_cancels_unsubmitted_death_retries(self):
        self.start_dead()
        self.tick(100)
        self.service.stop_managed()
        self.tick(701)
        self.assertFalse(self.service.managed.running)
        self.assertFalse(self.service.managed_can_resume)
        self.assertIsNone(self.service.managed_retry)
        self.assertIsNone(self.service.managed_return_recovery)
        self.assertEqual(self.preflight.call_count, 0)
        self.native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
