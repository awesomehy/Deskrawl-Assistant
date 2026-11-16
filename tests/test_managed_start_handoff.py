"""Deterministic start/poll handoff tests; synthetic clients, no live game writes."""
from threading import Event, Thread as RealThread
import unittest
from unittest.mock import patch

from deskrawl_assistant.models import ValidationError
import test_managed_service as fixtures


class ManagedStartHandoffTests(unittest.TestCase):
    settings = fixtures.ManagedServiceTests.settings

    def setUp(self):
        fixtures.ManagedServiceTests.setUp(self)
        self.service.save_managed(self.settings(auto_equip=True))
        self.release = Event()
        self.threads = []
        self.refresh_control = self.service._managed_refresh_control
        self.addCleanup(self.join_threads)

    def join_threads(self):
        self.release.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

    def thread(self, target):
        thread = RealThread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def background(self, kind='observe'):
        entered = Event()
        def blocked_refresh():
            entered.set()
            if not self.release.wait(3):
                raise RuntimeError('test did not release background refresh')
            self.refresh_control()

        replacement = patch.object(self.service, '_managed_refresh_control', side_effect=blocked_refresh)
        replacement.start()
        self.addCleanup(replacement.stop)
        with self.service.guard:
            self.service.busy = True
            self.service.operation_kind = kind
        self.thread(lambda: self.service._work(kind, {}))
        self.assertTrue(entered.wait(2))

    def start_waiting(self):
        waiting = Event()
        wait = self.service.managed_start_ready.wait

        def signal_wait(timeout=None):
            waiting.set()
            return wait(timeout)

        replacement = patch.object(self.service.managed_start_ready, 'wait', side_effect=signal_wait)
        replacement.start()
        self.addCleanup(replacement.stop)
        outcomes = []

        def start():
            try:
                outcomes.append(self.service.set_managed_running(True))
            except Exception as exc:
                outcomes.append(exc)

        thread = self.thread(start)
        self.assertTrue(waiting.wait(2), outcomes)
        self.assertIsNotNone(self.service.managed_start_pending)
        self.assertFalse(self.service.managed.running)
        return thread, outcomes

    def test_single_click_waits_for_observe_monitor_or_read_then_starts_once(self):
        for kind in ('observe', 'monitor', 'read'):
            with self.subTest(kind=kind):
                self.release.clear()
                self.background(kind)
                with patch.object(self.service.managed, 'start', wraps=self.service.managed.start) as start:
                    starter, outcomes = self.start_waiting()
                    self.assertEqual(start.call_count, 0)
                    self.release.set()
                    starter.join(2)
                    self.assertFalse(starter.is_alive())
                    self.assertEqual(len(outcomes), 1)
                    self.assertIsInstance(outcomes[0], dict)
                    self.assertTrue(self.service.managed.running, outcomes)
                    self.assertEqual(start.call_count, 1)
                    self.assertIsNone(self.service.managed_start_pending)
                    self.assertFalse(self.service.busy)
                    self.assertEqual(self.client.actions, [])
                self.service.stop_managed()

    def test_duplicate_click_does_not_queue_a_second_start(self):
        self.background()
        starter, outcomes = self.start_waiting()
        with self.assertRaisesRegex(ValidationError, '正在等待'):
            self.service.set_managed_running(True)
        self.release.set()
        starter.join(2)
        self.assertTrue(self.service.managed.running, outcomes)
        self.assertEqual(len(outcomes), 1)

    def test_manual_writes_keep_existing_busy_rejection(self):
        for kind in ('move_items', 'manual_auction', 'lock_selected', 'auction_refresh', 'managed', None):
            with self.subTest(kind=kind), self.service.guard:
                self.service.busy = True
                self.service.operation_kind = kind
                with self.assertRaisesRegex(ValidationError, '请等待当前操作完成'):
                    self.service.set_managed_running(True)
                self.assertIsNone(self.service.managed_start_pending)
                self.assertFalse(self.service.managed.running)
                self.service.busy = False
                self.service.operation_kind = None

    def test_timeout_releases_reservation_and_never_starts_later(self):
        self.background()
        with patch('deskrawl_assistant.managed_service.MANAGED_START_WAIT_SECONDS', 0):
            with self.assertRaisesRegex(ValidationError, '本次未启动托管'):
                self.service.set_managed_running(True)
        self.assertIsNone(self.service.managed_start_pending)
        self.release.set()
        self.join_threads()
        self.assertFalse(self.service.managed.running)
        self.service.set_managed_running(True)
        self.assertTrue(self.service.managed.running)

    def test_pause_stop_and_graceful_stop_cancel_pending_start_without_interrupting_worker(self):
        for stop in (lambda: self.service.set_managed_running(False),
                     lambda: self.service.stop_managed(), self.service.finish_managed):
            with self.subTest(stop=stop):
                self.release.clear()
                self.background()
                starter, outcomes = self.start_waiting()
                stop()
                starter.join(2)
                self.assertFalse(starter.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], ValidationError)
                self.assertIn('已取消', str(outcomes[0]))
                self.assertTrue(self.service.busy)
                self.assertFalse(self.service.cancelled.is_set())
                self.release.set()
                self.join_threads()
                self.assertFalse(self.service.managed.running)
                self.assertIsNone(self.service.managed_start_pending)

    def test_close_and_update_cancel_the_wait(self):
        for state in ('closed', 'updating'):
            with self.subTest(state=state):
                self.release.clear()
                self.background()
                starter, outcomes = self.start_waiting()
                with self.service.guard:
                    if state == 'closed': self.service.closed.set()
                    else: self.service.updating = True
                    self.service.managed_start_ready.notify_all()
                starter.join(2)
                self.assertFalse(starter.is_alive())
                self.assertIsInstance(outcomes[0], ValidationError)
                self.assertIn('退出或更新', str(outcomes[0]))
                self.release.set()
                self.join_threads()
                self.assertFalse(self.service.managed.running)
                self.assertIsNone(self.service.managed_start_pending)
                self.service.closed.clear()
                self.service.updating = False

    def test_real_shutdown_cancels_waiter_before_joining_inflight_worker(self):
        self.background()
        starter, outcomes = self.start_waiting()
        closer = self.thread(self.service.close)
        starter.join(2)
        self.assertFalse(starter.is_alive())
        self.assertIsInstance(outcomes[0], ValidationError)
        self.assertIn('已取消', str(outcomes[0]))
        # Shutdown still waits for the existing worker; no second one is run.
        self.assertTrue(closer.is_alive())
        self.assertTrue(self.service.closed.is_set())
        self.release.set()
        self.join_threads()
        self.assertFalse(self.client.connected)
        self.assertFalse(self.service.managed.running)
        self.assertIsNone(self.service.managed_start_pending)

    def test_worker_read_failure_wakes_start_and_rejects_disconnected_client(self):
        self.background()
        starter, outcomes = self.start_waiting()
        # The observer's error handler cannot recover the inventory snapshot,
        # so it disconnects. Completion must wake, then revalidate the start.
        with patch.object(self.client, 'snapshot', side_effect=RuntimeError('offline read failed')), \
                patch.object(self, 'refresh_control', side_effect=RuntimeError('offline read failed')):
            self.release.set()
            self.join_threads()
        self.assertFalse(starter.is_alive())
        self.assertIsInstance(outcomes[0], ValidationError)
        self.assertIn('请先连接', str(outcomes[0]))
        self.assertFalse(self.service.managed.running)
        self.assertIsNone(self.service.managed_start_pending)

    def test_connection_and_capability_are_rechecked_after_background_finishes(self):
        for state in ('disconnected', 'capability', 'transition'):
            with self.subTest(state=state):
                self.release.clear()
                self.background()
                starter, outcomes = self.start_waiting()
                if state == 'disconnected': self.client.connected = False
                elif state == 'capability': self.client.capabilities['equip']['available'] = False
                else: self.service.game_transition = True
                self.release.set()
                self.join_threads()
                self.assertIsInstance(outcomes[0], ValidationError)
                self.assertFalse(self.service.managed.running)
                self.assertIsNone(self.service.managed_start_pending)
                self.assertFalse(self.client.actions)
                self.client.connected = True
                self.client.capabilities['equip']['available'] = True
                self.service.game_transition = False

    def test_reservation_blocks_competing_operations_even_in_idle_handoff_gap(self):
        self.service.managed_start_pending = object()
        try:
            for operation in (lambda: self.service.start('read'),
                              lambda: self.service.set_monitoring(True),
                              lambda: self.service.set_automation_running(True),
                              self.service._manual_auction_gate, self.service.reset_managed):
                with self.subTest(operation=operation), self.assertRaises(ValidationError):
                    operation()
            # Run exactly one observer iteration in the idle handoff window.
            with patch.object(self.service, 'closed') as closed, \
                    patch.object(self.service, 'monitor_wake') as wake:
                closed.is_set.side_effect = [False, False, False, True]
                self.service._monitor_loop()
                wake.wait.assert_called_once_with(2)
            self.assertFalse(self.service.busy)
            self.assertIsNone(self.service.worker)
        finally:
            self.service.managed_start_pending = None


if __name__ == '__main__':
    unittest.main()
