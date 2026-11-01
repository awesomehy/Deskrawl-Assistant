from copy import deepcopy
import unittest
from unittest.mock import patch

import test_managed_service as service_tests


class ManagedSceneWaitTests(unittest.TestCase):
    setUp = service_tests.ManagedServiceTests.setUp
    settings = service_tests.ManagedServiceTests.settings
    ready = service_tests.ManagedServiceTests.ready

    def test_town_with_stuck_native_flag_pauses_without_touching_rewards(self):
        self.ready()
        observation = self.service._managed_observation()
        observation['scene']['scene_changing'] = True
        engine = self.service.managed
        engine.repeat_completed = 33
        engine.await_effect = {'kind': 'synthesize_runes', 'receipt_id': 'outstanding'}
        totals = deepcopy(engine.metrics.state())
        items = deepcopy(self.client.items)
        with patch.object(self.service, '_managed_observation', return_value=observation), \
                patch.object(engine, 'next_action') as planner:
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                self.service._managed_once()
            self.assertIn('已在主城', engine.status)
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=219):
                self.service._managed_once()
            self.assertTrue(engine.running)
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=220):
                self.service._managed_once()
        planner.assert_not_called()
        self.assertFalse(engine.running)
        self.assertEqual(engine.phase, 'paused')
        self.assertTrue(self.service.managed_can_resume)
        self.assertIn('场景切换状态', engine.status)
        self.assertEqual(engine.repeat_completed, 33)
        self.assertEqual(engine.metrics.state(), totals)
        self.assertEqual(engine.await_effect['receipt_id'], 'outstanding')
        self.assertEqual(self.client.items, items)
        self.assertEqual(self.client.actions, [])

    def test_explicit_scene_recovery_releases_planner_without_waiting_for_timeout(self):
        self.ready()
        observation = self.service._managed_observation()
        for flag in ('scene_changing', 'teleporting'):
            with self.subTest(flag=flag), \
                    patch.object(self.service, '_managed_observation', return_value=observation), \
                    patch.object(self.service.managed, 'next_action', return_value=None) as planner:
                observation['scene'][flag] = True
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                    self.service._managed_once()
                planner.assert_not_called()
                observation['scene'][flag] = False
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=110):
                    self.service._managed_once()
                planner.assert_called_once()
                self.assertIsNone(self.service.managed_scene_wait)
                self.assertTrue(self.service.managed.running)

    def test_unreadable_flag_or_scene_does_not_reset_deadline_or_release_planner(self):
        self.ready()
        observation = self.service._managed_observation()
        observation['scene']['scene_changing'] = True
        with patch.object(self.service, '_managed_observation', return_value=observation), \
                patch.object(self.service.managed, 'next_action') as planner:
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                self.service._managed_once()
            observation['scene']['scene_changing'] = None
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=170):
                self.service._managed_once()
            observation['scene'].update(scene_changing=False, available=False)
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=220):
                self.service._managed_once()
        planner.assert_not_called()
        self.assertFalse(self.service.managed.running)
        self.assertEqual(self.client.actions, [])

    def test_user_resume_rechecks_stuck_flag_instead_of_authorizing_cleanup(self):
        self.ready()
        control = self.client.control_snapshot()
        control['scene_changing'] = True
        with patch.object(self.client, 'control_snapshot', return_value=control):
            with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=100):
                self.service._managed_once()
            self.service.stop_managed(pause=True)
            self.assertIsNone(self.service.managed_scene_wait)
            self.service.set_managed_running(True)
            with patch.object(self.service.managed, 'next_action') as planner:
                with patch('deskrawl_assistant.managed_service.time.monotonic', return_value=300):
                    self.service._managed_once()
        planner.assert_not_called()
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.service.managed_scene_wait['started'], 300)
        self.assertEqual(self.client.actions, [])


if __name__ == '__main__':
    unittest.main()
