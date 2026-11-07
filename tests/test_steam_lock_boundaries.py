"""Controlled fake waits only; no Steam, game, hosts or certificate access."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.steam_trading import SteamTradingService
from deskrawl_assistant.tray_status import TrayStatusMixin
from tests.test_steam_trading import FakeSession, STEAMID, catalog


class SteamLockBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = FakeSession()
        self.trading = SteamTradingService(Path(self.tmp.name), catalog(), session=self.session)
        self.entered = threading.Event()
        self.release = threading.Event()
        original = self.session.state
        def wait_for_post():
            self.entered.set()
            if not self.release.wait(2):
                raise AssertionError('controlled session wait was not released')
            return original()
        self.session.state = wait_for_post
        self.session.peek_state = lambda: None

    def background(self, action):
        errors = []
        def work():
            try:
                action()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread, errors

    def finish(self, thread, errors):
        self.release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_waiting_for_post_account_lock_does_not_own_trading_guard(self):
        thread, errors = self.background(self.trading.state)
        try:
            self.assertTrue(self.entered.wait(1))
            acquired = self.trading.guard.acquire(blocking=False)
            if acquired:
                self.trading.guard.release()
            self.assertTrue(acquired)
        finally:
            self.finish(thread, errors)

    def test_worker_waiting_for_session_does_not_own_coordinator_guard(self):
        self.trading.refresh_auth = lambda: None
        self.trading.refresh_confirmation = lambda: None
        self.trading.restore_upload_identities = lambda _: None
        coordinator = AuctionCoordinator(Path(self.tmp.name), self.trading,
                                         lambda: (False, {}, STEAMID), start_worker=False)
        thread, errors = self.background(coordinator.process_once)
        try:
            self.assertTrue(self.entered.wait(1))
            acquired = coordinator.guard.acquire(blocking=False)
            if acquired:
                coordinator.guard.release()
            self.assertTrue(acquired)
            # auth_state uses a non-blocking session peek. Pure intent/status
            # projections remain available during the controlled HTTP wait.
            self.assertEqual(coordinator.state()['upload']['phase'], 'idle')
        finally:
            self.finish(thread, errors)
            coordinator.close()

    def test_tray_does_not_keep_service_guard_while_projecting_trades(self):
        service = TrayStatusMixin()
        service.guard = threading.RLock()
        service.client = SimpleNamespace(connected=True)
        service.managed_control = {}
        service.managed_control_read_at = time.monotonic()
        def project(**kwargs):
            self.entered.set()
            if not self.release.wait(2):
                raise AssertionError('controlled projection was not released')
            return {'items': []}
        service.managed_auction_tracking = project
        thread, errors = self.background(service.tray_snapshot)
        try:
            self.assertTrue(self.entered.wait(1))
            acquired = service.guard.acquire(blocking=False)
            if acquired:
                service.guard.release()
            self.assertTrue(acquired)
        finally:
            self.finish(thread, errors)

    def test_game_context_observation_can_progress_while_session_state_waits(self):
        thread, errors = self.background(self.trading.state)
        try:
            self.assertTrue(self.entered.wait(1))
            observed = threading.Event()
            def observe():
                self.trading.observe_game_trade_context({}, [], False)
                observed.set()
            observer, observer_errors = self.background(observe)
            try:
                self.assertTrue(observed.wait(.5))
            finally:
                self.release.set()
                observer.join(2)
                self.assertFalse(observer.is_alive())
                if observer_errors:
                    raise observer_errors[0]
        finally:
            self.finish(thread, errors)
