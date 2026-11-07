"""Sale tracking uses positive exact receipts; all writes below are fakes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from deskrawl_assistant.auction import DEFAULT_AUCTION
from deskrawl_assistant.auction_service import AuctionCoordinator
from deskrawl_assistant.auction_tracking import AuctionTrackingMixin, observe_candidates
from deskrawl_assistant.models import ValidationError
from deskrawl_assistant.sale_tracking import RepriceDeferred, project_receipt
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import SteamTradingService
from tests.test_steam_trading import APP_ID, STEAMID, OTHER, FakeSession, catalog, inventory
from tests.steam_history_fixture import BASELINE, history_page


class TrackingSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.wallet['binding_generation'] = 1
        self.html = f'<a onclick="RemoveMarketListing(\'mylisting\',\'345\',\'{APP_ID}\',\'2\',\'1\')">active</a>'
        self.history = {'success': 1, 'total_count': 0, 'events': [], 'listings': {}, 'purchases': {}}
        self.cancel_failure = None
        self.return_guid = 'game-guid'
        self.cancelled = False

    def state(self):
        return {**super().state(), 'binding_generation': 1}

    def request(self, method, path, data=None):
        if '/inventoryhistory/' in path:
            return history_page(BASELINE)
        if path.startswith('/market/mylistings/'):
            self.requests.append((method, path))
            return {'success': 1, 'total_count': int(bool(self.html)), 'results_html': self.html}
        if path.startswith('/market/myhistory/'):
            self.requests.append((method, path))
            return deepcopy(self.history)
        return super().request(method, path, data)

    def post(self, path, data, **kwargs):
        if path.startswith('/market/removelisting/'):
            self.sales.append((path, deepcopy(data)))
            if self.cancel_failure:
                raise self.cancel_failure
            self.cancelled = True
            self.html = ''
            self.inventory = inventory(('2', 1, {'game_instance_id': self.return_guid}))
            return {'success': 1}
        return super().post(path, data, **kwargs)


def history(row, *, receive=107, currency='2023', purchaser=OTHER, event_type=3, assetid='1', failed=0):
    lid = row.get('listingid', '345')
    return {'success': 1, 'total_count': 1,
        'events': [{'listingid': lid, 'purchaseid': '55', 'event_type': event_type, 'time_event': 1001}],
        'listings': {lid: {'original_price': row.get('seller_receive', 200),
                          'asset': {'appid': APP_ID, 'contextid': '2', 'id': assetid, 'amount': '0'}}},
        'purchases': {lid + '_55': {'listingid': lid, 'steamid_purchaser': purchaser, 'failed': failed,
            'needs_rollback': 0, 'paid_amount': 2500, 'currencyid': '2008',
            'asset': {'appid': APP_ID, 'contextid': '2', 'id': '999', 'amount': '1'},
            'received_amount': receive, 'received_currencyid': currency}}}


class SaleTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.session = TrackingSession()
        self.now = 1000
        self.service = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        self.row = self.service._journal[0]
        self.row.update(status='listed', proof='a' * 32, listingid='345', game_instance_id='game-guid',
            buyer_total=230, seller_receive=200, sale_amount=1, currency_id=23,
            submitted_at=999, publisher_fee='.10')
        self.service._save()

    def receipt(self):
        return self.service.proof_state('a' * 32, STEAMID)

    def test_active_unsold_and_wallet_quote_then_expired_or_failed_read_never_reuses_action(self):
        self.service.refresh_confirmation()
        value = project_receipt(self.receipt())
        self.assertEqual((value['status'], value['my_price'], value['lowest_price']),
                         ('listed', 'CNY 2.30', 'CNY 2.30'))
        self.assertTrue(value['can_reprice'])
        self.now += 21
        value = project_receipt(self.receipt())
        self.assertEqual(value['status'], 'uncertain')
        self.assertFalse(value['can_reprice'])

    def test_same_clock_forced_reread_cannot_keep_old_active_receipt(self):
        self.service.refresh_confirmation()
        self.session.html = ''
        self.service._reconcile_after = 0
        self.service.refresh_confirmation()
        self.assertEqual(project_receipt(self.receipt())['status'], 'uncertain')

    def test_ui_snapshot_remains_responsive_during_quote_read_but_never_extends_ttl(self):
        self.service.refresh_confirmation()
        before = len(self.session.requests)
        with self.service._operation:
            value = self.service.proof_state('a' * 32, STEAMID, wait=False)
            self.assertEqual(project_receipt(value)['status'], 'listed')
            self.now += 21
            value = self.service.proof_state('a' * 32, STEAMID, wait=False)
            self.assertEqual(project_receipt(value)['status'], 'uncertain')
            self.assertFalse(value['can_reprice'])
        self.assertEqual(len(self.session.requests), before)

    def test_exact_sale_uses_received_wallet_amount_not_foreign_buyer_payment(self):
        self.session.html = ''
        self.session.history = history(self.row)
        self.service.refresh_confirmation()
        result = project_receipt(self.receipt())
        self.assertEqual(result['status'], 'sold')
        self.assertEqual(result['proceeds'], 'CNY 1.07')
        self.assertFalse(result['can_reprice'])
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restored.proof_state('a' * 32, STEAMID)['proceeds'], 'CNY 1.07')

    def test_positive_sale_without_earlier_listingid_is_matched_by_exact_asset(self):
        self.session.html = ''
        self.session.history = history(self.row)
        self.row.pop('listingid')
        self.service.refresh_confirmation()
        self.assertEqual(project_receipt(self.receipt())['status'], 'sold')

    def test_unknown_listing_id_requires_unique_receipt_across_pages(self):
        self.session.html = ''
        first = history(self.row)
        second = deepcopy(first)
        second['events'][0]['listingid'] = '678'
        second['listings'] = {'678': second['listings']['345']}
        second['purchases'] = {'678_55': {**second['purchases']['345_55'], 'listingid': '678'}}
        first['total_count'] = second['total_count'] = 101
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/market/myhistory/'):
                return second if 'start=100' in path else first
            return original(method, path, data)
        self.row.pop('listingid')
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
        self.assertEqual(project_receipt(self.receipt())['status'], 'uncertain')
        self.assertNotIn('proceeds', self.row)

    def test_unknown_listing_id_requires_complete_history_scan(self):
        self.session.html = ''
        self.session.history = history(self.row)
        self.session.history['total_count'] = 501
        self.row.pop('listingid')
        self.service.refresh_confirmation()
        self.assertEqual(project_receipt(self.receipt())['status'], 'uncertain')

    def paged_history(self, positions=(608,)):
        """609 events: a legacy order outside the old 500-event window."""
        value = history(self.row)
        events = [{'listingid': str(10000 + n), 'event_type': 1, 'time_event': 2000 - n}
                  for n in range(609)]
        for n, position in enumerate(positions):
            lid = str(345 + n)
            event = {**value['events'][0], 'listingid': lid}
            events[position] = event
            if n:
                value['listings'][lid] = deepcopy(value['listings']['345'])
                value['purchases'][lid + '_55'] = {**value['purchases']['345_55'], 'listingid': lid}
        value.update(events=events, total_count=len(events))
        self.session.html = ''
        self.row.pop('listingid')
        original = self.session.request
        starts = []
        def request(method, path, data=None):
            if path.startswith('/market/myhistory/'):
                self.assertEqual((method, data), ('GET', None))
                start = int(parse_qs(urlsplit(path).query)['start'][0])
                starts.append(start)
                return {**deepcopy(value), 'events': deepcopy(value['events'][start:start + 100])}
            return original(method, path, data)
        return value, starts, request

    def test_legacy_sale_beyond_500_resumes_next_pass_and_leaves_pending_list(self):
        value, starts, request = self.paged_history()
        self.row['status'] = 'pending'
        self.service._save()
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
            self.assertEqual(starts, [0, 100, 200, 300, 400])
            self.assertEqual(self.row['status'], 'pending')
            self.assertEqual(len(self.service.pending_trade_receipts(STEAMID)), 1)
            self.now += 16
            self.service.refresh_confirmation()
        self.assertEqual(starts, [0, 100, 200, 300, 400, 0, 500, 600])
        self.assertEqual(self.row['status'], 'sold')
        self.assertEqual(self.row['listingid'], '345')
        self.assertEqual(self.row['proceeds'], 107)
        self.assertEqual(self.service.pending_trade_receipts(STEAMID), [])
        self.assertEqual(self.session.sales, [])

    def test_legacy_receipt_on_first_page_waits_for_later_ambiguity(self):
        _, starts, request = self.paged_history((0, 608))
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
            self.assertNotEqual(self.row['status'], 'sold')
            self.now += 16
            self.service.refresh_confirmation()
        self.assertEqual(starts[-2:], [500, 600])
        self.assertNotEqual(self.row['status'], 'sold')
        self.assertNotIn('proceeds', self.row)

    def test_history_head_change_restarts_offsets_even_with_unchanged_total(self):
        value, starts, request = self.paged_history()
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
            value['events'][0]['time_event'] += 1
            self.now += 16
            self.service.refresh_confirmation()
            self.assertEqual(starts, [0, 100, 200, 300, 400] * 2)
            self.assertNotEqual(self.row['status'], 'sold')
            self.now += 16
            self.service.refresh_confirmation()
        self.assertEqual(self.row['status'], 'sold')

    def test_failed_history_continuation_discards_partial_evidence(self):
        _, starts, request = self.paged_history()
        def failing(method, path, data=None):
            if path.startswith('/market/myhistory/') and 'start=500' in path:
                raise SteamSessionError('temporary failure')
            return request(method, path, data)
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
        self.now += 16
        with patch.object(self.session, 'request', side_effect=failing):
            self.service.refresh_confirmation()
        self.assertIsNone(self.service._history_scan)
        self.assertNotEqual(self.row['status'], 'sold')
        self.now += 16
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
        self.assertEqual(starts[-5:], [0, 100, 200, 300, 400])

    def test_history_account_change_mid_request_cannot_confirm_old_sale(self):
        _, _, request = self.paged_history((0,))
        def switched(method, path, data=None):
            result = request(method, path, data)
            if path.startswith('/market/myhistory/'):
                self.session.wallet['steamid'] = OTHER
            return result
        with patch.object(self.session, 'request', side_effect=switched):
            self.service.refresh_confirmation()
        self.assertNotEqual(self.row['status'], 'sold')
        self.assertIsNone(self.service._history_scan)

    def test_expired_history_scan_restarts_and_pagination_drift_never_confirms(self):
        value, starts, request = self.paged_history((0,))
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
            self.now += 601
            self.service.refresh_confirmation()
        self.assertEqual(starts, [0, 100, 200, 300, 400] * 2)
        self.assertNotEqual(self.row['status'], 'sold')
        self.now += 16
        def drift(method, path, data=None):
            result = request(method, path, data)
            if path.startswith('/market/myhistory/') and 'start=500' in path:
                result['total_count'] += 1
            return result
        with patch.object(self.session, 'request', side_effect=drift):
            self.service.refresh_confirmation()
        self.assertIsNone(self.service._history_scan)
        self.assertNotEqual(self.row['status'], 'sold')

    def absent_views(self, *, game_items=(), native_items=(), inventory_items=(), listing_items=()):
        self.row.update(status='pending', created_at=800, submitted_at=801)
        self.row.pop('listingid', None)
        self.service._save()
        view = {'steamid': STEAMID, 'generation': 1, 'fresh': True, 'scope_complete': True,
                'checked_at': self.now, 'expires': self.now + 12}
        self.service._observed_inventory = {**view, 'items': list(inventory_items)}
        self.service._observed_listings = {**view, 'items': list(listing_items)}
        context = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                   'read_at': time.monotonic(), 'native_read_at': time.monotonic(),
                   'native': {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                              'server_checked_at': self.now, 'listings': list(native_items)}}
        self.service.observe_game_trade_context(context, list(game_items), True)
        return context

    def test_complete_absence_is_assumed_sold_without_invented_wallet_credit_and_survives_restart(self):
        self.absent_views()
        with self.service._operation:
            self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'sold')
        self.assertTrue(self.row['assumed_sold'])
        self.assertNotIn('sold_at', self.row)
        self.assertNotIn('proceeds', self.row)
        self.assertIsNone(project_receipt(self.receipt())['proceeds'])
        self.assertIn('按已出售处理', project_receipt(self.receipt())['message'])
        self.assertEqual(self.service.pending_trade_receipts(STEAMID), [])
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restored.pending_trade_receipts(STEAMID), [])
        self.assertEqual(self.session.sales, [])

    def test_background_refresh_applies_absence_rule_after_reading_complete_steam_views(self):
        self.absent_views()
        self.service._observed_inventory = {}
        self.service._observed_listings = {}
        self.session.inventory = inventory()
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/market/mylistings/'):
                return {'success': 1, 'total_count': 0, 'results_html': ''}
            return original(method, path, data)
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
        self.assertEqual(self.row['status'], 'sold')
        self.assertTrue(self.row['assumed_sold'])
        self.assertEqual(self.service.pending_trade_receipts(STEAMID), [])
        self.assertEqual(self.session.sales, [])

    def test_possession_in_any_current_location_prevents_assumed_sale(self):
        for source in ('game_items', 'native_items', 'inventory_items', 'listing_items'):
            with self.subTest(source=source):
                if source == 'game_items':
                    item = {'name_key': 'GoldBar', 'container': 'storage', 'instance_id': 'game-guid'}
                elif source == 'native_items':
                    item = {'internal_name': 'GoldBar', 'received': False, 'instance_id': 'game-guid'}
                else:
                    item = {'item_key': 'GoldBar', 'variant': 'normal', 'assetid': '1'}
                self.absent_views(**{source: [item]})
                self.service._retire_absent_trades()
                self.assertEqual(self.row['status'], 'pending')

    def test_legacy_order_without_game_guid_is_retired_only_if_no_possible_matching_item_remains(self):
        self.row.pop('game_instance_id')
        self.absent_views(game_items=[{'name_key': 'GoldBar', 'container': 'inventory'}])
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'pending')
        self.absent_views()
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'sold')

    def test_legacy_exact_upload_proof_restores_guid_without_confusing_another_identical_item(self):
        self.row.pop('game_instance_id')
        intent = {'phase': 'resolved', 'steamid': STEAMID, 'ticket': self.row['proof'],
                  'item_key': 'GoldBar', 'variant': 'normal', 'instance_id': 'game-guid', 'uid': 'uploaded-uid'}
        self.service.restore_upload_identities([intent])
        self.assertEqual(self.row['game_instance_id'], 'game-guid')
        self.assertEqual(self.row['status'], 'listed')
        self.assertEqual(self.row['game_item_uid'], 'uploaded-uid')
        self.absent_views(game_items=[{'name_key': 'GoldBar', 'container': 'storage', 'instance_id': 'game-guid',
                                      'item_uid': 'different-current-uid'}])
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'sold')
        self.assertTrue(self.row['assumed_sold'])
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda:self.now)
        self.assertEqual(restored._journal[0]['game_instance_id'], 'game-guid')
        self.assertEqual(restored._journal[0]['game_item_uid'], 'uploaded-uid')
        self.assertEqual(self.session.sales, [])

    def test_legacy_identity_migration_requires_unique_exact_proof_account_kind_variant_and_confirmed_intent(self):
        self.row.pop('game_instance_id')
        intent = {'phase': 'resolved', 'steamid': STEAMID, 'ticket': self.row['proof'],
                  'item_key': 'GoldBar', 'variant': 'normal', 'instance_id': 'game-guid'}
        for changes in ({'phase': 'uncertain'}, {'phase': 'submitted'}, {'steamid': OTHER},
                        {'ticket': 'b'*32}, {'item_key': 'Different'}, {'variant': 'black_mist'}, {'instance_id': ''}):
            with self.subTest(changes=changes):
                self.service.restore_upload_identities([{**intent, **changes}])
                self.assertNotIn('game_instance_id', self.row)
        self.service.restore_upload_identities([intent, {**intent, 'instance_id': 'conflicting-guid'}])
        self.assertNotIn('game_instance_id', self.row)

    def test_same_physical_uid_or_native_instance_keeps_order_but_another_steam_asset_is_separate(self):
        self.row['game_item_uid'] = 'uploaded-uid'
        self.absent_views(game_items=[{'name_key': 'GoldBar', 'container': 'storage', 'instance_id': 'game-guid',
                                      'item_uid': 'uploaded-uid'}])
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'pending')
        self.absent_views(native_items=[{'internal_name': 'GoldBar', 'received': False, 'instance_id': 'game-guid'}])
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'pending')
        self.absent_views(inventory_items=[{'item_key': 'GoldBar', 'assetid': '2', 'instance_id': 'game-guid'}])
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'sold')

    def test_slow_read_has_separate_display_absence_lease_without_extending_reprice_permission(self):
        context = self.absent_views()
        self.service._observed_inventory = {}
        self.service._observed_listings = {}
        self.session.inventory = inventory()
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/market/mylistings/'):
                self.now += 21
                # The independent game reader keeps publishing while Steam
                # network requests run, but must not extend the native receipt.
                self.service.observe_game_trade_context(context, [], True)
                return {'success': 1, 'total_count': 0, 'results_html': ''}
            return original(method, path, data)
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
        self.assertFalse(self.service.current_trade_evidence(STEAMID)['complete'])
        self.assertTrue(self.service.current_trade_evidence(STEAMID, for_absence=True)['complete'])
        self.assertEqual(self.row['status'], 'sold')
        self.assertEqual(self.session.sales, [])
        self.service._observed_listings['fresh'] = False
        self.assertFalse(self.service.current_trade_evidence(STEAMID, for_absence=True)['complete'])

    def test_unknown_variant_of_same_kind_blocks_retiring_legacy_black_mist_order(self):
        self.row.pop('game_instance_id')
        self.row['variant'] = 'black_mist'
        self.absent_views(native_items=[{'internal_name': 'GoldBar', 'received': False}])
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'pending')

    def test_failed_incomplete_expired_or_other_account_views_do_not_prove_absence(self):
        for view_name, changes in (
                ('_observed_inventory', {'fresh': False}),
                ('_observed_inventory', {'scope_complete': False}),
                ('_observed_listings', {'expires': self.now}),
                ('_observed_listings', {'generation': 2}),
                ('_game_trade_view', {'complete': False}),
                ('_game_trade_view', {'steamid': OTHER}),
                ('_game_trade_view', {'expires': self.now})):
            with self.subTest(view=view_name, changes=changes):
                self.absent_views()
                getattr(self.service, view_name).update(changes)
                self.service._retire_absent_trades()
                self.assertEqual(self.row['status'], 'pending')

    def test_unread_native_list_stale_identity_and_incomplete_inventory_do_not_prove_absence(self):
        for changes, complete in (({'identity_fresh': False}, True), ({}, False),
                                  ({'native': {'available': False}}, True)):
            context = self.absent_views()
            context.update(changes)
            self.service.observe_game_trade_context(context, [], complete)
            self.service._retire_absent_trades()
            self.assertEqual(self.row['status'], 'pending')

    def test_new_submissions_and_unresolved_reprice_are_not_retired(self):
        self.absent_views()
        self.row['submitted_at'] = self.now - 119
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'pending')
        self.row['submitted_at'] = 801
        self.row['reprice'] = {'phase': 'sale_submitted'}
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'pending')

    def test_exact_asset_reappearance_restores_tracking_without_authorizing_resubmission(self):
        self.absent_views()
        self.service._retire_absent_trades()
        self.service._observed_inventory['items'] = [{'assetid': '1', 'item_key': 'GoldBar', 'variant': 'normal'}]
        self.service._retire_absent_trades()
        self.assertEqual(self.row['status'], 'uncertain')
        self.assertNotIn('assumed_sold', self.row)
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_slow_listing_read_does_not_extend_freshness_and_rate_limit_stops_quotes(self):
        original = self.session.request
        def request(method, path, data=None):
            if path.startswith('/market/mylistings/'):
                self.now += 21
            return original(method, path, data)
        with patch.object(self.session, 'request', side_effect=request):
            self.service.refresh_confirmation()
        self.assertFalse(self.receipt()['can_reprice'])
        self.session.html = ''
        self.service._reconcile_after = 0
        with patch.object(self.service, '_tracking_history', side_effect=SteamSessionError('limited', status=429)):
            with patch.object(self.service, '_tracking_quote') as quote:
                self.service.refresh_confirmation()
                quote.assert_not_called()
        self.assertGreaterEqual(self.service._retry_after, self.now + 300)

    def test_many_listed_items_do_not_starve_behind_expiring_first_quote(self):
        self.service._active_checked_at = self.row['active_checked_at'] = 1
        later = {**self.row, 'item_key': 'AnotherItem'}
        self.service._tracking_quotes = {('GoldBar', 'normal'): {'refresh_after': self.now - 1}}
        with patch.object(self.service, '_tracking_quote') as quote:
            self.service._refresh_tracking(STEAMID, [self.row, later])
            quote.assert_called_once_with(STEAMID, later)

    def test_failed_quote_refresh_keeps_successful_price_for_ten_minutes_without_renewing_it(self):
        self.service.refresh_confirmation()
        quote = dict(self.service._tracking_quotes[('GoldBar', 'normal')])
        self.assertEqual(quote['expires_at'] - quote['checked_at'], 600)
        self.now += 61
        with patch.object(self.service, '_tracking_quote', side_effect=SteamSessionError('quote unavailable')) as fetch:
            self.service.refresh_confirmation()
            value = self.receipt()
            self.assertEqual(value['lowest_price'], 'CNY 2.30')
            self.assertTrue(value['lowest_price_cached'])
            self.assertEqual(value['lowest_price_expires_at'], quote['expires_at'])
            self.service._refresh_tracking(STEAMID, [self.row])
            fetch.assert_called_once()
            self.now += 31
            self.service._refresh_tracking(STEAMID, [self.row])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.receipt()['lowest_price_expires_at'], quote['expires_at'])
        self.now = quote['expires_at']
        self.assertIsNone(self.receipt()['lowest_price'])

    def test_long_quote_cache_never_crosses_login_or_wallet_scope(self):
        self.service.refresh_confirmation()
        for changes in ({'steamid': OTHER}, {'binding_generation': 2}, {'wallet_currency': 'USD'}):
            original = self.service.auth_state()
            with self.subTest(changes=changes), patch.object(self.service, 'auth_state', return_value={**original, **changes}):
                self.assertIsNone(self.receipt()['lowest_price'])

    def test_quote_failure_during_login_change_cannot_rebind_old_price_to_new_session(self):
        self.service.refresh_confirmation()
        self.now += 61
        original = self.service.auth_state()
        auth = dict(original)
        def fail(*args):
            auth['binding_generation'] = 2
            raise SteamSessionError('login changed')
        with patch.object(self.service, 'auth_state', side_effect=lambda: dict(auth)), \
                patch.object(self.service, '_tracking_quote', side_effect=fail):
            self.service._refresh_tracking(STEAMID, [self.row])
            self.assertIsNone(self.receipt()['lowest_price'])
        self.assertEqual(self.service._tracking_quotes[('GoldBar', 'normal')]['generation'], 1)

    def test_stale_order_keeps_readonly_reprice_entry_but_never_write_permission(self):
        self.service.refresh_confirmation()
        self.now += 21
        value = project_receipt(self.receipt())
        self.assertEqual(value['status'], 'uncertain')
        self.assertTrue(value['reprice_available'])
        self.assertFalse(value['can_reprice'])
        self.assertEqual(self.session.sales, [])

    def test_reprice_without_cached_quote_allows_manual_price_without_network_reads(self):
        with patch.object(self.session, 'verify', side_effect=AssertionError('must stay local')), \
                patch.object(self.session, 'request', side_effect=AssertionError('must stay local')):
            preview = self.service.reprice_preview(self.row['id'])
        self.assertEqual(preview['default_price'], '')
        self.assertIsNone(preview['lowest_price'])
        self.assertIn('手填', preview['message'])
        self.assertEqual(self.session.sales, [])
        self.session.inventory = inventory()
        self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345', '/market/sellitem/'])

    def test_reprice_editor_uses_scoped_cache_even_while_worker_is_blocked_on_network(self):
        self.service.refresh_confirmation()
        self.now += 61
        requests = list(self.session.requests)
        self.service._operation.acquire()
        try:
            with patch.object(self.session, 'verify', side_effect=AssertionError('must stay local')), \
                    patch.object(self.session, 'request', side_effect=AssertionError('must stay local')):
                preview = self.service.reprice_preview(self.row['id'])
        finally:
            self.service._operation.release()
        self.assertEqual(preview['default_price'], '2.30')
        self.assertTrue(preview['lowest_price_cached'])
        self.assertEqual(preview['lowest_price_expires_at'], 1600)
        self.assertEqual(self.session.requests, requests)
        self.assertFalse(self.receipt()['can_reprice'])
        self.assertEqual(self.session.sales, [])

    def test_reprice_editor_never_reuses_expired_or_other_scope_quotes(self):
        self.service.refresh_confirmation()
        original = dict(self.service._tracking_quotes[('GoldBar', 'normal')])
        for changes in ({'expires_at': self.now}, {'steamid': OTHER}, {'generation': 2}, {'currency_id': 1}):
            with self.subTest(changes=changes):
                self.service._tracking_quotes[('GoldBar', 'normal')] = {**original, **changes}
                preview = self.service.reprice_preview(self.row['id'])
                self.assertEqual(preview['default_price'], '')
                self.assertIsNone(preview['lowest_price'])
        self.assertEqual(self.session.sales, [])

    def test_reprice_editor_rejects_unknown_or_expired_login_without_waiting(self):
        auth = self.service.auth_state()
        for changes in ({'verified': False}, {'expired': True}, {'steamid': OTHER}, {'wallet_currency': 'USD'}):
            with self.subTest(changes=changes), patch.object(self.service, 'auth_state', return_value={**auth, **changes}):
                with self.assertRaises(SteamSessionError):
                    self.service.reprice_preview(self.row['id'])
        self.assertEqual(self.session.sales, [])

    def test_reprice_from_stale_order_rechecks_exact_listing_before_any_cancel(self):
        self.row.update(status='uncertain', last_confirmed_status='listed')
        self.service._save()
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345', '/market/sellitem/'])

    def test_reopened_editor_invalidates_its_previous_token_without_writing(self):
        old = self.service.reprice_preview(self.row['id'])
        current = self.service.reprice_preview(self.row['id'])
        self.assertNotEqual(old['token'], current['token'])
        self.assertEqual(len(self.service._reprice_previews), 1)
        with self.assertRaises(SteamSessionError):
            self.service.reprice(old['token'], '2.30')
        self.assertEqual(self.session.sales, [])

    def test_reprice_preview_is_local_and_does_not_renew_listing_permission(self):
        self.service.refresh_confirmation()
        self.now += 21
        with patch.object(self.service, '_reconcile_listings') as orders, patch.object(self.service, '_tracking_quote') as quote:
            preview = self.service.reprice_preview(self.row['id'])
        self.assertEqual(preview['default_price'], '2.30')
        orders.assert_not_called()
        quote.assert_not_called()
        self.assertFalse(self.receipt()['can_reprice'])
        self.assertEqual(self.session.sales, [])

    def test_other_asset_account_cancellation_or_failed_purchase_is_not_a_sale(self):
        for options in ({'assetid': '2'}, {'purchaser': STEAMID}, {'event_type': 4}, {'failed': 1}):
            with self.subTest(options=options):
                self.session.history = history(self.row, **options)
                self.session.html = ''
                self.service._reconcile_after = self.service._history_after = 0
                self.service.refresh_confirmation()
                self.assertEqual(project_receipt(self.receipt())['status'], 'uncertain')
                self.assertEqual(self.session.sales, [])

    def test_history_currency_failure_does_not_guess_proceeds(self):
        self.session.html = ''
        self.session.history = history(self.row, currency='2008')
        self.service.refresh_confirmation()
        result = project_receipt(self.receipt())
        self.assertEqual(result['status'], 'sold')
        self.assertIsNone(result['proceeds'])

    def test_purchase_quantity_is_checked_instead_of_listing_remainder(self):
        self.session.html = ''
        self.session.history = history(self.row)
        self.session.history['purchases']['345_55']['asset']['amount'] = '2'
        self.service.refresh_confirmation()
        self.assertEqual(project_receipt(self.receipt())['status'], 'uncertain')

    def test_unlisted_missing_without_history_stays_uncertain_and_never_sells_again(self):
        self.session.html = ''
        self.service.refresh_confirmation()
        self.assertEqual(project_receipt(self.receipt())['status'], 'uncertain')
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_reprice_preview_is_read_only_and_validates_fee_before_cancel(self):
        self.service.refresh_confirmation()
        preview = self.service.reprice_preview(self.row['id'])
        self.assertEqual(preview['default_price'], '2.30')
        self.assertEqual(self.session.sales, [])
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '1.13')
        self.assertEqual(self.session.sales, [])

    def test_reprice_cancel_then_exact_return_then_single_new_sale(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        self.session.result = {'success': 1, 'requires_confirmation': True, 'listingid': '678'}
        self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345', '/market/sellitem/'])
        self.assertEqual(self.session.sales[-1][1]['assetid'], '2')
        self.assertEqual(self.session.sales[-1][1]['price'], 200)
        self.assertEqual(self.row['status'], 'pending')
        self.assertEqual(self.row['listingid'], '678')
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(len(self.session.sales), 2)

    def test_scalar_cancel_receipts_continue_to_exact_return_and_single_sale(self):
        for receipt in (True, 1, 'true', b'true', '1', {'success': True}):
            with self.subTest(receipt=receipt):
                self.setUp()
                preview = self.service.reprice_preview(self.row['id'])
                self.session.inventory = inventory()
                original_post = self.session.post
                def post(path, data, **kwargs):
                    result = original_post(path, data, **kwargs)
                    return receipt if path.startswith('/market/removelisting/') else result
                self.session.post = post
                self.service.reprice(preview['token'], '2.30')
                self.assertEqual([p for p, _ in self.session.sales],
                    ['/market/removelisting/345', '/market/sellitem/'])
                self.assertEqual(self.session.sales[-1][1]['assetid'], '2')

    def test_unconfirmed_cancel_receipts_alone_never_authorize_sale(self):
        for receipt in (False, 0, None, '', 'null', 'false', '[]', '{}', '"true"', '<html>bad gateway</html>'):
            with self.subTest(receipt=receipt):
                self.setUp()
                preview = self.service.reprice_preview(self.row['id'])
                def post(path, data, **kwargs):
                    # A receipt alone supplies neither a verified return nor
                    # disappearance of the old order.
                    self.session.sales.append((path, deepcopy(data)))
                    return receipt
                self.session.post = post
                with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
                    self.service.reprice(preview['token'], '2.30')
                self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])
                self.assertEqual(self.row['reprice']['phase'], 'cancel_submitted')

    def test_unknown_or_lost_cancel_reply_uses_exact_outcome_without_replaying_cancel(self):
        for receipt in ('', 'null', '{}', SteamSessionError('response lost')):
            with self.subTest(receipt=receipt):
                self.setUp()
                preview = self.service.reprice_preview(self.row['id'])
                self.session.inventory = inventory()
                original_post = self.session.post
                def post(path, data, **kwargs):
                    result = original_post(path, data, **kwargs)
                    if path.startswith('/market/removelisting/'):
                        if isinstance(receipt, Exception):
                            raise receipt
                        return receipt
                    return result
                self.session.post = post
                self.service.reprice(preview['token'], '2.30')
                self.assertEqual([p for p, _ in self.session.sales],
                    ['/market/removelisting/345', '/market/sellitem/'])

    def test_explicit_cancel_rejection_never_sells_even_if_a_matching_asset_appears(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        original_post = self.session.post
        def post(path, data, **kwargs):
            original_post(path, data, **kwargs)
            return {'success': False}
        self.session.post = post
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(len(self.session.sales), 1)

    def test_cancel_receipt_and_return_cannot_relist_while_original_order_remains(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        original_post = self.session.post
        original_html = self.session.html
        def post(path, data, **kwargs):
            result = original_post(path, data, **kwargs)
            self.session.html = original_html
            return result
        self.session.post = post
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])

    def test_scalar_cancel_does_not_weaken_object_only_new_sale_receipt(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        self.session.result = 'true'
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(len(self.session.sales), 2)
        self.assertEqual(self.row['reprice']['phase'], 'sale_submitted')

    def test_pending_or_incomplete_original_order_view_never_authorizes_new_sale(self):
        for kind in ('pending', 'incomplete'):
            with self.subTest(kind=kind):
                self.setUp()
                preview = self.service.reprice_preview(self.row['id'])
                self.session.inventory = inventory()
                original_post = self.session.post
                original_request = self.session.request
                def post(path, data, **kwargs):
                    result = original_post(path, data, **kwargs)
                    if kind == 'pending':
                        self.session.html = (f'<a onclick="CancelMarketListingConfirmation('
                            f"'mylisting_confirmation','345','{APP_ID}','2','1')\">pending</a>")
                    return result
                def request(method, path, data=None):
                    result = original_request(method, path, data)
                    if kind == 'incomplete' and self.session.cancelled and path.startswith('/market/mylistings/'):
                        result['total_count'] = 1  # Missing/unparsed row is not absence.
                    return result
                self.session.post = post
                self.session.request = request
                with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
                    self.service.reprice(preview['token'], '2.30')
                self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])

    def class_identity_listing(self):
        """Like real Steam: original class metadata, no game GUID on return."""
        original_request = self.session.request
        def request(method, path, data=None):
            result = original_request(method, path, data)
            if path.startswith('/market/mylistings/') and self.session.html:
                description = deepcopy(inventory()['descriptions'][0])
                result['assets'] = {str(APP_ID): {'2': {'1': description}}}
            return result
        self.session.request = request
        self.session.return_guid = None

    def test_reprice_never_uses_class_and_unique_delta_as_physical_return_identity(self):
        self.class_identity_listing()
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory(('8', 1, {}))  # Existing same-name item is not selected.
        original_post = self.session.post
        def post(path, data, **kwargs):
            result = original_post(path, data, **kwargs)
            if path.startswith('/market/removelisting/'):
                self.session.inventory = inventory(('8', 1, {}), ('2', 1, {}))
            return result
        self.session.post = post
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])
        self.assertEqual(self.row['assetid'], '1')
        self.assertEqual(self.row['game_instance_id'], 'game-guid')
        self.assertTrue(self.row['reprice']['cancel_acknowledged'])
        self.assertIn('后台继续核验', self.row['tracking_message'])
        self.assertIn('未重新上架', self.row['tracking_message'])

    def test_unknown_cancel_and_unrelated_same_class_asset_never_authorize_relisting(self):
        self.class_identity_listing()
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        def post(path, data, **kwargs):
            self.session.sales.append((path, deepcopy(data)))
            self.session.html = ''  # An absent order can also have sold.
            self.session.inventory = inventory(('999', 1, {}))
            return {}
        self.session.post = post
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])
        self.assertFalse(self.row['reprice']['cancel_acknowledged'])
        self.assertEqual(self.row['assetid'], '1')
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restored._journal[0]['reprice']['phase'], 'cancel_submitted')
        self.assertFalse(restored.proof_state('a' * 32, STEAMID)['can_reprice'])

    def test_reprice_accepts_unchanged_original_asset_without_game_guid(self):
        self.class_identity_listing()
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        original_post = self.session.post
        def post(path, data, **kwargs):
            result = original_post(path, data, **kwargs)
            if path.startswith('/market/removelisting/'):
                self.session.inventory = inventory(('1', 1, {}))
            return result
        self.session.post = post
        self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345', '/market/sellitem/'])
        self.assertEqual(self.session.sales[-1][1]['assetid'], '1')

    def test_reprice_never_accepts_name_only_wrong_class_or_multiple_inventory_deltas(self):
        for kind in ('wrong_class', 'different_description', 'multiple', 'excess', 'conflicting_guid'):
            with self.subTest(kind=kind):
                self.setUp()
                self.class_identity_listing()
                preview = self.service.reprice_preview(self.row['id'])
                self.session.inventory = inventory()
                original_post = self.session.post
                def post(path, data, **kwargs):
                    result = original_post(path, data, **kwargs)
                    if path.startswith('/market/removelisting/'):
                        if kind == 'wrong_class':
                            self.session.inventory['assets'][0]['classid'] = '43'
                            self.session.inventory['descriptions'][0]['classid'] = '43'
                        elif kind == 'different_description':
                            self.session.inventory['descriptions'][0]['descriptions'] = [{'value': 'different rolls'}]
                        elif kind == 'multiple':
                            self.session.inventory = inventory(('2', 1, {}), ('3', 1, {}))
                        elif kind == 'excess':
                            self.session.inventory = inventory(('2', 2, {}))
                        else:
                            self.session.inventory = inventory(('2', 1, {'game_instance_id': 'unrelated-guid'}))
                    return result
                self.session.post = post
                with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
                    self.service.reprice(preview['token'], '2.30')
                self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])
                self.assertTrue(self.row['reprice']['cancel_acknowledged'])
                self.assertIn('未重新上架', self.row['tracking_message'])

    def test_cancel_timeout_is_fenced_and_restart_never_replays(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.cancel_failure = SteamSessionError('response lost')
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(len(self.session.sales), 1)
        self.assertEqual(self.row['status'], 'uncertain')
        self.assertEqual(self.row['reprice']['phase'], 'cancel_submitted')
        restored = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        restored.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(len(self.session.sales), 1)
        self.assertFalse(restored.proof_state('a' * 32, STEAMID)['can_reprice'])

    def test_unrelated_returned_same_name_asset_never_authorizes_sale(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        self.session.return_guid = 'different-game-guid'
        with patch('deskrawl_assistant.sale_tracking.time.sleep'), self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.session.sales], ['/market/removelisting/345'])

    def test_new_sale_response_loss_persists_new_asset_and_never_resubmits(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.session.inventory = inventory()
        self.session.result = SteamSessionError('response lost')
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(self.row['assetid'], '2')
        self.assertNotIn('listingid', self.row)
        self.assertEqual(self.row['reprice']['phase'], 'sale_submitted')
        self.service.sale_once({'sell_enabled': True, 'categories': ['gold_bar']}, STEAMID)
        self.assertEqual(len(self.session.sales), 2)

    def test_preview_expiry_changed_account_and_disappeared_listing_prevent_cancel(self):
        preview = self.service.reprice_preview(self.row['id'])
        self.now += 121
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        preview = self.service.reprice_preview(self.row['id'])
        self.session.wallet['steamid'] = OTHER
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.session.wallet['steamid'] = STEAMID
        preview = self.service.reprice_preview(self.row['id'])
        self.session.html = ''
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(self.session.sales, [])


class ManagedTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.trading = SteamTradingService(Path(self.tmp.name), catalog(), session=TrackingSession())
        self.coordinator = AuctionCoordinator(Path(self.tmp.name), self.trading,
            lambda: (False, {}, STEAMID), start_worker=False)
        class Service(AuctionTrackingMixin):
            guard = threading.RLock()
            managed_started_at = 'current-run'
            auction_coordinator = self.coordinator
            _describe_item = staticmethod(lambda key: ('金条', ''))
        self.service = Service()
        self.service.managed = type('Managed', (), {'running': True, 'settings': {'auction': {
            **DEFAULT_AUCTION, 'upload_enabled': True, 'categories': ['gold_bar']}}})()
        self.service.client = SimpleNamespace(connected=True)
        self.service.snapshot = {'complete': True, 'bridge_session': 'session'}
        self.service.character = {'available': True, 'character_id': 'hero'}
        self.service.pending_trade_context = {'steamid': STEAMID, 'session': 'session',
            'character_id': 'hero', 'read_at': time.monotonic()}
        self.service._market_catalog = catalog
        self.item = {'rarity': 3, 'item_uid': 'native-uid', 'instance_id': 'native-guid', 'name_key': 'GoldBar',
            'identity_verified': True, 'container': 'storage', 'count': 1, 'item_type': 7,
            'base_rarity': 3, 'bound': False, 'upgrade_level': 0, 'socket_count': 0}

    def observe(self, items=None, complete=True, session='session', character_id='hero'):
        observe_candidates(self.service, [self.item] if items is None else items, complete,
                           session=session, character_id=character_id)

    def test_candidates_include_all_tradable_items_with_automation_off_and_survive_new_run(self):
        self.service.managed.running = False
        self.service.managed.settings['auction']['upload_enabled'] = False
        self.observe()
        self.assertEqual(self.service.managed_auction_tracking()['items'][0]['status'], 'waiting_town')
        self.service.managed_started_at = 'new-run'
        self.assertEqual(self.service.managed_auction_tracking()['total'], 1)
        self.observe([], complete=False)
        self.assertEqual(self.service.managed_auction_tracking()['total'], 1)
        self.observe([])
        self.assertEqual(self.service.managed_auction_tracking()['total'], 0)

    def test_cancelled_old_asset_and_returned_inventory_do_not_create_two_pending_items(self):
        self.trading.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        row = self.trading._journal[0]
        row.update(status='uncertain', game_instance_id='native-guid', proof='a' * 32,
            listingid='345', last_confirmed_status='listed',
            reprice={'phase': 'cancel_submitted', 'started_at': time.time() - 10})
        self.trading._save()
        now = time.time()
        self.trading._observed_inventory = {'steamid': STEAMID, 'generation': 1,
            'fresh': True, 'scope_complete': True, 'checked_at': now,
            'expires': now + 40, 'absence_expires': now + 120, 'items': [
                {'item_key': 'GoldBar', 'variant': 'normal', 'assetid': '2', 'instance_id': None, 'count': 1}]}
        self.trading._observed_listings = {'steamid': STEAMID, 'generation': 1,
            'fresh': True, 'scope_complete': True, 'checked_at': now,
            'expires': now + 12, 'absence_expires': now + 120, 'items': []}
        self.service.pending_trade_context['native'] = {'available': True, 'steamid': STEAMID,
            'steamid_verified': True, 'server_checked_at': now, 'listings': [
                {'instance_id': 'native-guid', 'internal_name': 'GoldBar', 'received': False}]}
        self.service.pending_trade_context['native_read_at'] = time.monotonic()
        items = self.service.managed_auction_tracking()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['id'], items[0]['status']), ('inventory-2', 'uploaded'))
        self.assertEqual(row['status'], 'uncertain')  # No invented sale or permission to replay.
        self.assertEqual(row['reprice']['phase'], 'cancel_submitted')
        self.assertFalse(self.trading.session.sales)
        for change in ('failed', 'partial', 'old', 'active'):
            with self.subTest(change=change):
                saved = deepcopy(self.trading._observed_listings)
                if change == 'failed': self.trading._observed_listings['fresh'] = False
                if change == 'partial': self.trading._observed_listings['scope_complete'] = False
                if change == 'old': self.trading._observed_listings['checked_at'] = now - 30
                if change == 'active': self.trading._observed_listings['items'] = [{'assetid': '1'}]
                old = next(r for r in self.trading.pending_trade_receipts(STEAMID) if r['id'] == row['id'])
                self.assertFalse(old['display_retired'])
                self.trading._observed_listings = saved
        self.trading._retire_cancelled_reprice_displays(STEAMID)
        restored = SteamTradingService(Path(self.tmp.name), catalog(), session=self.trading.session)
        old = next(r for r in restored.pending_trade_receipts(STEAMID) if r['id'] == row['id'])
        self.assertTrue(old['display_retired'])  # Survives restart and expired views.
        self.assertEqual(restored._journal[0]['status'], 'uncertain')
        self.trading._observed_listings['items'] = [{'assetid': '1'}]
        self.trading._retire_cancelled_reprice_displays(STEAMID)
        old = next(r for r in self.trading.pending_trade_receipts(STEAMID) if r['id'] == row['id'])
        self.assertFalse(old['display_retired'])  # Fresh positive evidence restores verification.

    def test_successful_reprice_new_asset_is_one_row_with_its_market_stage(self):
        self.trading.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        row = self.trading._journal[0]
        row.update(status='pending', game_instance_id='native-guid', proof='a' * 32, assetid='2',
                   previous_listings=[{'listingid': '345', 'assetid': '1'}])
        self.trading._save()
        self.trading._observed_inventory = {'steamid': STEAMID, 'items': [
            {'item_key': 'GoldBar', 'variant': 'normal', 'assetid': '2', 'count': 1}],
            'fresh': True, 'expires': time.time() + 30, 'generation': 1}
        items = self.service.managed_auction_tracking()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], row['id'])
        self.assertEqual(items[0]['status'], 'uncertain')
        self.assertEqual(items[0]['previous_status'], 'pending_confirmation')

    def test_candidate_group_follows_current_policy_without_a_new_inventory_read(self):
        self.observe()
        value = self.service.managed_auction_tracking()['items'][0]
        self.assertTrue(value['auto_eligible'])
        self.assertNotIn('_policy_item', value)
        config = self.service.managed.settings['auction']
        config['excluded_keys'] = ['GoldBar']
        value = self.service.managed_auction_tracking()['items'][0]
        self.assertFalse(value['auto_eligible'])
        self.assertIn('强制保留', value['message'])
        config['excluded_keys'] = []
        config['categories'] = ['grand_rune']
        self.assertFalse(self.service.managed_auction_tracking()['items'][0]['auto_eligible'])
        config.update(categories=['gold_bar'], upload_enabled=False)
        self.assertFalse(self.service.managed_auction_tracking()['items'][0]['auto_eligible'])
        config['upload_enabled'] = True
        self.assertTrue(self.service.managed_auction_tracking()['items'][0]['auto_eligible'])

    def test_raw_inventory_uses_the_same_identity_and_native_rules_as_managed_upload(self):
        raw = {**self.item, 'identity_verified': None, 'read_evidence': {'snapshot_consistent': True}}
        self.observe([raw])
        self.assertTrue(self.service.managed_auction_tracking()['items'][0]['auto_eligible'])
        for changes in ({'bound': True}, {'upgrade_level': 1}, {'count': 2},
                        {'identity_verified': False}, {'read_evidence': {}}, {'instance_id': ''}):
            with self.subTest(changes=changes):
                self.observe([{**raw, **changes}])
                self.assertFalse(self.service.managed_auction_tracking()['items'][0]['auto_eligible'])

    def test_old_runs_and_manual_uploads_are_included_but_other_accounts_are_not(self):
        self.coordinator.submitted(self.item, STEAMID, managed_run='old-run')
        self.coordinator.submitted({**self.item, 'item_uid': 'manual', 'instance_id': 'manual-guid'}, STEAMID)
        self.coordinator.submitted({**self.item, 'item_uid': 'other'}, OTHER)
        self.assertEqual(self.service.managed_auction_tracking()['total'], 2)

    def test_current_possession_deduplicates_historical_upload_by_exact_uid(self):
        self.coordinator.submitted(self.item, STEAMID)
        self.observe()
        value = self.service.managed_auction_tracking(include_identity=True)
        self.assertEqual(value['total'], 1)
        self.assertEqual(value['items'][0]['item_uid'], 'native-uid')
        self.assertEqual(value['items'][0]['status'], 'waiting_town')

    def test_other_character_session_disconnection_and_stale_identity_are_safe(self):
        self.observe(character_id='other'); self.observe(session='other')
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        self.observe()
        self.service.pending_trade_context['identity_fresh'] = False
        self.assertEqual(self.service.managed_auction_tracking()['items'][0]['previous_status'], 'waiting_town')
        self.service.character['character_id'] = 'other'
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        self.service.character['character_id'] = 'hero'
        self.service.client.connected = False
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])

    def test_exact_sold_receipts_are_removed_instead_of_reappearing_from_upload_intent(self):
        identity = self.coordinator.submitted(self.item, STEAMID, 'a' * 32, managed_run='old-run')
        self.coordinator.intents[identity]['phase'] = 'resolved'
        with patch.object(self.trading, 'proof_state', return_value={'status': 'sold', 'proceeds': 'CNY 1.07'}):
            self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        with patch.object(self.trading, 'pending_trade_receipts', return_value=[{
                'id': 'receipt', 'instance_id': 'native-guid', 'proof': 'a' * 32,
                'item_key': 'GoldBar', 'status': 'listed', 'my_price': 'CNY 2.30', 'count': 1}]):
            value = self.service.managed_auction_tracking()
            self.assertEqual(value['total'], 1)
            self.assertEqual(value['items'][0]['status'], 'listed')
            self.assertNotIn('proof', value['items'][0])
        self.coordinator.intents[identity]['market_skipped'] = True
        value = self.service.managed_auction_tracking()['items'][0]
        self.assertEqual((value['status'], value['previous_status']), ('uncertain', 'uploaded'))
        self.assertFalse(value['auto_eligible'])

    def test_old_resolved_uploads_are_not_current_orders_after_complete_steam_views(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=100)
        native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                  'listings': [{'instance_id': 'native-guid', 'internal_name': 'GoldBar', 'received': False}]}
        self.service.pending_trade_context.update(native_read_at=time.monotonic(), native=native)
        with patch.object(self.trading, 'current_trade_evidence', return_value={
                'complete': True, 'checked_at': 1000, 'items': []}):
            self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        # A later failed query cannot resurrect an already retired old upload.
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        self.assertEqual(self.coordinator.intents[identity]['phase'], 'resolved')
        self.assertEqual(self.trading._journal, [])  # No invented sold receipt.
        # Physical ownership is authoritative if the player takes it back.
        self.observe()
        self.assertEqual(self.service.managed_auction_tracking()['items'][0]['status'], 'waiting_town')

    def test_recent_upload_or_incomplete_steam_views_preserve_uncertain_history(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=950)
        with patch.object(self.trading, 'current_trade_evidence', return_value={
                'complete': True, 'checked_at': 1000, 'items': []}):
            value = self.service.managed_auction_tracking()['items'][0]
            self.assertEqual((value['status'], value['previous_status']), ('uncertain', 'uploaded'))
        self.coordinator.intents[identity]['created_at'] = 100
        with patch.object(self.trading, 'current_trade_evidence', return_value={'complete': False}):
            self.assertEqual(self.service.managed_auction_tracking()['total'], 1)

    def test_current_steam_asset_supersedes_old_unlinked_upload_without_name_matching(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=100)
        # Steam does not always expose the game's GUID. The live asset is
        # displayed exactly once, without guessing which old GUID it belongs to.
        with patch.object(self.trading, 'pending_trade_receipts', return_value=[{
                'id': 'inventory-99', 'item_key': 'GoldBar', 'variant': 'normal', 'status': 'uploaded'}]), \
             patch.object(self.trading, 'current_trade_evidence', return_value={
                'complete': True, 'checked_at': 1000, 'items': [{'item_key': 'GoldBar', 'variant': 'normal'}]}):
            value = self.service.managed_auction_tracking()
        self.assertEqual(value['total'], 1)
        self.assertEqual(value['items'][0]['id'], 'inventory-99')

    def test_exact_sold_instance_retires_an_upload_even_without_a_steam_ticket(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity]['phase'] = 'resolved'
        with patch.object(self.trading, 'sold_trade_instances', return_value=frozenset({'native-guid'})):
            self.assertEqual(self.service.managed_auction_tracking()['items'], [])

    def test_refreshed_native_absence_retires_only_historical_upload_not_an_unconfirmed_request(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=100)
        native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                  'server_checked_at': time.time(), 'listings': []}
        self.service.pending_trade_context.update(native_read_at=time.monotonic(), native=native)
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        self.assertEqual(self.trading._journal, [])
        self.coordinator.intents[identity]['phase'] = 'uncertain'
        self.assertEqual(self.service.managed_auction_tracking()['total'], 1)

    def test_native_cached_unloaded_busy_old_or_wrong_account_is_not_absence_evidence(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=time.time()-10)
        native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                  'server_checked_at': time.time(), 'listings': []}
        for changes in ({'server_checked_at': 0}, {'server_checked_at': time.time()-61},
                        {'server_checked_at': time.time()-20}, {'busy': True}, {'needs_refresh': True},
                        {'listings': None}, {'steamid': OTHER}, {'steamid_verified': False}):
            with self.subTest(changes=changes):
                self.service.pending_trade_context.update(native_read_at=time.monotonic(), native={**native, **changes})
                row = self.service.managed_auction_tracking()['items'][0]
                self.assertEqual(row['status'], 'uncertain')
                self.assertEqual(row['previous_status'], 'uploaded')

    def test_current_native_presence_wins_over_old_intent_and_unlinked_steam_absence(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=100)
        native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                  'server_checked_at': time.time(), 'listings': []}
        self.service.pending_trade_context.update(native_read_at=time.monotonic(), native=native)
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        native['listings'] = [{'instance_id': 'native-guid', 'internal_name': 'GoldBar', 'received': False}]
        with patch.object(self.trading, 'current_trade_evidence', return_value={
                'complete': True, 'checked_at': time.time(), 'items': []}):
            rows = self.service.managed_auction_tracking()['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'uploaded')
        self.assertNotIn((STEAMID, 'native-guid'), self.service._historical_uploads)

    def test_native_absence_display_retirement_survives_restart_and_current_presence_restores_it(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=100)
        native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                  'server_checked_at': time.time(), 'listings': []}
        self.coordinator.observe(native)
        restored = AuctionCoordinator(Path(self.tmp.name), self.trading,
            lambda: (False, {}, STEAMID), start_worker=False)
        self.service.auction_coordinator = restored
        self.assertTrue(restored.intents[identity]['display_retired_at'])
        self.assertEqual(self.service.managed_auction_tracking()['items'], [])
        self.assertEqual(self.trading._journal, [])
        native['listings'] = [{'instance_id': 'native-guid', 'internal_name': 'GoldBar', 'received': False}]
        restored.observe(native)
        self.assertNotIn('display_retired_at', restored.intents[identity])
        self.service.pending_trade_context.update(native_read_at=time.monotonic(), native=native)
        self.assertEqual(self.service.managed_auction_tracking()['items'][0]['status'], 'uploaded')

    def test_invalid_native_query_never_persists_absence_and_does_not_change_unconfirmed_upload(self):
        identity = self.coordinator.submitted(self.item, STEAMID)
        self.coordinator.intents[identity].update(phase='resolved', created_at=100)
        native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                  'server_checked_at': time.time(), 'listings': []}
        for changes in ({'server_checked_at': 0}, {'server_checked_at': time.time()-61},
                        {'steamid': OTHER}, {'steamid_verified': False}, {'busy': True},
                        {'needs_refresh': True}, {'listings': None}, {'listings': [{}]}):
            with self.subTest(changes=changes):
                self.coordinator.observe({**native, **changes})
                self.assertNotIn('display_retired_at', self.coordinator.intents[identity])
        self.coordinator.intents[identity]['phase'] = 'uncertain'
        self.coordinator.observe(native)
        self.assertNotIn('display_retired_at', self.coordinator.intents[identity])

    def test_shared_reprice_job_requires_explicit_payload_and_serializes_duplicate_submissions(self):
        self.service.closed = threading.Event()
        self.service.updating = False
        payload = {'token': 'a' * 43, 'price': '2.30'}
        self.trading._reprice_previews = {payload['token']: {'steamid': STEAMID}}
        with patch.object(self.trading, 'reprice', return_value=None) as reprice:
            with patch('deskrawl_assistant.auction_tracking.threading.Thread') as worker:
                value = self.service.auction_reprice_start(payload)
                self.assertTrue(value['running'])
                reprice.assert_not_called()
                with self.assertRaises(ValidationError):
                    self.service.auction_reprice_start(payload)
                job = worker.call_args.kwargs['target']
            job()
            reprice.assert_called_once_with(payload['token'], payload['price'], validate=unittest.mock.ANY)
            validate = reprice.call_args.kwargs['validate']
            self.assertTrue(validate())
            self.service.closed.set()
            self.assertFalse(validate())
            self.assertFalse(self.service.auction_reprice_job['running'])
            self.service.closed.clear()
        with patch.object(self.trading, 'reprice', side_effect=SteamSessionError('precise failure')):
            with patch('deskrawl_assistant.auction_tracking.threading.Thread') as worker:
                self.service.auction_reprice_start(payload)
                job = worker.call_args.kwargs['target']
            job()
            self.assertTrue(self.service.auction_reprice_job['error'])
            self.assertIn('precise failure', self.service.auction_reprice_job['message'])
        for invalid in ({'tracking_id': 'a' * 32, 'assetid': '1'}, {'tracking_id': 'untrusted'}):
            with self.assertRaises(ValidationError):
                self.service.auction_reprice_preview(invalid)

    def test_deferred_reprice_job_tracks_background_recovery_in_shared_list(self):
        self.service.closed = threading.Event()
        self.service.updating = False
        payload = {'token': 'a' * 43, 'price': '2.30'}
        self.trading._reprice_previews = {payload['token']: {'steamid': STEAMID}}
        identity = 'b' * 32
        with patch.object(self.trading, 'reprice', side_effect=RepriceDeferred(identity, '后台继续核验')):
            with patch('deskrawl_assistant.auction_tracking.threading.Thread') as worker:
                self.service.auction_reprice_start(payload)
                job = worker.call_args.kwargs['target']
            job()
        job_id = self.service.auction_reprice_job['job_id']
        self.assertRegex(job_id, r'^[a-f0-9]{32}$')
        self.assertEqual(self.service.auction_reprice_job, {'running': False, 'waiting': True,
            'tracking_id': identity, 'message': '后台继续核验', 'job_id': job_id})
        completed = {'running': False, 'message': '已按原设定价格提交重挂'}
        with patch.object(self.trading, 'reprice_recovery_state', return_value=completed) as project:
            value = self.service.managed_auction_tracking()
        project.assert_called_once_with(identity)
        self.assertEqual(value['reprice'], {**completed, 'job_id': job_id})

    def test_stale_native_auction_cache_cannot_resurrect_a_confirmed_sale(self):
        self.trading.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        row = self.trading._journal[0]
        row.update(status='sold', game_instance_id='sold-guid')
        self.trading._save()
        self.service.pending_trade_context.update(native_read_at=time.monotonic(), native={
            'available': True, 'steamid': STEAMID, 'steamid_verified': True, 'server_checked_at': time.time(), 'listings': [
                {'instance_id': 'sold-guid', 'internal_name': 'GoldBar', 'received': False},
                {'instance_id': 'pending-guid', 'internal_name': 'GoldBar', 'received': False}]})
        value = self.service.managed_auction_tracking()
        self.assertEqual(value['total'], 1)
        self.assertEqual(value['items'][0]['id'], 'auction-pending-guid')
        self.assertEqual(value['items'][0]['status'], 'uploaded')
        self.service.pending_trade_context['native_read_at'] -= 21
        value = self.service.managed_auction_tracking()
        self.assertEqual((value['items'][0]['status'], value['items'][0]['previous_status']),
                         ('uncertain', 'uploaded'))


class NativeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.identity = {'steamid': STEAMID, 'session': 'session', 'character_id': 'hero'}
        self.service = AuctionTrackingMixin()
        self.service.guard = threading.RLock()
        self.service.closed = threading.Event()
        self.service.cancelled = threading.Event()
        self.service.updating = False
        self.service.managed = SimpleNamespace(running=False, finishing=False)
        self.service.game_actions_blocked = lambda: False
        self.service.pending_trade_context = dict(self.identity)
        self.service._manual_auction_identity = Mock(return_value=dict(self.identity))
        self.service._manual_auction_capability = Mock()
        self.service._publish = Mock()
        self.service.auction_coordinator = SimpleNamespace(observe=Mock(), wake=threading.Event())
        self.client = self.service.client = Mock(connected=True)
        self.native = {'available': True, 'steamid': STEAMID, 'steamid_verified': True,
                       'listings': [], 'server_checked_at': time.time()+1}
        self.client.auction_state.return_value = self.native
        self.client.execute_game_action.return_value = {'effect_confirmed': True}

    def test_refresh_requests_only_native_list_and_publishes_confirmed_account_response(self):
        self.service._refresh_native_trades()
        args, kwargs = self.client.execute_game_action.call_args
        self.assertEqual(args, ('auction_open', {'refresh_only': True}, self.service.cancelled))
        self.assertTrue(kwargs['validate']())
        self.service._publish.assert_called_once_with(self.client.snapshot.return_value)
        self.assertEqual(self.service.pending_trade_context['native']['listings'], [])
        self.service.auction_coordinator.observe.assert_called_once_with(self.native)
        self.assertTrue(self.service.auction_coordinator.wake.is_set())

    def test_ack_without_complete_fresh_list_never_replaces_previous_state(self):
        for changes in ({'server_checked_at': 0}, {'needs_refresh': True}, {'busy': True},
                        {'steamid': OTHER}, {'steamid_verified': False}, {'listings': None},
                        {'available': False}):
            with self.subTest(changes=changes):
                self.client.auction_state.return_value = {**self.native, **changes}
                with self.assertRaises(ValidationError):
                    self.service._refresh_native_trades()
                self.assertTrue(self.service.pending_trade_context['native']['needs_refresh'])
                self.assertEqual(self.service.pending_trade_context['native']['server_checked_at'], 0)
        self.client.auction_state.return_value = self.native
        self.client.execute_game_action.return_value = {'effect_confirmed': False}
        with self.assertRaises(ValidationError):
            self.service._refresh_native_trades()
        self.service._publish.assert_not_called()
        self.service.auction_coordinator.observe.assert_not_called()

    def background_ready(self):
        self.service.snapshot = {'complete': True}
        self.service.pending_trade_context.update(scene='town', read_at=time.monotonic(), steamid_verified=True)
        self.service.managed.pending = None

    def test_background_force_refresh_is_bounded_and_serialized_while_managed_is_idle(self):
        self.background_ready()
        self.service.managed.running = True
        self.service._refresh_trade_tracking_if_needed()
        self.client.execute_game_action.assert_called_once()
        self.assertEqual(self.client.execute_game_action.call_args.args[1], {'refresh_only': True})
        self.service._refresh_trade_tracking_if_needed()
        self.assertEqual(self.client.execute_game_action.call_count, 1)

    def test_background_refresh_failure_stays_unknown_and_retries_later_without_propagating(self):
        self.background_ready()
        self.service.pending_trade_context['native'] = dict(self.native)
        self.service.pending_trade_context['native']['server_checked_at'] = time.time()-31
        self.client.execute_game_action.side_effect = RuntimeError('offline')
        self.service._refresh_trade_tracking_if_needed()
        self.assertTrue(self.service.pending_trade_context['native']['needs_refresh'])
        self.assertEqual(self.service.pending_trade_context['native']['server_checked_at'], 0)
        self.service._refresh_trade_tracking_if_needed()
        self.client.execute_game_action.assert_called_once()
        scope, at = self.service._native_trade_refresh_attempt
        self.service._native_trade_refresh_attempt = (scope, at-31)
        self.service._refresh_trade_tracking_if_needed()
        self.assertEqual(self.client.execute_game_action.call_count, 2)

    def test_background_refresh_skips_combat_pending_operations_and_recent_server_receipt(self):
        self.background_ready()
        self.service.pending_trade_context['scene'] = 'combat'
        self.service._refresh_trade_tracking_if_needed()
        self.service.pending_trade_context['scene'] = 'town'
        self.service.managed.pending = {'kind': 'synthesize'}
        self.service._refresh_trade_tracking_if_needed()
        self.service.managed.pending = None
        self.service.pending_trade_context['native'] = {**self.native, 'server_checked_at': time.time()}
        self.service._refresh_trade_tracking_if_needed()
        self.client.execute_game_action.assert_not_called()

    def test_account_change_or_resumed_managed_action_rejects_response(self):
        self.service._manual_auction_identity.side_effect = [dict(self.identity), {**self.identity, 'steamid': OTHER}]
        with self.assertRaises(ValidationError):
            self.service._refresh_native_trades()
        self.service._manual_auction_identity.side_effect = None
        def resumed(*args, **kwargs):
            self.service.managed.running = True
            self.assertFalse(kwargs['validate']())
            return {'effect_confirmed': True}
        self.client.execute_game_action.side_effect = resumed
        with self.assertRaises(ValidationError):
            self.service._refresh_native_trades()
        self.service._publish.assert_not_called()
        self.service.auction_coordinator.observe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
