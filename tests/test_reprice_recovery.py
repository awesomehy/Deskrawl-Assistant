"""Delayed cancellation receipts; all inventory, sessions and POSTs are fakes."""
from copy import deepcopy
from unittest.mock import patch
import threading
import unittest

from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.sale_tracking import RepriceDeferred, REPRICE_RECOVERY_TTL
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from tests import test_steam_return_receipts as return_tests
from tests.test_steam_trading import STEAMID, OTHER, catalog, inventory, APP_ID
from tests.steam_history_fixture import event


class RepriceRecoveryTests(unittest.TestCase):
    setUp = return_tests.ChangedAssetRepriceTests.setUp
    class_identity_listing = return_tests.ChangedAssetRepriceTests.class_identity_listing
    configure_return = return_tests.ChangedAssetRepriceTests.configure_return

    def defer(self, *, validate=None):
        token = self.configure_return([])
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(RepriceDeferred):
            self.service.reprice(token, '13.11', validate=validate)
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])
        self.assertTrue(self.service.reprice_recovery_state(self.row['id'])['waiting'])
        return token

    def ready(self):
        self.now += 61
        self.events = [event(2)]

    def test_late_history_resumes_exact_new_asset_and_original_user_price_once(self):
        token = self.defer()
        self.ready()
        self.session.quote = 'a new quote must never change the confirmed price'
        self.assertTrue(self.service.resume_reprices())
        self.assertEqual(self.session.sales[-1], ('/market/sellitem/', {
            'appid': APP_ID, 'contextid': '2', 'assetid': '2', 'amount': 1, 'price': 1140}))
        self.assertEqual(self.row['buyer_total'], 1311)
        self.assertNotIn('reprice', self.row)
        self.assertFalse(self.service.reprice_recovery_state(self.row['id']).get('waiting'))
        for _ in range(3):
            self.now += 61
            self.assertFalse(self.service.resume_reprices())
        with self.assertRaises(SteamSessionError): self.service.reprice(token, '2.30')
        self.assertEqual(len(self.session.sales), 2)

    def test_inventory_return_itself_can_arrive_after_the_immediate_window(self):
        token = self.configure_return()
        original = self.session.request
        visible = False
        def request(method, path, data=None):
            if self.session.cancelled and path.startswith('/inventory/') and not visible:
                return inventory()
            return original(method, path, data)
        self.session.request = request
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(RepriceDeferred):
            self.service.reprice(token, '13.11')
        visible = True
        self.now += 61
        self.assertTrue(self.service.resume_reprices())
        self.assertEqual(len(self.session.sales), 2)

    def test_lost_cancel_reply_recovers_without_repeating_cancellation(self):
        token = self.configure_return([])
        original = self.session.post
        def post(path, data, **kwargs):
            response = original(path, data, **kwargs)
            if path.startswith('/market/removelisting/'):
                raise SteamSessionError('cancel reply lost')
            return response
        self.session.post = post
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(RepriceDeferred):
            self.service.reprice(token, '13.11')
        self.ready()
        self.service.resume_reprices()
        self.assertEqual([p for p, _ in self.session.sales],
                         ['/market/removelisting/345', '/market/sellitem/'])
        self.assertEqual(self.session.sales[-1][1]['price'], 1140)

    def test_explicit_cancel_rejection_is_not_adopted_for_recovery(self):
        token = self.configure_return([], receipt=False)
        with self.assertRaises(SteamSessionError): self.service.reprice(token, '13.11')
        self.ready()
        self.assertFalse(self.service.resume_reprices())
        self.assertFalse(self.service._reprice_recoveries)
        self.assertEqual(len(self.session.sales), 1)

    def test_unchanged_original_asset_can_also_recover_without_game_guid(self):
        self.defer()
        self.session.inventory = inventory(('1', 1, {}))
        self.ready()
        self.service.resume_reprices()
        self.assertEqual(self.session.sales[-1][1]['assetid'], '1')
        self.assertEqual(len(self.session.sales), 2)

    def test_missing_history_rechecks_are_bounded_and_never_sleep_on_worker(self):
        self.defer()
        count = len(self.session.requests)
        self.assertFalse(self.service.resume_reprices())  # not due
        self.assertEqual(len(self.session.requests), count)
        self.now += 5
        with patch('deskrawl_assistant.sale_tracking.time.sleep', side_effect=AssertionError('worker slept')):
            self.assertTrue(self.service.resume_reprices())
        count = len(self.session.requests)
        self.now += 9
        self.assertFalse(self.service.resume_reprices())
        self.assertEqual(len(self.session.requests), count)
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.row['reprice']['verification'], 'return_identity')

    def test_transient_inventory_or_history_error_keeps_original_intent(self):
        for kind in ('/inventory/', '/inventoryhistory/'):
            with self.subTest(kind=kind):
                self.setUp()
                self.defer()
                self.ready()
                original = self.session.request
                def request(method, path, data=None):
                    if kind in path: raise SteamSessionError('temporary read failure')
                    return original(method, path, data)
                self.session.request = request
                self.service.resume_reprices()
                self.assertTrue(self.service.reprice_recovery_state(self.row['id'])['waiting'])
                self.assertEqual(len(self.session.sales), 1)
                self.session.request = original
                self.now += 61
                self.service.resume_reprices()
                self.assertEqual(len(self.session.sales), 2)

    def test_steam_backoff_is_respected_before_any_more_reads(self):
        self.defer()
        self.ready()
        original = self.session.request
        def request(method, path, data=None):
            raise SteamSessionError('limited', status=429, retry_after=300)
        self.session.request = request
        self.service.resume_reprices()
        self.session.request = original
        with patch.object(self.session, 'request', side_effect=AssertionError('backoff bypassed')):
            self.now += 299
            self.assertFalse(self.service.resume_reprices())
        self.now += 1
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 2)

    def test_original_order_must_disappear_in_a_complete_view(self):
        self.defer()
        self.ready()
        self.session.html = f'<a onclick="RemoveMarketListing(\'mylisting\',\'345\',\'{APP_ID}\',\'2\',\'1\')">active</a>'
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.row['reprice']['verification'], 'listing_removal')
        self.session.html = ''
        self.now += 61
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 2)

    def test_incomplete_listing_read_cannot_authorize_relisting(self):
        self.defer()
        self.ready()
        original = self.session.request
        def request(method, path, data=None):
            value = original(method, path, data)
            if path.startswith('/market/mylistings/'): value['total_count'] = 1
            return value
        self.session.request = request
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 1)
        self.session.request = original
        self.now += 61
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 2)

    def test_concurrent_same_class_history_never_grants_sale_permission(self):
        self.defer()
        self.ready()
        self.events.append(event(3, description='Earned in Deskrawl: Idle ARPG'))
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 1)

    def test_multiple_returns_and_conflicting_game_guid_end_recovery(self):
        for rows in ((('2', 1, {}), ('3', 1, {})), (('2', 2, {}),),
                     (('2', 1, {'game_instance_id': 'other-item'}),)):
            with self.subTest(rows=rows):
                self.setUp()
                self.defer()
                self.ready()
                self.session.inventory = inventory(*rows)
                self.service.resume_reprices()
                self.assertFalse(self.service._reprice_recoveries)
                self.session.inventory = inventory(('2', 1, {}))
                self.now += 61
                self.assertFalse(self.service.resume_reprices())
                self.assertEqual(len(self.session.sales), 1)

    def test_same_name_with_different_description_is_never_relisted(self):
        self.defer()
        self.ready()
        self.session.inventory['descriptions'][0]['descriptions'] = [{'value': 'different stats'}]
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 1)

    def test_account_generation_or_game_context_change_revokes_recovery(self):
        for kind in ('account', 'generation', 'character', 'closed', 'expired'):
            with self.subTest(kind=kind):
                self.setUp()
                active = True
                self.defer(validate=lambda: active)
                self.ready()
                old_state = self.session.state
                if kind == 'account': self.session.wallet['steamid'] = OTHER
                if kind == 'generation':
                    self.session.wallet['binding_generation'] = 2
                    self.session.state = lambda: {**old_state(), 'binding_generation': 2}
                if kind == 'character': active = False
                if kind == 'closed': self.service.close()
                if kind == 'expired': self.session.state = lambda: {**old_state(), 'expired': True}
                with patch.object(self.session, 'request', side_effect=AssertionError('revoked task read Steam')):
                    self.service.resume_reprices()
                self.assertFalse(self.service._reprice_recoveries)
                self.assertEqual(len(self.session.sales), 1)

    def test_temporary_unverified_login_can_recover_but_explicit_401_cannot(self):
        self.defer()
        self.ready()
        state = self.session.state
        self.session.state = lambda: {**state(), 'verified': False}
        self.assertFalse(self.service.resume_reprices())
        self.session.state = state
        with patch.object(self.session, 'request', side_effect=SteamSessionError('expired', status=401)):
            self.service.resume_reprices()
        self.assertFalse(self.service._reprice_recoveries)
        self.now += 61
        self.assertFalse(self.service.resume_reprices())
        self.assertEqual(len(self.session.sales), 1)

    def test_missing_wallet_identity_or_busy_session_is_waiting_not_revocation(self):
        for temporary in (None, {'bound': True, 'verified': False, 'steamid': None, 'binding_generation': 1}):
            with self.subTest(auth=temporary):
                self.setUp()
                self.defer()
                self.ready()
                with patch.object(self.service, 'auth_state', return_value=temporary), \
                        patch.object(self.session, 'request', side_effect=AssertionError('unverified read Steam')):
                    self.assertFalse(self.service.resume_reprices())
                self.assertTrue(self.service._reprice_recoveries)
                self.service.resume_reprices()
                self.assertEqual(len(self.session.sales), 2)

    def test_currency_fees_and_original_price_changes_never_reprice_silently(self):
        for kind in ('currency', 'fees', 'publisher_fee', 'price'):
            with self.subTest(kind=kind):
                self.setUp()
                self.defer()
                self.ready()
                if kind == 'currency': self.session.wallet['currency_id'] = 1
                if kind == 'fees': self.session.wallet['wallet']['wallet_fee_percent'] = '0'
                if kind == 'publisher_fee': self.session.inventory['descriptions'][0]['market_fee'] = '.15'
                if kind == 'price': self.row['reprice']['buyer_total'] = 230
                self.service.resume_reprices()
                self.assertFalse(self.service._reprice_recoveries)
                self.assertEqual(len(self.session.sales), 1)

    def test_preexisting_same_class_inventory_is_not_used_as_the_return(self):
        token = self.configure_return([])
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/inventory/'):
                return inventory(('9', 1, {}), *(('2', 1, {}),) if self.session.cancelled else ())
            return original(method, path, data)
        self.session.request = request
        self.service._observe_steam_inventory(STEAMID)
        earlier_preview = self.service.manual_sale_preview('inventory-9', STEAMID)['token']
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(RepriceDeferred):
            self.service.reprice(token, '13.11')
        self.ready()
        with self.assertRaises(SteamSessionError):
            self.service.manual_sale(earlier_preview, '2.30', expected_steamid=STEAMID)
        self.service.resume_reprices()
        self.assertEqual(self.session.sales[-1][1]['assetid'], '2')
        self.assertEqual(len(self.session.sales), 2)

    def test_character_changes_during_final_inventory_read_never_sells(self):
        active = True
        self.defer(validate=lambda: active)
        self.ready()
        original, reads = self.session.request, 0
        def request(method, path, data=None):
            nonlocal reads, active
            if path.startswith('/inventory/'):
                reads += 1
                if reads == 2: active = False
            return original(method, path, data)
        self.session.request = request
        self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 1)
        self.assertFalse(self.service._reprice_recoveries)

    def test_lost_relisting_reply_is_fenced_across_retries_and_restart(self):
        self.defer()
        self.ready()
        self.session.result = SteamSessionError('sell reply lost')
        self.service.resume_reprices()
        self.assertEqual(self.row['reprice']['phase'], 'sale_submitted')
        self.assertEqual(self.row['assetid'], '2')
        for _ in range(2):
            self.now += 61
            self.assertFalse(self.service.resume_reprices())
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertFalse(restored.resume_reprices())
        restored.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(len(self.session.sales), 2)

    def test_failed_durable_sale_fence_never_sends_or_replays_sale(self):
        self.defer()
        self.ready()
        save = self.service._save
        def fail_sale_fence():
            if self.row.get('reprice', {}).get('phase') == 'sale_submitted':
                raise OSError('journal disk full')
            return save()
        with patch.object(self.service, '_save', side_effect=fail_sale_fence):
            with self.assertRaises(OSError): self.service.resume_reprices()
        self.assertEqual(len(self.session.sales), 1)
        self.now += 61
        self.assertFalse(self.service.resume_reprices())
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertFalse(restored.resume_reprices())
        self.assertEqual(len(self.session.sales), 1)

    def test_parallel_worker_pass_cannot_submit_the_same_return_twice(self):
        self.defer()
        self.ready()
        entered, release = threading.Event(), threading.Event()
        original = self.session.post
        def post(path, data, **kwargs):
            if path == '/market/sellitem/':
                entered.set()
                if not release.wait(2): raise AssertionError('test did not release sale')
            return original(path, data, **kwargs)
        self.session.post = post
        errors = []
        def work():
            try: self.service.resume_reprices()
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(self.service.resume_reprices())
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(self.session.sales), 2)

    def test_logout_revokes_the_original_pending_authorization(self):
        self.defer()
        self.ready()
        self.service.logout()
        with patch.object(self.session, 'request', side_effect=AssertionError('logged-out recovery read Steam')):
            self.assertFalse(self.service.resume_reprices())
        self.assertFalse(self.service._reprice_reservations)
        self.assertEqual(len(self.session.sales), 1)

    def test_restart_or_expired_authorization_never_adopts_cancel_as_a_new_sale(self):
        self.defer()
        self.ready()
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertFalse(restored.resume_reprices())
        self.now += REPRICE_RECOVERY_TTL
        self.service.resume_reprices()
        self.assertFalse(self.service._reprice_recoveries)
        self.assertEqual(len(self.session.sales), 1)

    def test_same_class_other_sale_is_blocked_while_original_return_is_reserved(self):
        self.defer()
        self.ready()
        self.service._observe_steam_inventory(STEAMID)
        current = next(r for r in self.service.pending_trade_receipts(STEAMID) if r['id'] == 'inventory-2')
        self.assertFalse(current['sale_available'])
        with self.assertRaises(SteamSessionError): self.service.manual_sale_preview('inventory-2', STEAMID)
        other = deepcopy(self.row)
        other.update(id='b' * 32, status='captured', assetid='2')
        other.pop('reprice')
        self.service._journal.append(other)
        self.service._save()
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(len(self.session.sales), 1)
        self.service.resume_reprices()  # Another journal already owns asset 2.
        self.assertEqual(len(self.session.sales), 1)
        self.assertFalse(self.service._reprice_recoveries)

    def test_same_manual_worker_runs_recovery_with_managed_sales_disabled(self):
        self.defer()
        self.ready()
        coordinator = AuctionCoordinator(self.path, self.service, lambda: (False, {}, STEAMID), start_worker=False)
        self.addCleanup(coordinator.close)
        with patch.object(self.service, 'refresh_confirmation') as optional:
            coordinator.process_once()
        optional.assert_not_called()
        self.assertEqual(len(self.session.sales), 2)

    def test_recovery_projection_never_waits_for_network_or_transaction_lock(self):
        self.defer()
        with patch.object(self.session, 'request', side_effect=AssertionError('projection network')):
            result = self.service.reprice_recovery_state(self.row['id'])
        self.assertTrue(result['waiting'])
        self.assertIn('13.11', result['message'])
