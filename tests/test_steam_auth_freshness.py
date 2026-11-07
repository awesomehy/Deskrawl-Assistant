"""Authentication and confirmation recovery, using in-memory Steam replies only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
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
        if path.startswith('/market/myhistory/'):
            return {'success': 1, 'total_count': 0, 'events': [], 'listings': {}, 'purchases': {}}
        if method == 'POST' and path == '/market/sellitem/':
            self.sales.append(deepcopy(data))
            return {'success': 1, 'listingid': '345', 'requires_confirmation': True}
        raise AssertionError(path)

    def pending_sale(self):
        self.trading.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        return self.trading.sale_once(self.settings, STEAMID)

    def test_confirmed_upload_survives_real_session_transient_wallet_failure_without_rebinding(self):
        ticket = self.trading.prepare_upload(STEAMID, 'GoldBar')
        expired = []
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, self.settings, STEAMID), start_worker=False, on_auth_expired=expired.append)
        self.addCleanup(coordinator.close)
        item = {'item_uid': 'local', 'instance_id': 'native', 'internal_name': 'GoldBar'}
        identity = coordinator.submitted(item, STEAMID, ticket)
        coordinator.confirmed(identity, {'uploaded_uid': 'local', 'uploaded_instance_id': 'native',
                                         'uploaded_internal_name': 'GoldBar'})
        generation = self.session.state()['binding_generation']
        self.now += 31
        self.failure = SteamSessionError('temporary network error')
        coordinator.process_once()
        self.assertEqual(coordinator.intents[identity]['phase'], 'confirmed')
        self.assertIn(ticket, self.trading._tickets)
        self.assertTrue(self.session.state()['bound'])
        self.assertFalse(self.session.state()['verified'])
        self.assertEqual(expired, [])  # A temporary error must not clear the sale setting.
        self.failure = None
        original = self.session.transport
        self.session.transport = lambda method, path, data, values: (
            inventory(('1', 1, {}), ('2', 1, {})) if path.startswith('/inventory/')
            else original(method, path, data, values))
        self.now += 31
        coordinator.process_once()
        self.assertEqual(self.session.state()['binding_generation'], generation)
        self.assertEqual(coordinator.intents[identity]['phase'], 'resolved')
        self.assertEqual(self.trading._journal[0]['game_instance_id'], 'native')
        self.assertEqual(self.sales, [])
        self.assertEqual(expired, [])

    def test_original_baseline_expiry_releases_confirmed_market_gate_without_forging_game_failure(self):
        ticket = self.trading.prepare_upload(STEAMID, 'GoldBar')
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, self.settings, STEAMID), start_worker=False)
        self.addCleanup(coordinator.close)
        item = {'item_uid': 'local', 'instance_id': 'native', 'internal_name': 'GoldBar'}
        identity = coordinator.submitted(item, STEAMID, ticket)
        coordinator.confirmed(identity, {'uploaded_uid': 'local', 'uploaded_instance_id': 'native',
                                         'uploaded_internal_name': 'GoldBar'})
        self.now += 600
        before = len(self.requests)
        coordinator.process_once()
        self.assertEqual(coordinator.intents[identity]['phase'], 'resolved')
        self.assertEqual(coordinator.intents[identity]['market_skip_reason'], 'baseline_expired')
        self.assertEqual(coordinator.intents[identity]['instance_id'], 'native')
        self.assertNotIn(ticket, self.trading._tickets)
        self.assertEqual(coordinator.observe({'steamid': STEAMID})['blocked_keys'], [])
        self.assertEqual(self.trading._journal, [])
        self.assertEqual(self.sales, [])
        self.assertFalse(any(method == 'POST' for method, _ in self.requests[before:]))

    def pending_html(self):
        return (f'<a onclick="CancelMarketListingConfirmation(\'mylisting_confirmation\','
                f'\'345\',\'{APP_ID}\',\'2\',\'1\')">pending</a>')

    def sale_proof(self):
        self.pending_sale()
        # Real sale journal plus an internal batch proof: reads must only
        # observe this receipt, never submit or fetch Steam themselves.
        proof = 'a' * 32
        self.trading._journal[0]['proof'] = proof
        self.trading._save()
        return proof

    def test_batch_receipt_tracks_phone_confirmation_via_background_read(self):
        proof = self.sale_proof()
        before = len(self.requests)
        self.assertTrue(self.trading.proof_state(proof, STEAMID, wait=False)['confirmation_pending'])
        self.assertEqual(len(self.requests), before)
        self.now += 16
        self.html = (f'<a onclick="RemoveMarketListing(\'mylisting\',\'345\','
                     f'\'{APP_ID}\',\'2\',\'1\')">listed</a>')
        self.trading.refresh_confirmation()
        receipt = self.trading.proof_state(proof, STEAMID, wait=False)
        self.assertEqual(receipt['status'], 'listed')
        self.assertNotIn('assetid', receipt)
        self.assertNotIn('steamid', receipt)
        self.assertEqual(len(self.sales), 1)

    def test_batch_pending_evidence_expires_and_clears_on_read_failure(self):
        proof = self.sale_proof()
        self.now += 15
        self.assertFalse(self.trading.proof_state(proof, STEAMID)['confirmation_pending'])
        self.html = self.pending_html()
        self.trading.refresh_confirmation()
        self.assertTrue(self.trading.proof_state(proof, STEAMID)['confirmation_pending'])
        self.now += 15
        self.listing_failure = SteamSessionError('unreadable')
        self.trading.refresh_confirmation()
        self.assertFalse(self.trading.proof_state(proof, STEAMID)['confirmation_pending'])
        self.assertEqual(len(self.sales), 1)

    def test_batch_receipt_does_not_treat_missing_listing_or_changed_binding_as_pending(self):
        proof = self.sale_proof()
        self.now += 16
        self.trading.refresh_confirmation()
        self.assertEqual(self.trading.proof_state(proof, STEAMID)['status'], 'pending')
        self.assertFalse(self.trading.proof_state(proof, STEAMID)['confirmation_pending'])
        self.assertEqual(self.trading.proof_state(proof, '76561198000000002'), {'status': 'unmatched'})
        self.html = self.pending_html()
        self.now += 16
        self.trading.refresh_confirmation()
        self.assertTrue(self.trading.proof_state(proof, STEAMID)['confirmation_pending'])
        self.session.logout()
        self.assertFalse(self.trading.proof_state(proof, STEAMID)['confirmation_pending'])
        self.assertEqual(len(self.sales), 1)

    def test_batch_receipt_poll_never_waits_on_steam_network_lock(self):
        proof = self.sale_proof()
        before = len(self.requests)
        with self.trading._operation:
            receipt = self.trading.proof_state(proof, STEAMID, wait=False)
            self.assertEqual(receipt['status'], 'pending')
            self.assertTrue(receipt['confirmation_pending'])
            self.now += 16
            self.assertFalse(self.trading.proof_state(proof, STEAMID, wait=False)['confirmation_pending'])
        self.assertEqual(len(self.requests), before)

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

    def test_local_login_progress_is_visible_before_market_worker_updates_cache(self):
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.cached = {'bound': False, 'verified': False, 'login_open': True}
        self.session.capture_session(cookies(), verify_now=False)
        before = len(self.requests)
        state = coordinator.state()['steam']
        self.assertTrue(state['bound'])
        self.assertFalse(state['login_pending'])
        self.assertFalse(state['authenticated'])
        self.assertIn('已加密保存', state['message'])
        self.session.verify()
        state = coordinator.state()['steam']
        self.assertTrue(state['authenticated'])
        self.assertEqual(state['wallet_currency'], 'CNY')
        self.assertEqual(len(self.requests), before + 1)

    def test_relogin_clears_sale_expiry_before_market_worker_refreshes(self):
        self.document = 'var g_steamID = false;'
        self.now += 31
        expired = self.trading.sale_once(self.settings, STEAMID)
        self.assertEqual(expired['status'], 'login_required')
        self.assertTrue(expired['expired'])
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.cached = deepcopy(expired)

        self.document = page()
        self.session.capture_session(cookies(), verify_now=False)
        waiting = coordinator.state()
        self.assertFalse(waiting['steam']['authenticated'])
        self.assertIn('核验', waiting['sell']['message'])
        self.assertNotIn('过期', waiting['sell']['message'])
        self.session.verify()
        before = len(self.requests)
        state = self.trading.state()
        self.assertEqual(state['status'], 'idle')
        self.assertNotIn('过期', state['sale_message'])
        state = coordinator.state()
        self.assertTrue(state['steam']['authenticated'])
        self.assertEqual(state['sell']['phase'], 'idle')
        self.assertNotIn('过期', state['sell']['message'])
        self.assertEqual(len(self.requests), before)
        self.assertEqual(self.sales, [])

        # A later successful no-op sale must not retain the old auth error
        # after its phase changes from login_required back to idle.
        state = self.trading.sale_once(self.settings, STEAMID)
        self.assertEqual(state['status'], 'idle')
        self.assertNotIn('过期', state['sale_message'])
        self.assertEqual(self.sales, [])
        self.session.logout()
        state = coordinator.state()
        self.assertFalse(state['steam']['authenticated'])
        self.assertIn('请先登录', state['sell']['message'])
        self.assertNotIn('过期', state['sell']['message'])

    def test_relogin_keeps_pending_sale_fence_and_other_errors(self):
        self.pending_sale()
        self.document = 'var g_steamID = false;'
        self.now += 31
        self.assertTrue(self.trading.sale_once(self.settings, STEAMID)['expired'])
        self.document = page()
        self.session.capture_session(cookies())
        state = self.trading.state()
        self.assertNotIn('过期', state['sale_message'])
        self.assertEqual(state['counts']['pending'], 1)
        self.assertFalse(state['confirmation_known'])
        self.trading.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.sales), 1)
        self.trading._status = 'journal_error'
        self.trading._message = 'Steam 出售记录损坏；原文件保留，自动出售已暂停。'
        state = self.trading.state()
        self.assertEqual(state['status'], 'journal_error')
        self.assertIn('损坏', state['sale_message'])

    def test_status_does_not_wait_for_market_worker_guard(self):
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.cached = self.trading.state()
        for held_lock in (self.trading.guard, self.trading._operation, self.session.guard):
            with self.subTest(lock=held_lock):
                done, result = threading.Event(), []
                def read():
                    try: result.append(coordinator.state())
                    finally: done.set()
                with held_lock:
                    thread = threading.Thread(target=read, daemon=True)
                    thread.start()
                    completed_without_network_lock = done.wait(1)
                thread.join(2)
                self.assertTrue(completed_without_network_lock)
                self.assertTrue(result[0]['steam']['authenticated'])

    def test_old_confirmation_cache_is_hidden_after_relogin_or_logout(self):
        self.pending_sale()
        coordinator = AuctionCoordinator(self.root, self.trading,
            lambda: (False, deepcopy(DEFAULT_AUCTION), None), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.cached = self.trading.state()
        with patch('deskrawl_assistant.auction_service.time.time', return_value=self.now):
            self.assertTrue(coordinator.state()['sell']['confirmation_known'])
            self.session.capture_session(cookies())
            state = coordinator.state()
            self.assertTrue(state['steam']['authenticated'])
            self.assertFalse(state['sell']['confirmation_known'])
            self.assertEqual(state['sell']['confirmation_count'], 0)
            self.session.logout()
            self.assertFalse(coordinator.state()['steam']['authenticated'])
            self.assertFalse(coordinator.state()['steam']['bound'])


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
