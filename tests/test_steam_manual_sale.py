"""No Steam writes: exact-asset manual-sale boundaries exercised with fakes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deskrawl_assistant.auction_tracking import AuctionTrackingMixin
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from tests.test_steam_trading import FakeSession, STEAMID, OTHER, catalog, inventory, listing


class ManualSaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.now = 1000
        self.session = FakeSession()
        self.session.wallet['binding_generation'] = 1
        self.service = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.service._observe_steam_inventory(STEAMID)
        self.service._tracking_quote(STEAMID, {'item_key': 'GoldBar', 'variant': 'normal'})

    def preview(self):
        return self.service.manual_sale_preview('inventory-1', STEAMID)

    def submit(self, value=None, price='13.11', **kwargs):
        return self.service.manual_sale((value or self.preview())['token'], price,
                                        expected_steamid=STEAMID, **kwargs)

    def test_prices_default_to_ask_and_only_explicit_confirmation_submits_once(self):
        value = self.preview()
        self.assertEqual((value['default_price'], value['lowest_price'], value['highest_price']),
                         ('2.30', 'CNY 2.30', 'CNY 2.27'))
        self.assertEqual(self.session.sales, [])
        self.assertTrue(self.service.pending_trade_receipts(STEAMID)[0]['sale_available'])
        self.submit(value)
        self.assertEqual(self.session.sales, [('/market/sellitem/',
            {'appid': 4623570, 'contextid': '2', 'assetid': '1', 'amount': 1, 'price': 1140})])
        row = self.service._journal[0]
        self.assertEqual((row['buyer_total'], row['status'], row['manual_only']), (1311, 'listed', True))
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.assertEqual(len(self.session.sales), 1)

    def test_fee_guidance_is_local_scoped_and_uses_item_fee_without_relaxing_submission(self):
        before = list(self.session.requests)
        value = self.preview()
        self.assertEqual(value['fee_parameters']['steam'], '0.05')
        self.assertEqual(value['fee_parameters']['publisher'], '0.10')
        self.assertEqual(self.session.requests, before)
        self.service._observed_inventory['sale_assets']['1']['publisher_fee'] = '.25'
        self.assertEqual(self.preview()['fee_parameters']['publisher'], '0.25')
        self.service._fee_wallet['binding_generation'] = 2
        self.assertIsNone(self.preview()['fee_parameters'])
        with self.assertRaisesRegex(SteamSessionError, '12.62.*12.65'):
            self.submit(price='12.63')
        self.assertEqual(self.session.sales, [])

    def test_stale_exact_inventory_may_open_readonly_editor_but_account_changes_and_reservations_do_not(self):
        self.now += 61
        row = self.service.pending_trade_receipts(STEAMID)[0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertFalse(row['sale_available'])
        self.assertTrue(row['sale_preview_available'])
        before = list(self.session.requests)
        self.preview()
        self.assertEqual(self.session.requests, before)
        self.session.wallet['binding_generation'] = 2
        self.assertFalse(self.service.pending_trade_receipts(STEAMID)[0]['sale_preview_available'])
        with self.assertRaises(SteamSessionError): self.preview()
        self.session.wallet['binding_generation'] = 1
        with patch.object(self.service, '_reprice_reserved', return_value=True):
            self.assertFalse(self.service.pending_trade_receipts(STEAMID)[0]['sale_preview_available'])
        self.assertEqual(self.session.sales, [])

    def test_registered_capture_reuses_the_same_journal_row(self):
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        identity = self.service._journal[0]['id']
        self.assertTrue(self.service.pending_trade_receipts(STEAMID)[0]['sale_available'])
        value = self.service.manual_sale_preview(identity, STEAMID)
        self.session.result = {'success': 1, 'requires_confirmation': True}
        self.submit(value)
        self.assertEqual(len(self.service._journal), 1)
        self.assertEqual(self.service._journal[0]['status'], 'pending')

    def test_no_market_orders_allows_user_price_but_does_not_invent_quote(self):
        self.session.quote = listing(low=None, high=None)
        self.service._tracking_quote(STEAMID, {'item_key': 'GoldBar', 'variant': 'normal'})
        value = self.preview()
        self.assertEqual(value['default_price'], '')
        self.assertIsNone(value['lowest_price'])
        self.assertIsNone(value['highest_price'])
        self.submit(value)
        self.assertEqual(len(self.session.sales), 1)

    def test_invalid_prices_currency_and_expiry_never_sell(self):
        for price in ('0', '-1', 'NaN', 'Infinity', '1.001', '1.13', '99999999999'):
            with self.subTest(price=price), self.assertRaises(SteamSessionError):
                self.submit(price=price)
        value = self.preview()
        self.now += 121
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.service._inventory_observe_after = 0
        self.service._observe_steam_inventory(STEAMID)
        value = self.preview()
        self.session.wallet['currency_id'] = 1
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.assertEqual(self.session.sales, [])

    def test_unrelated_asset_same_name_and_stacked_asset_never_substitute(self):
        value = self.preview()
        self.session.inventory = inventory(('2', 1, {}))
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.session.inventory = inventory(('1', 2, {}))
        with self.assertRaises(SteamSessionError): self.submit()
        self.service._inventory_observe_after = 0
        self.service._observe_steam_inventory(STEAMID)
        with self.assertRaises(SteamSessionError): self.preview()
        self.assertEqual(self.session.sales, [])

    def test_failed_or_incomplete_listing_check_and_existing_order_never_sell(self):
        for response in ({'success': 1, 'total_count': 1, 'results_html': ''},
                         {'success': 1, 'total_count': 1, 'results_html':
                          "<a onclick=\"RemoveMarketListing('mylisting','345','4623570','2','1')\">x</a>"}):
            value = self.preview()
            request = self.session.request
            self.session.request = lambda m,p,d=None: response if p.startswith('/market/mylistings/') else request(m,p,d)
            with self.assertRaises(SteamSessionError): self.submit(value)
            self.session.request = request
        self.assertEqual(self.session.sales, [])

    def test_lost_reply_is_durable_and_cannot_be_sold_by_manual_or_auto_again(self):
        value = self.preview()
        self.session.result = SteamSessionError('response lost')
        with self.assertRaises(SteamSessionError): self.submit(value)
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        restored._observe_steam_inventory(STEAMID)
        with self.assertRaises(SteamSessionError): restored.manual_sale_preview('inventory-1', STEAMID)
        restored.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(restored._journal[0]['status'], 'uncertain')
        self.assertEqual(len(self.session.sales), 1)

    def test_account_switch_generation_player_stop_and_durable_fence_failure(self):
        value = self.preview()
        self.session.wallet['binding_generation'] = 2
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.session.wallet['binding_generation'] = 1
        value = self.preview()
        self.session.wallet['steamid'] = OTHER
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.session.wallet['steamid'] = STEAMID
        with self.assertRaises(SteamSessionError): self.submit(validate=lambda: False)
        value = self.preview()
        with patch.object(self.service, '_save', side_effect=OSError('disk')), self.assertRaises(OSError):
            self.submit(value)
        self.assertEqual(self.session.sales, [])

    def test_no_arbitrary_selection_and_preview_remains_local_while_worker_busy(self):
        for selection in ('1', 'inventory-2', '../x', 'a' * 32):
            with self.assertRaises(SteamSessionError): self.service.manual_sale_preview(selection, STEAMID)
        self.now += 61
        with self.service._operation, \
                patch.object(self.session, 'verify', side_effect=AssertionError('must stay local')), \
                patch.object(self.session, 'request', side_effect=AssertionError('must stay local')):
            value = self.preview()
        self.assertEqual(value['default_price'], '2.30')
        self.assertTrue(value['quote_pending'])
        self.service._retry_after = self.now + 10
        limited = self.preview()
        self.assertIn('限制请求', limited['quote_error'])
        self.assertFalse(limited['quote_pending'])
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.assertEqual(self.session.sales, [])

    def test_missing_quote_returns_immediately_then_worker_reads_ask_and_bid(self):
        self.service._tracking_quotes = {}
        before = list(self.session.requests)
        value = self.preview()
        self.assertEqual(self.session.requests, before)
        self.assertTrue(value['quote_pending'])
        self.assertFalse(value['quote_known'])
        self.assertEqual(value['default_price'], '')
        self.assertTrue(self.service.refresh_price_previews())
        value = self.preview()
        self.assertFalse(value['quote_pending'])
        self.assertTrue(value['quote_known'])
        self.assertEqual((value['lowest_price'], value['highest_price']), ('CNY 2.30', 'CNY 2.27'))
        self.assertEqual(self.session.sales, [])

    def test_failed_price_read_keeps_valid_cache_and_bounds_retries(self):
        original = dict(self.service._tracking_quotes[('GoldBar', 'normal')])
        self.now += 61
        self.preview()
        self.session.failure = SteamSessionError('private upstream payload')
        self.assertTrue(self.service.refresh_price_previews())
        value = self.preview()
        self.assertFalse(value['quote_pending'])
        self.assertEqual(value['default_price'], '2.30')
        self.assertNotIn('private', value['quote_error'])
        self.assertEqual(value['lowest_price_expires_at'], original['expires_at'])
        self.assertFalse(self.service.refresh_price_previews())
        self.now += 31
        self.preview()
        self.assertTrue(self.service.refresh_price_previews())
        self.assertEqual(self.session.sales, [])

    def test_queued_quote_dropped_on_account_or_generation_change(self):
        for field, changed in [('steamid', OTHER), ('binding_generation', 2)]:
            with self.subTest(field=field):
                self.service._tracking_quotes = {}
                self.session.wallet.update(steamid=STEAMID, binding_generation=1)
                self.preview()
                self.session.wallet[field] = changed
                before = list(self.session.requests)
                self.assertFalse(self.service.refresh_price_previews())
                self.assertEqual(self.session.requests, before)
                with self.assertRaises(SteamSessionError): self.preview()

    def test_stale_preview_cannot_sell_a_different_live_signature(self):
        value = self.preview()
        self.session.inventory['descriptions'][0]['owner_descriptions'] = [{'value': 'different item'}]
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.assertEqual(self.session.sales, [])

    def test_automatic_submission_between_preview_and_submit_prevents_second_post(self):
        value = self.preview()
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        self.service._journal[0]['status'] = 'intent'
        self.service._save()
        with self.assertRaises(SteamSessionError): self.submit(value)
        self.assertEqual(self.session.sales, [])


class ManualSaleAPITests(unittest.TestCase):
    setUp = ManualSaleTests.setUp

    def api(self):
        api = AuctionTrackingMixin()
        api.guard = threading.RLock()
        api.closed = threading.Event()
        api.updating = False
        api.client = SimpleNamespace(connected=True)
        api.pending_trade_context = {'steamid': STEAMID, 'read_at': time.monotonic(),
                                     'session': 'bridge', 'character_id': 'hero'}
        api.auction_coordinator = SimpleNamespace(trading=self.service, operation=threading.Lock(), wake=threading.Event())
        return api

    def test_api_rejects_arbitrary_payload_and_dispatches_one_background_sale(self):
        api = self.api()
        for data in ({}, {'item_id': '1'}, {'item_id': 'inventory-1', 'steamid': OTHER}):
            with self.assertRaises(ValidationError): api.auction_sale_preview(data)
        value = api.auction_sale_preview({'item_id': 'inventory-1'})
        with self.assertRaises(ValidationError): api.auction_sale_start({'token': value['token'], 'price': '1; x'})
        with api.auction_coordinator.operation:
            api.auction_sale_start({'token': value['token'], 'price': '13.11'})
            with self.assertRaises(ValidationError): api.auction_sale_start({'token': value['token'], 'price': '13.11'})
        self.assertTrue(api.auction_coordinator.wake.wait(3))
        self.assertEqual(len(self.session.sales), 1)
        self.assertFalse(api.auction_reprice_job['running'])

    def test_changed_character_while_queued_never_sells(self):
        api = self.api()
        value = api.auction_sale_preview({'item_id': 'inventory-1'})
        with api.auction_coordinator.operation:
            api.auction_sale_start({'token': value['token'], 'price': '13.11'})
            api.pending_trade_context['character_id'] = 'other hero'
        self.assertTrue(api.auction_coordinator.wake.wait(3))
        self.assertTrue(api.auction_reprice_job['error'])
        self.assertEqual(self.session.sales, [])
