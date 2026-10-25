"""Crash, account change and delayed-response regressions; no real writes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from deskrawl_assistant.auction import next_action
from deskrawl_assistant.auction_runtime import AuctionRuntime
from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.steam_listing_receipts import listing_receipts
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from test_auction import item, observation
from test_auction_coordinator_audit import TradingFake, STEAMID, OTHER
from test_steam_trading import FakeSession, catalog, inventory


class UploadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.session = FakeSession()
        self.now = 1000
        self.service = self.reopen()

    def reopen(self):
        return SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)

    def test_recent_saved_baseline_resumes_exact_new_asset_after_restart(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        recovered = self.reopen()
        recovered.complete_upload(ticket)
        self.assertEqual([r['assetid'] for r in recovered._journal], ['2'])
        self.assertEqual(self.reopen()._tickets, {})
        self.assertEqual(self.session.sales, [])

    def test_expired_baseline_and_clock_rollback_cannot_adopt_assets(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        for now in (999, 1600):
            self.now = now
            recovered = self.reopen()
            with self.assertRaises(SteamSessionError):
                recovered.complete_upload(ticket)
            self.assertEqual(recovered._journal, [])

    def test_ambiguous_delta_remains_ambiguous_after_restart(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}), ('3', 1, {}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket)
        self.session.inventory = inventory(('1', 1, {}), ('3', 1, {}))
        with self.assertRaises(SteamSessionError):
            self.reopen().complete_upload(ticket)

    def test_crash_between_registration_and_ticket_removal_does_not_duplicate(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        baseline = deepcopy(self.service._tickets[ticket])
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        self.service.complete_upload(ticket)
        # Simulate the persisted state at _register's commit boundary.
        self.service._tickets[ticket] = baseline
        self.service._save()
        recovered = self.reopen()
        recovered.complete_upload(ticket)
        self.assertEqual(len(recovered._journal), 1)

    def test_confirmed_native_intent_resumes_mapping_without_upload_or_sale(self):
        trading = TradingFake()
        intent = {'uid': 'local', 'instance_id': 'server', 'item_key': 'GoldBar', 'variant': 'normal',
                  'steamid': STEAMID, 'phase': 'confirmed', 'ticket': 'a' * 32}
        path = self.path / 'auction-upload-intents.json'
        path.write_text(json.dumps({'schema_version': 1, 'intents': {'known': intent}}), encoding='utf-8')
        coordinator = AuctionCoordinator(self.path, trading, lambda: (False, {}, STEAMID), start_worker=False)
        self.addCleanup(coordinator.close)
        coordinator.process_once()
        self.assertEqual(trading.calls, [('complete', 'a' * 32, 1, 'server')])
        self.assertEqual(coordinator.intents['known']['phase'], 'resolved')

    def test_native_receipt_rebuilds_a_lost_coordinator_completion_job(self):
        trading = TradingFake()
        coordinator = AuctionCoordinator(self.path, trading, lambda: (False, {}, STEAMID), start_worker=False)
        self.addCleanup(coordinator.close)
        row = {'item_uid': 'local', 'instance_id': 'server', 'internal_name': 'GoldBar'}
        identity = coordinator.submitted(row, STEAMID, 'a' * 32)
        receipt = {'steamid': STEAMID, 'uploaded_uid': 'local', 'uploaded_instance_id': 'server',
                   'uploaded_internal_name': 'GoldBar'}
        coordinator.observe({'steamid': OTHER, 'steamid_verified': True, 'confirmed_uploads': [receipt]})
        self.assertEqual(coordinator.intents[identity]['phase'], 'submitted')
        coordinator.observe({'steamid': STEAMID, 'steamid_verified': True, 'confirmed_uploads': [receipt]})
        coordinator.process_once()
        self.assertEqual(coordinator.intents[identity]['phase'], 'resolved')
        self.assertEqual(trading.calls, [('complete', 'a' * 32, 1, 'server')])

    def test_native_recovery_needs_same_account_exact_listing_and_complete_removal(self):
        runtime = AuctionRuntime(Mock(reader=Mock()), journal_path=self.path / 'native.json')
        runtime._pending = {'server': {'item_uid': 'local', 'internal_name': 'GoldBar', 'steamid': STEAMID}}
        state = {'steamid': STEAMID, 'listings': [{'instance_id': 'server', 'internal_name': 'GoldBar',
                 'listed_at': 20, 'pending': True, 'received': False}], 'listing_count': 1}
        empty = {'complete': True, 'containers': {'inventory': {'slots': []}}}
        runtime._reconcile({**state, 'steamid': OTHER}, empty)
        runtime._reconcile(state, {**empty, 'complete': False})
        runtime._reconcile(state, {'complete': True, 'containers': {'inventory': {'slots': [{'item_uid': 'local'}]}}})
        self.assertTrue(runtime._pending)
        runtime._reconcile(state, empty)
        self.assertFalse(runtime._pending)
        restarted = AuctionRuntime(Mock(reader=Mock()), journal_path=runtime.journal_path)
        self.assertEqual(restarted._confirmed['server']['uploaded_uid'], 'local')

    def test_equipped_item_repeated_in_observation_cannot_authorize_upload(self):
        row = item()
        obs = observation([row])
        obs['character'] = {'equipment': [dict(row)]}
        settings = {'upload_enabled': True, 'categories': ['divine_equipment']}
        obs['items'][0]['is_equipped'] = True
        self.assertIsNone(next_action(settings, obs))

    def test_pending_upload_may_refresh_but_cannot_submit_again(self):
        obs = observation([item()])
        obs['auction'].update(pending=True, needs_refresh=True)
        settings = {'upload_enabled': True, 'categories': ['divine_equipment'], 'retain_count': 0}
        self.assertEqual(next_action(settings, obs)['kind'], 'auction_open')
        obs['auction']['needs_refresh'] = False
        self.assertIsNone(next_action(settings, obs))


class ListingRecoveryTests(unittest.TestCase):
    @staticmethod
    def html(asset='1', listing='345', app='4623570', pending=False):
        function = 'CancelMarketListingConfirmation' if pending else 'RemoveMarketListing'
        prefix = 'mylisting_confirmation' if pending else 'mylisting'
        return f'''<a onclick="{function}('{prefix}', '{listing}', '{app}', '2', '{asset}');">action</a>'''

    def test_parser_ignores_text_and_different_games_and_distinguishes_confirmation(self):
        self.assertEqual(listing_receipts(self.html())['345']['assetid'], '1')
        self.assertTrue(listing_receipts(self.html(pending=True))['345']['pending'])
        self.assertEqual(listing_receipts(self.html(app='730')), {})
        self.assertEqual(listing_receipts('<p>RemoveMarketListing("mylisting",345,4623570,2,1)</p>'), {})
        with self.assertRaises(SteamSessionError):
            listing_receipts(self.html() + self.html(asset='2'))

    def test_only_exact_active_listing_resolves_a_pending_order(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession()
            service = SteamTradingService(Path(directory), catalog(), session=session, clock=lambda: 1000)
            service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
            session.result = {'success': 1, 'requires_confirmation': True, 'listingid': '345'}
            config = {'sell_enabled': True, 'categories': ['gold_bar']}
            service.sale_once(config, STEAMID)
            request = session.request
            page = [self.html(asset='9')]
            session.request = lambda method, path, data=None: (
                {'success': 1, 'total_count': 1, 'results_html': page[0]}
                if path.startswith('/market/mylistings/') else request(method, path, data))
            for html, expected in ((self.html(asset='9'), 'pending'), (self.html(listing='346'), 'pending'),
                                   (self.html(pending=True), 'pending'), ('', 'pending'), (self.html(), 'listed')):
                page[0] = html
                service._reconcile_after = 0
                service.sale_once(config, STEAMID)
                self.assertEqual(service._journal[0]['status'], expected)
            self.assertEqual(len(session.sales), 1)

    def test_idle_login_verification_does_not_read_inventory_or_place_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession()
            session.state = lambda: {'bound': True, 'verified': False}
            session.verify = Mock(return_value=session.wallet)
            service = SteamTradingService(Path(directory), catalog(), session=session, clock=lambda: 1000)
            service.refresh_auth()
            service.refresh_auth()
            session.verify.assert_called_once()
            self.assertEqual(session.requests, [])
            self.assertEqual(session.sales, [])
