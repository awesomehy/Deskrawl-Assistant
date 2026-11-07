from copy import deepcopy
import unittest
from unittest.mock import patch

from deskrawl_assistant.steam_return_receipts import inventory_history, history_anchor, cancellation_return
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from tests import test_sale_tracking
from tests.test_steam_trading import STEAMID, OTHER, APP_ID, inventory, catalog
from tests.steam_history_fixture import BASELINE, event, history_page


class HistoryTests(unittest.TestCase):
    def test_actual_markup_structure_and_new_asset_return(self):
        rows = inventory_history(history_page(event(2), BASELINE), STEAMID)
        self.assertTrue(cancellation_return(rows, history_anchor(rows[1]), '42', '0', '2', 1))
        for changed in ('Earned in Deskrawl: Idle ARPG', 'You listed an item on the Community Market.'):
            rows = inventory_history(history_page(event(2, description=changed), BASELINE), STEAMID)
            self.assertFalse(cancellation_return(rows, history_anchor(rows[1]), '42', '0', '2', 1))

    def test_old_or_ambiguous_history_does_not_prove_cancellation(self):
        base = inventory_history(history_page(BASELINE), STEAMID)[0]
        for rows in ([base], inventory_history(history_page(event(2)), STEAMID),
                     inventory_history(history_page(event(3), event(2), BASELINE), STEAMID),
                     inventory_history(history_page(event(2, amount=2), BASELINE), STEAMID)):
            self.assertFalse(cancellation_return(rows, history_anchor(base), '42', '0', '2', 1))

    def test_malformed_or_foreign_account_evidence_fails_closed(self):
        for document in (b'\xff', '<html>login</html>', history_page(event(2, steamid=OTHER), BASELINE),
                         history_page(event(2), event(2)), history_page(BASELINE)[:-6]):
            with self.subTest(document=document[:70]), self.assertRaises(SteamSessionError):
                inventory_history(document, STEAMID)


class ChangedAssetRepriceTests(unittest.TestCase):
    setUp = test_sale_tracking.SaleTrackingTests.setUp
    class_identity_listing = test_sale_tracking.SaleTrackingTests.class_identity_listing

    def configure_return(self, events=None, *, receipt={}):
        self.class_identity_listing()
        self.session.inventory = inventory()
        original_request, original_post = self.session.request, self.session.post
        self.events = [event(2)] if events is None else events
        def request(method, path, data=None):
            if '/inventoryhistory/' in path:
                return history_page(*(self.events if self.session.cancelled else []), BASELINE)
            return original_request(method, path, data)
        def post(path, data, **kwargs):
            result = original_post(path, data, **kwargs)
            return receipt if path.startswith('/market/removelisting/') else result
        self.session.request, self.session.post = request, post
        return self.service.reprice_preview(self.row['id'])['token']

    def test_changed_asset_without_guid_uses_cancellation_proof_and_exact_user_price(self):
        token = self.configure_return()
        self.service.reprice(token, '13.11')
        self.assertEqual([path for path, _ in self.session.sales], ['/market/removelisting/345', '/market/sellitem/'])
        self.assertEqual(self.session.sales[-1][1], {'appid': APP_ID, 'contextid': '2', 'assetid': '2', 'amount': 1, 'price': 1140})
        self.assertEqual(self.row['buyer_total'], 1311)
        self.assertEqual(self.row['previous_listings'][0]['assetid'], '1')
        with self.assertRaises(SteamSessionError):
            self.service.reprice(token, '13.11')
        self.assertEqual(len(self.session.sales), 2)

    def test_concurrent_same_class_acquisition_or_cancellation_never_sells(self):
        for events in ([event(2, description='Earned in Deskrawl: Idle ARPG')],
                       [event(2), event(3, description='Earned in Deskrawl: Idle ARPG')],
                       [event(2), event(3)], [event(2, assetid='999')], []):
            with self.subTest(events=len(events)):
                self.setUp()
                token = self.configure_return(events)
                with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
                    self.service.reprice(token, '13.11')
                self.assertEqual(len(self.session.sales), 1)

    def test_lost_sell_reply_never_replays_after_restart(self):
        token = self.configure_return()
        original = self.session.post
        def post(path, data, **kwargs):
            result = original(path, data, **kwargs)
            if path == '/market/sellitem/': raise SteamSessionError('response lost')
            return result
        self.session.post = post
        with self.assertRaises(SteamSessionError): self.service.reprice(token, '13.11')
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restored._journal[0]['reprice']['phase'], 'sale_submitted')
        restored.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(len(self.session.sales), 2)

    def test_failed_history_preflight_does_not_cancel_listing(self):
        token = self.configure_return()
        request = self.session.request
        self.session.request = lambda m, p, data=None: '<html>login</html>' if '/inventoryhistory/' in p else request(m,p,data)
        with self.assertRaises(SteamSessionError): self.service.reprice(token, '13.11')
        self.assertEqual(self.session.sales, [])

    def test_asset_changes_during_final_check_never_sells(self):
        token = self.configure_return()
        original = self.session.request
        reads = 0
        def request(method, path, data=None):
            nonlocal reads
            if self.session.cancelled and path.startswith('/inventory/'):
                reads += 1
                if reads >= 2: return inventory()
            return original(method,path,data)
        self.session.request = request
        with self.assertRaises(SteamSessionError): self.service.reprice(token, '13.11')
        self.assertEqual(len(self.session.sales), 1)

    def test_other_same_class_order_must_remain_unchanged(self):
        for other_disappears in (False, True):
            with self.subTest(other_disappears=other_disappears):
                self.setUp()
                token = self.configure_return()
                request = self.session.request
                def read(method, path, data=None):
                    result = request(method, path, data)
                    if path.startswith('/market/mylistings/') and (not self.session.cancelled or not other_disappears):
                        result['total_count'] += 1
                        result['results_html'] += (f"<a onclick=\"RemoveMarketListing('mylisting','456','{APP_ID}','2','9')\">other</a>")
                        assets = result.setdefault('assets', {}).setdefault(str(APP_ID), {}).setdefault('2', {})
                        assets['9'] = deepcopy(inventory()['descriptions'][0])
                    return result
                self.session.request = read
                with patch('deskrawl_assistant.sale_tracking.time.sleep'):
                    if other_disappears:
                        with self.assertRaises(SteamSessionError): self.service.reprice(token, '13.11')
                    else:
                        self.service.reprice(token, '13.11')
                self.assertEqual(len(self.session.sales), 1 if other_disappears else 2)

    def test_player_stop_after_cancel_does_not_submit_new_sale(self):
        token = self.configure_return()
        with self.assertRaises(SteamSessionError):
            self.service.reprice(token, '13.11', validate=lambda: not self.session.cancelled)
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])

    def test_return_history_from_changed_account_is_not_evidence(self):
        token = self.configure_return()
        request = self.session.request
        def read(method, path, data=None):
            result = request(method, path, data)
            if '/inventoryhistory/' in path and self.session.cancelled:
                self.session.wallet['steamid'] = OTHER
            return result
        self.session.request = read
        with self.assertRaises(SteamSessionError): self.service.reprice(token, '13.11')
        self.assertEqual(len(self.session.sales), 1)
