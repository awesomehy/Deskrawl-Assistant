"""Read-only status diagnostics: every process API is mocked, no game actions."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_lifecycle import GameLifecycle, WindowsGameProcess
from tests import test_managed_service as service_fixtures


class GameStatusUnknownTests(unittest.TestCase):
    target = {'pid': 42, 'instance': '42:123'}

    def setUp(self):
        logging = patch('deskrawl_assistant.game_lifecycle.record')
        self.log = logging.start()
        self.addCleanup(logging.stop)

    @staticmethod
    def windows_error(code):
        error = OSError('mock Windows process query failure')
        error.winerror = code
        return error

    def backend(self, pids, failures=None):
        process = WindowsGameProcess()
        failures = failures or {}

        @contextmanager
        def identity(pid):
            if pid in failures:
                raise failures[pid]
            yield 7, {'pid': pid, 'instance': str(pid)+':123'}

        process._identity = Mock(side_effect=identity)
        return process, patch('deskrawl_assistant.native_memory.game_pids', return_value=pids)

    def test_matching_process_with_access_denied_is_unknown(self):
        process, enumerated = self.backend([42], {42: self.windows_error(5)})
        with enumerated:
            state = GameLifecycle(process).state(fresh=True)
        self.assertEqual(state['status'], 'unknown')
        self.assertFalse(state['can_start'])
        self.assertFalse(state['can_close'])
        self.assertIn('process query failure', state['error'])

    def test_one_inaccessible_instance_preserves_another_readable_running_instance(self):
        process, enumerated = self.backend([42, 43], {43: self.windows_error(5)})
        with enumerated:
            state = GameLifecycle(process).state(fresh=True)
        self.assertEqual(state['status'], 'running')
        self.assertTrue(state['running'])
        self.assertEqual(state['count'], 1)
        self.assertEqual(state['unverified_count'], 1)
        self.assertFalse(state['can_close'])
        self.assertIsNone(state['target'])
        self.assertEqual(state['diagnostics'][0]['pid'], 43)
        self.assertEqual(state['diagnostics'][0]['winerror'], 5)

    def test_readable_multiple_instances_are_running_not_unknown(self):
        process, enumerated = self.backend([42, 43])
        with enumerated:
            state = GameLifecycle(process).state(fresh=True)
        self.assertEqual(state['status'], 'running')
        self.assertTrue(state['running'])
        self.assertEqual(state['count'], 2)
        self.assertFalse(state['can_close'])

    def test_known_exit_races_are_skipped_instead_of_unknown(self):
        for code in (6, 87, 1168):
            with self.subTest(code=code):
                process, enumerated = self.backend([42], {42: self.windows_error(code)})
                with enumerated:
                    state = GameLifecycle(process).state(fresh=True)
                self.assertEqual(state['status'], 'stopped')
                self.assertTrue(state['can_start'])

    def test_no_matching_process_is_stopped_not_unknown(self):
        process, enumerated = self.backend([])
        with enumerated:
            state = GameLifecycle(process).state(fresh=True)
        self.assertEqual(state['status'], 'stopped')
        process._identity.assert_not_called()

    def test_probe_exception_recovers_on_next_successful_query(self):
        backend = Mock()
        backend.instances.side_effect = [self.windows_error(5), [self.target]]
        lifecycle = GameLifecycle(backend)
        self.assertEqual(lifecycle.state(fresh=True)['status'], 'unknown')
        self.assertEqual(lifecycle.state(fresh=True)['status'], 'running')
        backend.launch.assert_not_called()
        backend.close.assert_not_called()

    def test_live_connection_confirms_presence_without_authorizing_close(self):
        backend = Mock()
        backend.instances.side_effect = self.windows_error(5)
        lifecycle = GameLifecycle(backend)
        state = lifecycle.state(fresh=True, known_running_pid=42)
        self.assertEqual(state['status'], 'running')
        self.assertTrue(state['running'])
        self.assertTrue(state['control_unavailable'])
        self.assertFalse(state['can_close'])
        self.assertFalse(state['can_start'])
        self.assertIsNone(state['target'])
        with self.assertRaises(ValueError):
            lifecycle.close(self.target)
        backend.close.assert_not_called()
        backend.launch.assert_not_called()

    def test_old_verified_target_is_discarded_when_fresh_identity_fails(self):
        backend = Mock()
        backend.instances.side_effect = [[self.target], self.windows_error(5), self.windows_error(5)]
        lifecycle = GameLifecycle(backend)
        self.assertEqual(lifecycle.state(fresh=True)['target'], self.target)
        state = lifecycle.state(fresh=True, known_running_pid=42)
        self.assertEqual(state['status'], 'running')
        self.assertFalse(state['can_close'])
        self.assertIsNone(state['target'])
        with self.assertRaises(ValueError):
            lifecycle.close(self.target)
        backend.close.assert_not_called()

    def test_lost_live_evidence_does_not_reuse_running_cache(self):
        backend = Mock()
        backend.instances.side_effect = self.windows_error(5)
        lifecycle = GameLifecycle(backend)
        self.assertEqual(lifecycle.state(known_running_pid=42)['status'], 'running')
        self.assertEqual(lifecycle.state()['status'], 'unknown')

    def test_empty_enumeration_with_live_connection_keeps_controls_unavailable(self):
        backend = Mock()
        backend.instances.return_value = []
        state = GameLifecycle(backend).state(known_running_pid=42)
        self.assertEqual(state['status'], 'running')
        self.assertFalse(state['can_start'])
        self.assertFalse(state['can_close'])
        self.assertIsNone(state['target'])
        self.assertEqual(state['diagnostics'][0]['stage'], 'connected_process')

    def test_failed_probe_does_not_falsely_confirm_pending_close(self):
        backend = Mock()
        backend.instances.side_effect = self.windows_error(5)
        lifecycle = GameLifecycle(backend)
        lifecycle.pending, lifecycle.deadline = 'closing', float('inf')
        state = lifecycle.state(fresh=True)
        self.assertEqual(state['status'], 'closing')
        self.assertEqual(lifecycle.pending, 'closing')
        self.assertFalse(state['can_start'])

    def test_status_diagnostics_logged_only_when_state_changes(self):
        backend = Mock()
        backend.instances.side_effect = [self.windows_error(5), self.windows_error(5), [self.target]]
        lifecycle = GameLifecycle(backend)
        lifecycle.state(fresh=True)
        lifecycle.state(fresh=True)
        self.assertEqual(self.log.call_count, 1)
        lifecycle.state(fresh=True)
        self.assertEqual(self.log.call_count, 2)
        self.assertEqual(self.log.call_args.kwargs['status'], 'running')

    def test_unicode_smb_path_does_not_need_steam_or_local_disk(self):
        def query(_handle, _flags, path, _size):
            path.value = r'\\Fnos\游戏\SteamLibrary\Deskrawl\Deskrawl.exe'
            return True

        def times(_handle, created, *_rest):
            created._obj.dwLowDateTime = 123
            return True

        def alive(_handle, status):
            status._obj.value = 259
            return True

        kernel = SimpleNamespace(OpenProcess=Mock(return_value=7),
            QueryFullProcessImageNameW=Mock(side_effect=query),
            GetProcessTimes=Mock(side_effect=times), GetExitCodeProcess=Mock(side_effect=alive), CloseHandle=Mock())
        with patch('deskrawl_assistant.native_memory.K', kernel):
            with WindowsGameProcess._identity(42) as (handle, identity):
                self.assertEqual(handle, 7)
                self.assertEqual(identity, self.target)
        kernel.OpenProcess.assert_called_once_with(0x1000, False, 42)
        kernel.GetExitCodeProcess.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(7)

    def test_native_api_failures_report_exact_stage_pid_and_winerror(self):
        for stage in ('open_process', 'image_name', 'creation_time', 'exit_code'):
            with self.subTest(stage=stage):
                def query(_handle, _flags, path, _size):
                    path.value = r'G:\games\Deskrawl.exe'
                    return stage != 'image_name'
                kernel = SimpleNamespace(OpenProcess=Mock(return_value=0 if stage=='open_process' else 7),
                    QueryFullProcessImageNameW=Mock(side_effect=query),
                    GetProcessTimes=Mock(return_value=stage != 'creation_time'),
                    GetExitCodeProcess=Mock(return_value=stage != 'exit_code'), CloseHandle=Mock())
                with patch('deskrawl_assistant.native_memory.K', kernel), \
                        patch('deskrawl_assistant.native_memory.game_pids', return_value=[42]), \
                        patch('ctypes.get_last_error', return_value=5):
                    state = GameLifecycle().state(fresh=True)
                self.assertEqual(state['status'], 'unknown')
                self.assertEqual(state['diagnostics'], [{'stage': stage, 'pid': 42,
                    'winerror': 5, 'reason': str(__import__('ctypes').WinError(5))}])
                if stage == 'exit_code':
                    self.assertIn('检查进程存活状态 42未完成', state['error'])
                if stage != 'open_process': kernel.CloseHandle.assert_called_once_with(7)

    def test_readable_identity_that_already_exited_never_becomes_running_or_close_target(self):
        def query(_handle, _flags, path, _size):
            path.value = r'G:\games\Deskrawl.exe'
            return True

        def exited(_handle, status):
            status._obj.value = 0
            return True

        kernel = SimpleNamespace(OpenProcess=Mock(return_value=7),
            QueryFullProcessImageNameW=Mock(side_effect=query), GetProcessTimes=Mock(return_value=True),
            GetExitCodeProcess=Mock(side_effect=exited), CloseHandle=Mock())
        with patch('deskrawl_assistant.native_memory.K', kernel), \
                patch('deskrawl_assistant.native_memory.game_pids', return_value=[42]):
            state = GameLifecycle().state(fresh=True)
        self.assertEqual(state['status'], 'stopped')
        self.assertFalse(state['running'])
        self.assertTrue(state['can_start'])
        self.assertFalse(state['can_close'])
        self.assertIsNone(state['target'])
        self.assertEqual(state['diagnostics'], [])
        kernel.GetExitCodeProcess.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(7)

    def test_enumeration_failure_is_distinct_from_per_process_access_failure(self):
        with patch('deskrawl_assistant.native_memory.game_pids', side_effect=self.windows_error(5)):
            state = GameLifecycle().state(fresh=True)
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(state['diagnostics'][0]['stage'], 'enumerate_processes')
        self.assertIsNone(state['diagnostics'][0]['pid'])

    def test_query_api_errors_preserve_safety_and_do_not_launch_or_close(self):
        for error in (ValueError('目标进程已变化'), ImportError('API unavailable')):
            with self.subTest(error=type(error).__name__):
                backend = Mock()
                backend.instances.side_effect = error
                state = GameLifecycle(backend).state(fresh=True)
                self.assertEqual(state['status'], 'unknown')
                self.assertFalse(state['can_start'])
                self.assertFalse(state['can_close'])
                backend.launch.assert_not_called()
                backend.close.assert_not_called()


class WebStatusEvidenceTests(unittest.TestCase):
    setUp = service_fixtures.ManagedServiceTests.setUp

    def test_state_uses_live_connected_pid_as_running_evidence(self):
        backend = Mock()
        backend.instances.side_effect = OSError('mock process probe denied')
        self.service.game_lifecycle = GameLifecycle(backend)
        with patch('deskrawl_assistant.game_lifecycle.record'):
            state = self.service.state()
        self.assertTrue(state['connected'])
        self.assertEqual(state['game']['status'], 'running')
        self.assertFalse(state['game']['can_close'])
        self.assertIsNone(state['game']['target'])

    def test_readonly_block_check_keeps_the_same_live_evidence_and_status_log(self):
        backend = Mock()
        backend.instances.side_effect = OSError('mock process probe denied')
        self.service.game_lifecycle = GameLifecycle(backend)
        with patch('deskrawl_assistant.game_lifecycle.record') as log:
            state = self.service.state()
            self.assertFalse(self.service.game_actions_blocked())
        self.assertEqual(state['game']['status'], 'running')
        self.assertEqual(log.call_count, 1)
        self.assertEqual(backend.instances.call_count, 1)

    def test_observer_readonly_status_check_uses_current_live_pid(self):
        lifecycle = self.service.game_lifecycle = Mock()
        lifecycle.state.return_value = {'status': 'running'}
        # One deterministic worker wake, then exit before another wait.
        # Thread.start is mocked; no real worker or native action can run.
        self.service.closed.is_set = Mock(side_effect=[False, False, False, True])
        self.service.monitor_wake.wait = Mock()
        self.service.monitor_wake.clear = Mock()
        with patch('deskrawl_assistant.web_service.threading.Thread') as thread:
            self.service._monitor_loop()
        lifecycle.state.assert_called_once_with(known_running_pid=self.client.pid)
        thread.return_value.start.assert_called_once_with()

    def test_close_endpoint_rechecks_identity_after_connected_running_status(self):
        backend = Mock()
        target = {'pid': self.client.pid, 'instance': str(self.client.pid)+':123'}
        backend.instances.side_effect = [[target], OSError('mock process probe denied')]
        self.service.game_lifecycle = GameLifecycle(backend)
        with patch('deskrawl_assistant.game_lifecycle.record'):
            state = self.service.state()
            self.assertTrue(state['game']['can_close'])
            with self.assertRaisesRegex(ValueError, '变化'):
                self.service.game_action('close', {'confirmed': True, 'target': target})
        backend.close.assert_not_called()
        self.assertEqual(self.client.actions, [])

    def test_disconnected_pid_does_not_become_live_evidence(self):
        backend = Mock()
        backend.instances.side_effect = OSError('mock process probe denied')
        self.service.game_lifecycle = GameLifecycle(backend)
        self.client.connected = False
        with patch('deskrawl_assistant.game_lifecycle.record'):
            state = self.service.state()
        self.assertFalse(state['connected'])
        self.assertEqual(state['game']['status'], 'unknown')
        self.assertFalse(state['game']['running'])

    def test_start_endpoint_rejects_live_game_even_when_enumeration_is_empty(self):
        backend = Mock()
        backend.instances.return_value = []
        self.service.game_lifecycle = GameLifecycle(backend)
        with self.assertRaisesRegex(ValueError, '游戏已运行'):
            self.service.game_action('start', {})
        backend.launch.assert_not_called()
        self.assertFalse(self.service.game_transition)


if __name__ == '__main__':
    unittest.main()
