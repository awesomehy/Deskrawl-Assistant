"""Authentication and confirmation recovery, using in-memory Steam replies only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.auction import DEFAULT_AUCTION
from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.steam_market import APP_ID
from deskrawl_assistant.steam_session import SteamSession, SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from tests.test_steam_session import STEAMID, cookies, page
from tests.test_steam_trading import catalog, inventory, listing


class SteamFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000
        self.document = page()
        self.failure = None
        self.listing_failure = None
        self.html = ''
        self.sales = []
        self.requests = []
        self.session = SteamSession(self.root / 'session', transport=self.transport,
            protector=lambda raw, **kw: raw, clock=lambda: self.now)
        self.session.capture_session(cookies())
        self.trading = SteamTradingService(self.root, catalog(), session=self.session, clock=lambda: self.now)
        self.settings = {'upload_enabled': True, 'sell_enabled': True, 'categories': ['gold_bar']}

    def transport(self, method, path, data, values):
        self.requests.append((method, path))
        if self.failure:
            raise self.failure
        if path == '/market/':
            return self.document
        if path.startswith('/inventory/'):
            return inventory(('1', 1, {}))
        if path.startswith('/market/listings/'):
            return listing()
        if path.startswith('/market/mylistings/'):
            if self.listing_failure:
                raise self.listing_failure
            return {'success': 1, 'total_count': 1, 'results_html': self.html}
        if method == 'POST' and path == '/market/sellitem/':
            self.sales.append(deepcopy(data))
            return {'success': 1, 'listingid': '345', 'requires_confirmation': True}
        raise AssertionError(path)

    def pending_sale(self):
        self.trading.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        return self.trading.sale_once(self.settings, STEAMID)

    def pending_html(self):
        return (f'<a onclick="CancelMarketListingConfirmation(\'mylisting_confirmation\','
                f'\'345\',\'{APP_ID}\',\'2\',\'1\')">pending</a>')

    def test_confirmed_login_is_rechecked_without_managed_running(self):
        self.trading.refresh_auth()
        self.document = 'var g_steamID = false;'
        self.now += 31
        self.trading.refresh_auth()
        state = self.trading.state()
        self.assertTrue(state['expired'])
        self.assertFalse(state['verified'])
        self.assertFalse(state['bound'])
        self.assertIn('重新登录', state['message'])
        self.assertFalse(self.session.path.exists())
        previous = len(self.requests)
        self.now += 60
        self.trading.refresh_auth()
        self.assertEqual(len(self.requests), previous)
        self.assertEqual(self.sales, [])

    def test_network_limits_and_unknown_page_do_not_prove_expiry(self):
        for error in (SteamSessionError('network'), SteamSessionError('limited', status=429),
                      SteamSessionError('forbidden', status=403), None):
            with self.subTest(error=error):
                self.failure = None
                self.document = page()
                self.session.capture_session(cookies())
                self.failure = error
                self.document = '<html>temporarily unavailable</html>'
                with self.assertRaises(SteamSessionError):
                    self.session.verify(force=True)
                state = self.session.state()
                self.assertFalse(state['expired'])
                self.assertFalse(state['verified'])
                self.assertTrue(state['bound'])
                self.assertTrue(self.session.path.exists())

    def test_http_unauthorized_on_inventory_revokes_previously_verified_login(self):
        self.failure = SteamSessionError('unauthorized', status=401)
        with self.assertRaises(SteamSessionError):
            self.session.request('GET', f'/inventory/{STEAMID}/{APP_ID}/2')
        self.assertTrue(self.session.state()['expired'])
        self.assertFalse(self.session.state()['verified'])

    def test_local_generation_check_is_not_an_expired_login(self):
        with self.assertRaises(SteamSessionError):
            self.session.post('/market/sellitem/', {}, expected_generation=-1)
        self.assertFalse(self.session.state()['expired'])
        self.assertTrue(self.session.state()['verified'])

    def test_late_expiry_reply_does_not_erase_a_new_binding(self):
        def rebind_then_fail(*args):
            self.session.capture_session(cookies(steamid='76561198000000002'), verify_now=False)
            raise SteamSessionError('old request unauthorized', status=401)
        self.session.transport = rebind_then_fail
        with self.assertRaises(SteamSessionError):
            self.session.verify(force=True)
        self.assertFalse(self.session.state()['expired'])
        self.assertTrue(self.session.state()['bound'])
        self.assertTrue(self.session.path.exists())

    def test_relogin_clears_expiry(self):
        self.document = 'var g_steamID = false;'
        with self.assertRaises(SteamSessionError):
            self.session.verify(force=True)
        self.document = page()
        state = self.session.capture_session(cookies())
        self.assertTrue(state['verified'])
        self.assertFalse(state['expired'])

    def test_failed_uncommitted_binding_does_not_delete_previous_saved_credentials(self):
        saved = self.session.path.read_bytes()
        self.document = 'var g_steamID = false;'
        with self.assertRaises(SteamSessionError):
            self.session.capture_session(cookies(steamid='76561198000000002'))
        self.assertEqual(self.session.path.read_bytes(), saved)
        self.assertFalse(self.session.state()['expired'])
        self.assertTrue(self.session.state()['bound'])

    def test_coordinator_delivers_expiry_to_config_revoke_hook_without_running_sales(self):
        revoke = Mock()
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False, on_auth_expired=revoke)
        self.addCleanup(coordinator.close)
        self.document = 'var g_steamID = false;'
        self.now += 31
        coordinator.process_once()
        revoke.assert_called_once()
        self.assertTrue(revoke.call_args.args[0]['expired'])
        self.assertTrue(coordinator.state()['steam']['expired'])
        self.assertEqual(self.sales, [])

    def test_confirmation_count_expires_after_fifteen_seconds_not_journal(self):
        state = self.pending_sale()
        self.assertEqual(state['pending_count'], 1)
        self.assertTrue(state['confirmation_known'])
        self.now += 15
        state = self.trading.state()
        self.assertEqual(state['pending_count'], 0)
        self.assertFalse(state['confirmation_known'])
        self.assertEqual(state['counts']['pending'], 1)

    def test_confirmation_refresh_works_with_sales_off_and_clears_on_failure(self):
        self.pending_sale()
        self.html = self.pending_html()
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.process_once()
        self.assertEqual(self.trading.state()['pending_count'], 1)
        self.now += 15
        self.listing_failure = SteamSessionError('unreadable')
        coordinator.process_once()
        self.assertEqual(self.trading.state()['pending_count'], 0)
        self.assertFalse(self.trading.state()['confirmation_known'])
        self.assertEqual(self.trading.state()['counts']['pending'], 1)
        self.now += 15
        self.listing_failure = None
        coordinator.process_once()
        self.assertEqual(self.trading.state()['pending_count'], 1)
        self.assertEqual(len(self.sales), 1)

    def test_old_missing_pending_entry_is_not_claimed_as_pending_or_resold(self):
        self.pending_sale()
        self.now += 16
        self.trading.refresh_confirmation()
        self.assertTrue(self.trading.state()['confirmation_known'])
        self.assertEqual(self.trading.state()['pending_count'], 0)
        self.assertEqual(self.trading.state()['counts']['pending'], 1)
        self.trading.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.sales), 1)

    def test_reboot_does_not_restore_cached_confirmation_count(self):
        self.pending_sale()
        restarted = SteamTradingService(self.root, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restarted.state()['pending_count'], 0)
        self.assertFalse(restarted.state()['confirmation_known'])
        self.assertEqual(restarted.state()['counts']['pending'], 1)

    def test_stale_coordinator_cache_cannot_extend_confirmation_lifetime(self):
        self.pending_sale()
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.cached = self.trading.state()
        with patch('deskrawl_assistant.auction_service.time.time', return_value=self.now):
            self.assertEqual(coordinator.state()['sell']['confirmation_count'], 1)
        with patch('deskrawl_assistant.auction_service.time.time', return_value=self.now + 16):
            self.assertEqual(coordinator.state()['sell']['confirmation_count'], 0)


class ExpiryManagedConfigTests(unittest.TestCase):
    from tests.test_managed_service import ManagedServiceTests
    setUp = ManagedServiceTests.setUp

    def test_expiry_persists_only_sale_switch_and_leaves_running_uploads_enabled(self):
        self.service.managed.settings['auction'].update(upload_enabled=True, sell_enabled=True,
            categories=['gold_bar'], excluded_keys=['DivineHelm1'])
        self.service.managed.running = True
        before = deepcopy(self.service.managed.settings)
        self.service._disable_steam_sale()
        expected = deepcopy(before)
        expected['auction']['sell_enabled'] = False
        self.assertEqual(self.service.managed.settings, expected)
        self.assertEqual(json.loads(self.service.managed_config.read_text(encoding='utf-8')), expected)
        self.assertTrue(self.service.managed.running)
        self.assertEqual(self.client.actions, [])

    def test_failed_config_write_revokes_runtime_permission_and_retries(self):
        self.service.managed.settings['auction'].update(upload_enabled=True, sell_enabled=True)
        with patch('deskrawl_assistant.storage_preferences.write_settings_files', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.service._disable_steam_sale()
        self.assertFalse(self.service.managed.settings['auction']['sell_enabled'])
        self.assertTrue(self.service.managed.settings['auction']['upload_enabled'])
        self.service._disable_steam_sale()
        self.assertFalse(json.loads(self.service.managed_config.read_text(encoding='utf-8'))['auction']['sell_enabled'])

    def test_old_expiry_generation_does_not_revoke_new_login_settings(self):
        self.service.managed.settings['auction'].update(upload_enabled=True, sell_enabled=True)
        self.service.auction_coordinator = Mock()
        self.service.auction_coordinator.trading.session.state.return_value = {'expired': False, 'binding_generation': 2}
        self.service._disable_steam_sale(expired_generation=1)
        self.assertTrue(self.service.managed.settings['auction']['sell_enabled'])
        self.service.auction_coordinator = None
