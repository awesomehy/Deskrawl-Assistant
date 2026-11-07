"""Account-wide pending trades: discovery stays read-only, transactions are fenced."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deskrawl_assistant.steam_trading import SteamTradingService
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_listing_receipts import listing_prices
from tests.test_sale_tracking import TrackingSession, history
from tests.test_steam_trading import APP_ID, STEAMID, OTHER, catalog, inventory


class PendingTradesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.now = 1000
        self.session = TrackingSession()
        self.service = SteamTradingService(Path(self.tmp.name), catalog(), session=self.session, clock=lambda: self.now)

    def discovery(self, *, pending=False):
        action = 'CancelMarketListingConfirmation' if pending else 'RemoveMarketListing'
        prefix = 'mylisting_confirmation' if pending else 'mylisting'
        html = f'<div id="{prefix}_345"><span class="market_listing_price">\u00a520.00<br><span>(\u00a517.40)</span></span><a onclick="{action}(\'{prefix}\',\'345\',\'{APP_ID}\',\'2\',\'1\')">action</a></div>'
        response = {'success': 1, 'total_count': 1, 'results_html': html, 'assets': {
            str(APP_ID): {'2': {'1': {'appid': APP_ID, 'amount': '1', 'market_hash_name': 'Gold Bar',
                                      'marketable': 1, 'game_instance_id': 'external-guid'}}}}}
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/market/mylistings/'):
                self.session.requests.append((method,path)); return deepcopy(response)
            return original(method,path,data)
        self.session.request = request
        return response

    def test_steam_inventory_without_an_upload_record_is_visible_but_not_authorized_to_sell(self):
        self.session.html = ''
        self.service.refresh_confirmation()
        value = self.service.pending_trade_receipts(STEAMID)
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0]['status'], 'uploaded')
        self.assertEqual(self.service._journal, [])
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.pending_trade_receipts(OTHER), [])

    def test_external_listing_is_discovered_without_prior_journal_and_has_exact_price(self):
        self.discovery()
        self.service.refresh_confirmation()
        value = self.service.pending_trade_receipts(STEAMID)
        self.assertEqual(len(value), 1)
        self.assertEqual((value[0]['status'], value[0]['my_price']), ('listed', 'CNY 20.00'))
        self.assertTrue(value[0]['can_reprice'])
        self.assertTrue(self.service._journal[0]['manual_only'])
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(self.session.sales, [])
        restored = SteamTradingService(Path(self.tmp.name), catalog(), session=self.session, clock=lambda:self.now)
        stale = restored.pending_trade_receipts(STEAMID)[0]
        self.assertEqual((stale['status'], stale['previous_status']), ('uncertain', 'listed'))
        self.assertFalse(stale['can_reprice'])

    def test_manual_relist_is_discovered_even_with_the_old_cancel_fence(self):
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        old = self.service._journal[0]
        old.update(assetid='9', listingid='123', status='uncertain',
                   last_confirmed_status='listed', game_instance_id='external-guid',
                   reprice={'phase': 'cancel_submitted', 'started_at': self.now - 10})
        self.service._save()
        self.discovery()  # User independently relisted as asset 1 / listing 345.
        self.session.inventory = inventory()
        self.service.refresh_confirmation()
        receipts = self.service.pending_trade_receipts(STEAMID)
        visible = [r for r in receipts if not r.get('display_retired')]
        self.assertEqual(len(visible), 1)
        self.assertEqual((visible[0]['status'], visible[0]['my_price']), ('listed', 'CNY 20.00'))
        self.assertNotEqual(visible[0]['id'], old['id'])
        new = next(r for r in self.service._journal if r['id'] == visible[0]['id'])
        self.assertEqual((new['assetid'], new['listingid'], new['source']), ('1', '345', 'observed_market'))
        self.assertTrue(new['manual_only'])
        self.assertEqual(old['reprice']['phase'], 'cancel_submitted')
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertFalse(self.session.sales)

    def test_pending_then_listed_then_sold_and_transient_failure_keep_prior_status(self):
        response = self.discovery(pending=True)
        self.service.refresh_confirmation()
        self.assertEqual(self.service.pending_trade_receipts(STEAMID)[0]['status'], 'pending_confirmation')
        self.now += 6
        self.session.failure = SteamSessionError('temporary')
        original = self.session.request
        self.session.request = lambda *a, **k: (_ for _ in ()).throw(self.session.failure)
        self.service.refresh_confirmation()
        value = self.service.pending_trade_receipts(STEAMID)[0]
        self.assertEqual((value['status'], value['previous_status']), ('uncertain', 'pending_confirmation'))
        self.assertFalse(value['can_reprice'])
        self.session.request = original; self.session.failure = None
        response['results_html'] = self.session.html
        self.now += 6; self.service.refresh_confirmation()
        self.assertEqual(self.service.pending_trade_receipts(STEAMID)[0]['status'], 'listed')
        row = self.service._journal[0]
        self.session.history = history(row)
        response['results_html'] = ''
        self.now += 16; self.service.refresh_confirmation()
        self.assertEqual(self.service.pending_trade_receipts(STEAMID), [])

    def test_listings_refresh_after_five_seconds_not_every_ui_poll_and_honor_rate_limit(self):
        self.discovery(); self.service.refresh_confirmation()
        def requests(): return len([p for _,p in self.session.requests if p.startswith('/market/mylistings/')])
        self.assertEqual(requests(), 1)
        for _ in range(10): self.service.pending_trade_receipts(STEAMID)
        self.now += 4; self.service.refresh_confirmation(); self.assertEqual(requests(), 1)
        self.now += 1; self.service.refresh_confirmation(); self.assertEqual(requests(), 2)
        self.service._limited(SteamSessionError('limited', status=429, retry_after=600))
        self.now += 100; self.service.refresh_confirmation(); self.assertEqual(requests(), 2)

    def test_failed_inventory_read_retains_previous_state_with_verification_marker(self):
        self.session.html = ''; self.service.refresh_confirmation()
        self.now += 31; self.session.failure = SteamSessionError('failed')
        self.service.refresh_confirmation()
        value = self.service.pending_trade_receipts(STEAMID)[0]
        # Even if the listing read fails before inventory refresh, its old
        # inventory observation expires and cannot masquerade as current.
        self.now += 16
        value = self.service.pending_trade_receipts(STEAMID)[0]
        self.assertEqual((value['status'], value['previous_status']), ('uncertain', 'uploaded'))

    def test_inventory_failure_does_not_erase_a_successful_listing_verification(self):
        self.discovery(pending=True)
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/inventory/'):
                raise SteamSessionError('inventory unavailable')
            return original(method, path, data)
        self.session.request = request
        self.service.refresh_confirmation()
        self.assertTrue(self.service.state()['confirmation_known'])
        self.assertEqual(self.service.state()['pending_count'], 1)
        self.assertEqual(self.service.pending_trade_receipts(STEAMID)[0]['status'], 'pending_confirmation')
        self.assertFalse(any(path.startswith('/market/myhistory/') for _, path in self.session.requests))

    def test_logout_retains_last_inventory_stage_for_its_owner_without_sale_permission(self):
        self.session.html = ''; self.service.refresh_confirmation()
        self.session.wallet['steamid'] = OTHER
        value = self.service.pending_trade_receipts(STEAMID)[0]
        self.assertEqual((value['status'], value['previous_status']), ('uncertain', 'uploaded'))
        self.assertFalse(value['can_reprice'])
        self.assertEqual(self.service.pending_trade_receipts(OTHER), [])

    def test_account_switch_during_listing_query_never_adopts_response_into_old_account(self):
        self.discovery()
        original = self.session.request
        def request(method, path, data=None):
            response = original(method, path, data)
            if path.startswith('/market/mylistings/'):
                self.session.wallet['steamid'] = OTHER
            return response
        self.session.request = request
        self.service.refresh_confirmation()
        self.assertEqual(self.service._journal, [])
        self.assertFalse(self.service.state()['confirmation_known'])
        self.assertEqual(self.session.sales, [])

    def test_ui_uses_nonblocking_auth_snapshot_while_a_steam_operation_holds_the_session(self):
        self.discovery(pending=True); self.service.refresh_confirmation()
        self.session.peek_state = lambda: None
        with patch.object(self.session, 'state', side_effect=AssertionError('blocking auth read')):
            value = self.service.pending_trade_receipts(STEAMID)[0]
        self.assertEqual((value['status'], value['previous_status']), ('uncertain', 'pending_confirmation'))
        self.assertFalse(value['can_reprice'])

    def test_parser_uses_buyer_total_and_rejects_unqualified_or_wrong_currency(self):
        document = '<div id="mylisting_345"><span class="market_listing_price">\u00a520.00<br><span>(\u00a517.40)</span></span></div>'
        self.assertEqual(listing_prices(document, 'CNY'), {'345': 2000})
        self.assertEqual(listing_prices(document, 'USD'), {})
        self.assertEqual(listing_prices(document.replace('\u00a520.00', '20.00'), 'CNY'), {})

    def test_current_inventory_and_listing_evidence_requires_both_complete_views_and_same_login(self):
        self.discovery(); self.service.refresh_confirmation()
        value = self.service.current_trade_evidence(STEAMID)
        self.assertTrue(value['complete'])
        self.assertEqual(value['checked_at'], self.now)
        self.assertTrue(value['items'])
        self.assertFalse(self.service.current_trade_evidence(OTHER)['complete'])
        self.now += 13
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])
        self.service.refresh_confirmation()
        self.assertTrue(self.service.current_trade_evidence(STEAMID)['complete'])
        original = self.session.request
        self.now += 5
        self.session.request = lambda *a, **k: (_ for _ in ()).throw(SteamSessionError('temporary'))
        self.service.refresh_confirmation()
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])
        self.session.request = original
        self.now += 6; self.service.refresh_confirmation()
        self.session.wallet['steamid'] = OTHER
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])

    def test_missing_listing_metadata_is_not_proof_of_empty_current_inventory(self):
        self.session.html = '<a onclick="RemoveMarketListing(\'mylisting\',\'345\',\'%s\',\'2\',\'9\')">active</a>' % APP_ID
        self.service.refresh_confirmation()
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])
        self.assertEqual(self.session.sales, [])

    def test_unparsed_nonempty_listing_and_unknown_inventory_are_not_negative_evidence(self):
        response = self.discovery()
        response['results_html'] = '<div id="mylisting_345">unknown action layout</div>'
        self.service.refresh_confirmation()
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])
        response['total_count'] = 0
        response['results_html'] = ''
        self.session.inventory['descriptions'][0]['market_hash_name'] = 'Unknown new item'
        self.now += 31
        self.service.refresh_confirmation()
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])


if __name__ == '__main__': unittest.main()
