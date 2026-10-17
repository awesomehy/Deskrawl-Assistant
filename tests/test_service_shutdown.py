"""Shutdown ownership tests; fake clients never access Steam or a game."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from deskrawl_assistant.community_stats import CommunityStats
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.web_service import AssistantService


class ServiceShutdownTests(unittest.TestCase):
    def service(self):
        service = AssistantService.__new__(AssistantService)
        service.guard = threading.RLock()
        service.closed = threading.Event()
        service.monitor_wake = threading.Event()
        service.cancelled = threading.Event()
        service.worker = None
        service.recommendation_thread = None
        service.loot_capture_thread = SimpleNamespace(join=lambda: None)
        service.community = service.market = None
        service.updating = service.busy = False
        service.managed = SimpleNamespace(running=False)
        service.stop = lambda: None
        service.client = SimpleNamespace(close=lambda: None)
        return service

    def test_shutdown_atomically_blocks_http_start_before_worker_registration(self):
        service = self.service()
        closing = threading.Event()
        attempting = threading.Event()
        acquired = threading.Event()
        release = threading.Event()
        outcomes = []

        def stop():
            # Simulate the normal stop's guard, then give an HTTP request the
            # precise opportunity that used to exist before closed.set().
            with service.guard:
                closing.set()
            self.assertTrue(attempting.wait(2))
            acquired.wait(.15)

        def game_actions_blocked():
            acquired.set()
            self.assertTrue(release.wait(2))
            return False

        def request():
            closing.wait(2)
            attempting.set()
            try:
                AssistantService.start(service, 'connect')
                outcomes.append('started')
            except Exception as exc:
                outcomes.append(exc)

        service.stop = stop
        service.game_actions_blocked = game_actions_blocked
        service.client.close = release.set
        service._work = lambda *args: outcomes.append('worker-ran')
        service.monitor_thread = threading.Thread(target=service.closed.wait)
        service.monitor_thread.start()
        request_thread = threading.Thread(target=request)
        request_thread.start()
        try:
            AssistantService.close(service)
        finally:
            release.set()
            request_thread.join(2)
            service.monitor_thread.join(2)
            if service.worker is not None:
                service.worker.join(2)
        self.assertFalse(request_thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], ValidationError)
        self.assertIsNone(service.worker)

    def test_client_close_waits_for_last_worker_registered_by_observer(self):
        service = self.service()
        entered = threading.Event()
        release = threading.Event()
        client_closed = threading.Event()
        service.client.close = client_closed.set

        def observe():
            service.closed.wait(2)
            # An observer can have registered its last job just as close
            # begins. Its final worker is discoverable only after joining it.
            service.worker = threading.Thread(target=lambda: (entered.set(), release.wait(2)))
            service.worker.start()

        service.monitor_thread = threading.Thread(target=observe)
        service.monitor_thread.start()
        close_thread = threading.Thread(target=lambda: AssistantService.close(service))
        close_thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(client_closed.is_set())
        finally:
            release.set()
            close_thread.join(2)
            service.monitor_thread.join(2)
            if service.worker is not None:
                service.worker.join(2)
        self.assertFalse(close_thread.is_alive())
        self.assertTrue(client_closed.is_set())

    def test_community_close_waits_for_native_statistics_provider_before_database_close(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()
            release = threading.Event()
            joined = threading.Event()
            closed = threading.Event()
            errors = []

            def statistics():
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test timed out')
                return {'available': True, 'session': 'one', 'source': 'verified_game_statistics',
                        'class_mask': 4, 'level': 70, 'play_seconds': 123,
                        'divine_count': 1, 'rune_set_count': 2}

            community = CommunityStats(Path(directory) / 'community.sqlite3', 'test',
                provider=lambda: True, start=False, statistics_provider=statistics)

            def report():
                try:
                    community.report_once()
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=report)
            worker.start()

            class JoinDeadline:
                # A timed join is allowed to return while a native read is
                # still active. Emulate that expiry without an 8-second test.
                def join(self, timeout=None):
                    joined.set()
                    worker.join(None if timeout is None else 0)

            community.worker = JoinDeadline()
            close_thread = threading.Thread(target=lambda: (community.close(), closed.set()))
            try:
                self.assertTrue(entered.wait(2))
                close_thread.start()
                self.assertTrue(joined.wait(2))
                self.assertFalse(closed.wait(.05))
            finally:
                release.set()
                close_thread.join(2)
                worker.join(2)
            self.assertFalse(close_thread.is_alive())
            self.assertTrue(closed.is_set())
            self.assertFalse(errors)


if __name__ == '__main__':
    unittest.main()
