import json
from pathlib import Path
import tempfile
import threading
import unittest

from deskrawl_assistant.steam_market import APP_ID, MarketCatalog
from deskrawl_assistant.steam_session import SteamSessionError
from deskrawl_assistant.steam_trading import (
    SteamTradingService, buyer_total, fee_parameters, receive_for_total,
)

STEAMID = '76561198000000001'
OTHER = '76561198000000002'


def catalog():
    return MarketCatalog({'schema_version': 1, 'appid': APP_ID, 'items': {
        'GoldBar': {'kind': 'material', 'rarity': 3,
                    'normal': {'definition_id': 5000, 'market_hash_name': 'Gold Bar'}}}})


def wallet():
    return {'steamid': STEAMID, 'currency_id': 23, 'currency': 'CNY', 'publisher_fee': '.10',
            'wallet': {'wallet_market_minimum': 1, 'wallet_currency_increment': 1,
                       'wallet_fee_percent': '.05', 'wallet_trade_max_balance': 10000000}}


def listing(currency=23, low=230, high=227):
    def q(key, value):
        return {'queryKey': key, 'state': {'status': 'success', 'data': value}}
    queries = [q(['market', 'description', APP_ID, 'Gold Bar'],
                 {'appid': APP_ID, 'market_hash_name': 'Gold Bar', 'marketable': 1}),
               q(['market', 'orderbook', APP_ID, 'Gold Bar'],
                 {'eCurrency': currency, 'amtMinSellOrder': low, 'amtMaxBuyOrder': high,
                  'cSellOrders': 3 if low else 0, 'cBuyOrders': 2 if high else 0})]
    data = {'renderContext': {'queryData': json.dumps({'queries': queries})}}
    return '<script type="application/json" id="valve-ssr-data">' + json.dumps(data) + '</script>'


def inventory(*rows):
    assets = [{'appid': APP_ID, 'contextid': '2', 'assetid': identity, 'classid': '42',
               'instanceid': '0', 'amount': str(amount), **extra}
              for identity, amount, extra in rows]
    return {'success': 1, 'assets': assets, 'descriptions': [
        {'appid': APP_ID, 'classid': '42', 'instanceid': '0', 'market_hash_name': 'Gold Bar',
         'marketable': 1, 'market_fee': '.10'}]}


class FakeSession:
    def __init__(self):
        self.wallet = wallet()
        self.inventory = inventory(('1', 1, {}))
        self.quote = listing()
        self.result = {'success': 1, 'listingid': '345'}
        self.requests = []
        self.sales = []
        self.failure = None
        self.on_quote = None

    def state(self):
        return {'bound': True, 'verified': True, 'steamid': self.wallet['steamid'],
                'wallet_currency': 'CNY', 'message': 'mock'}

    def verify(self):
        return self.wallet

    def request(self, method, path, data=None):
        self.requests.append((method, path))
        if self.failure:
            raise self.failure
        if path.startswith('/inventory/'):
            return self.inventory
        if path.startswith('/market/listings/'):
            if self.on_quote:
                self.on_quote()
            return self.quote
        raise AssertionError(path)

    def post(self, path, data, *, expected_steamid=None, expected_generation=None):
        if expected_steamid != self.wallet['steamid']:
            raise SteamSessionError('登录账号已变化', status=401)
        self.sales.append((path, data))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def logout(self):
        return {'bound': False}

    def close(self):
        pass


class FeeTests(unittest.TestCase):
    def test_exact_buyer_total_has_exact_receive(self):
        parameters = fee_parameters(wallet(), '.10')
        self.assertEqual(receive_for_total(230, parameters), 200)
        self.assertEqual(buyer_total(200, parameters), 230)

    def test_official_wallet_minimum_seven_is_not_assumed_one(self):
        value = wallet()
        value['wallet']['wallet_market_minimum'] = '7'
        parameters = fee_parameters(value, '.10')
        self.assertEqual(buyer_total(50, parameters), 64)
        self.assertEqual(receive_for_total(64, parameters), 50)
        self.assertEqual(receive_for_total(230, parameters), 200)

    def test_unrepresentable_total_is_not_silently_discounted(self):
        parameters = fee_parameters(wallet(), '.10')
        self.assertEqual(buyer_total(99, parameters), 112)
        self.assertEqual(buyer_total(100, parameters), 115)
        for total in (113, 114):
            with self.assertRaises(SteamSessionError):
                receive_for_total(total, parameters)

    def test_game_fee_is_required(self):
        with self.assertRaises(SteamSessionError):
            fee_parameters(wallet(), None)

    def test_zero_fee_is_allowed_and_not_minimum_charged(self):
        value = wallet()
        value['wallet']['wallet_fee_percent'] = '0'
        parameters = fee_parameters(value, '0')
        self.assertEqual(buyer_total(100, parameters), 100)
        self.assertEqual(receive_for_total(100, parameters), 100)

    def test_currency_steps_are_enforced(self):
        value = wallet()
        value['wallet'].update(wallet_market_minimum=100, wallet_currency_increment=100)
        parameters = fee_parameters(value, '.1')
        self.assertEqual(buyer_total(1000, parameters), 1200)
        self.assertEqual(receive_for_total(1200, parameters), 1000)
        with self.assertRaises(SteamSessionError):
            receive_for_total(1230, parameters)


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.session = FakeSession()
        self.now = 1000
        self.service = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.settings = {'sell_enabled': True, 'categories': ['gold_bar'], 'retain_count': 0,
                         'price_rule': 'lowest_sell'}

    def tearDown(self):
        self.tmp.cleanup()

    def adopt(self, **extra):
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID, **extra)

    def test_existing_stock_is_never_adopted_by_name(self):
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_manual_scope_cannot_sell_other_captured_or_pending_assets(self):
        self.adopt()  # unrelated captured stock
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar', manual=True)
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        self.service.complete_upload(ticket)
        self.service.sale_once(self.settings, STEAMID, proof_scope=frozenset({ticket}))
        self.assertEqual([row[1]['assetid'] for row in self.session.sales], ['2'])
        self.assertEqual(self.service._journal[0]['status'], 'captured')
        self.assertEqual(self.service.proof_state(ticket, STEAMID)['status'], 'listed')
        self.service.sale_once(self.settings, STEAMID, proof_scope=frozenset({ticket}))
        self.assertEqual(len(self.session.sales), 1)

    def test_unsubmitted_manual_asset_never_becomes_automatic_after_restart(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar', manual=True)
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        self.service.complete_upload(ticket)
        restarted = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertTrue(restarted._journal[0]['manual_only'])
        restarted.sale_once(self.settings, STEAMID, proof_scope=frozenset({ticket}))
        self.assertEqual(self.session.sales[0][1]['assetid'], '2')

    def test_empty_or_invalid_manual_scope_cannot_fall_back_to_all_records(self):
        self.adopt()
        for scope in (frozenset(), {'bad'}, ['a' * 32], frozenset({'bad'})):
            self.service._retry_after = 0
            self.service.sale_once(self.settings, STEAMID, proof_scope=scope)
        self.assertEqual(self.session.sales, [])

    def test_saved_manual_marker_with_wrong_type_fails_closed(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar', manual=True)
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        self.service.complete_upload(ticket)
        self.service._journal[0]['manual_only'] = 'true'
        self.service._save()
        restarted = SteamTradingService(self.path, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restarted.state()['status'], 'journal_error')
        restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_upload_binding_invalidates_on_logout_or_new_session_generation(self):
        self.session.wallet['binding_generation'] = 4
        original_state = self.session.state
        self.session.state = lambda: {**original_state(), 'binding_generation': 4}
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar', manual=True)
        self.assertTrue(self.service.upload_binding_current(ticket, STEAMID))
        self.now += 600
        self.assertFalse(self.service.upload_binding_current(ticket, STEAMID))
        self.now -= 600
        self.session.state = lambda: {**original_state(), 'binding_generation': 5}
        self.assertFalse(self.service.upload_binding_current(ticket, STEAMID))
        self.session.state = lambda: {'bound': False, 'verified': False, 'binding_generation': 4}
        self.assertFalse(self.service.upload_binding_current(ticket, STEAMID))

    def test_disabled_sale_does_not_make_network_requests(self):
        self.service.sale_once({**self.settings, 'sell_enabled': False}, STEAMID)
        self.assertEqual(self.session.requests, [])

    def test_explicit_verified_asset_is_sold_at_net_price(self):
        self.adopt()
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['status'], 'listed')
        self.assertEqual(self.session.sales[0][1]['price'], 200)
        self.assertEqual(self.session.sales[0][1]['assetid'], '1')
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.session.sales), 1)

    def test_highest_buy_uses_exact_buyer_total(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'price_rule': 'highest_buy'}, STEAMID)
        self.assertEqual(self.session.sales[0][1]['price'], 199)

    def test_missing_inventory_fee_uses_verified_wallet_publisher_fee(self):
        self.adopt()
        self.session.inventory['descriptions'][0].pop('market_fee')
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales[0][1]['price'], 200)

    def test_null_inventory_fee_uses_verified_wallet_publisher_fee(self):
        self.adopt()
        self.session.inventory['descriptions'][0]['market_fee'] = None
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales[0][1]['price'], 200)

    def test_explicit_zero_inventory_fee_overrides_wallet_publisher_fee(self):
        self.adopt()
        self.session.inventory['descriptions'][0]['market_fee'] = '0'
        self.session.quote = listing(low=210)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales[0][1]['price'], 200)

    def test_missing_both_publisher_fees_never_submits(self):
        self.adopt()
        self.session.inventory['descriptions'][0].pop('market_fee')
        self.session.wallet['publisher_fee'] = None
        state = self.service.sale_once(self.settings, STEAMID)
        self.assertIn('尚未核实', state['sale_message'])
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)

    def test_invalid_inventory_fee_never_falls_back_or_submits(self):
        self.adopt()
        self.session.inventory['descriptions'][0]['market_fee'] = 'NaN'
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)

    def test_wrong_game_account_never_lists(self):
        self.adopt()
        state = self.service.sale_once(self.settings, OTHER)
        self.assertIn('账号不同', state['sale_message'])
        self.assertEqual(self.session.sales, [])

    def test_unknown_game_identity_never_lists(self):
        state = self.service.sale_once(self.settings, None)
        self.assertIn('尚未确认', state['sale_message'])
        self.assertEqual(self.session.requests, [])

    def test_fx_currency_never_used_for_orders(self):
        self.adopt()
        self.session.quote = listing(currency=29)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_no_order_is_not_a_zero_price(self):
        self.adopt()
        self.session.quote = listing(low=0)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_retired_retain_number_cannot_block_an_authorized_sale(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'retain_count': 1}, STEAMID)
        self.assertEqual(len(self.session.sales), 1)

    def test_category_change_preserves_captured_asset(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'categories': []}, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_legacy_item_keys_cannot_authorize_sale_without_category(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'categories': [], 'item_keys': ['GoldBar']}, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)

    def test_legacy_item_keys_cannot_expand_different_selected_category(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'categories': ['grand_rune'], 'item_keys': ['GoldBar']}, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)

    def test_category_sale_uses_verified_assets_and_exact_protection(self):
        self.adopt()
        config = {**self.settings, 'item_keys': [], 'categories': ['gold_bar'], 'excluded_keys': ['GoldBar']}
        self.service.sale_once(config, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)
        self.service.sale_once({**config, 'excluded_keys': []}, STEAMID)
        self.assertEqual(len(self.session.sales), 1)
        self.service.sale_once({**config, 'excluded_keys': []}, STEAMID)
        self.assertEqual(len(self.session.sales), 1)

    def test_unknown_category_never_submits_sale(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'categories': ['typo']}, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_excluded_item_overrides_positive_whitelist_for_captured_asset(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'excluded_keys': ['GoldBar']}, STEAMID)
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)

    def test_excluded_item_is_not_adopted_from_new_receipt(self):
        self.service.sale_once({**self.settings, 'excluded_keys': ['GoldBar']}, STEAMID,
                               uploaded={'name_key': 'GoldBar', 'assetid': '1'})
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 0)

    def test_invalid_exclusion_configuration_cannot_list(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'excluded_keys': 'GoldBar'}, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_empty_exclusion_leaves_other_sale_settings_intact(self):
        self.adopt()
        self.service.sale_once({**self.settings, 'excluded_keys': [], 'price_rule': 'highest_buy'}, STEAMID)
        self.assertEqual(self.session.sales[0][1]['price'], 199)

    def test_pending_mobile_confirmation_is_not_resubmitted(self):
        self.adopt()
        self.session.result = {'success': 1, 'requires_confirmation': True, 'needs_mobile_confirmation': True}
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['pending_count'], 1)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.session.sales), 1)

    def test_timeout_is_durable_and_not_resubmitted_after_restart(self):
        self.adopt()
        self.session.result = OSError('sensitive server body must not leak')
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['uncertain_count'], 1)
        self.assertNotIn('sensitive', json.dumps(result))
        self.session.result = {'success': True}
        restarted = SteamTradingService(self.path, catalog(), session=self.session)
        restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.session.sales), 1)

    def test_crash_after_intent_is_not_resubmitted(self):
        self.adopt()
        data = json.loads((self.path / 'steam-sale-journal.json').read_text(encoding='utf-8'))
        data['records'][0]['status'] = 'intent'
        (self.path / 'steam-sale-journal.json').write_text(json.dumps(data), encoding='utf-8')
        restarted = SteamTradingService(self.path, catalog(), session=self.session)
        self.assertEqual(restarted.state()['uncertain_count'], 1)
        restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_upload_delta_identifies_only_new_asset(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}))
        self.service.complete_upload(ticket)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales[0][1]['assetid'], '2')

    def test_upload_delta_multiple_assets_is_ambiguous(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}), ('3', 1, {}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket, count=2)
        self.assertEqual(self.service.state()['queued_count'], 0)

    def test_ambiguous_upload_never_recovers_by_guessing_after_inventory_change(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {}), ('3', 1, {}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket, count=1)
        self.session.inventory = inventory(('1', 1, {}), ('3', 1, {}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket, count=1)
        self.assertEqual(self.service.state()['queued_count'], 0)

    def test_explicit_conflicting_game_instance_cannot_complete_upload(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {'game_instance_id': 'other-guid'}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket, expected_instance_id='uploaded-guid')
        self.assertEqual(self.service.state()['queued_count'], 0)

    def test_conflicting_explicit_game_instance_properties_do_not_become_unknown(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 1, {}), ('2', 1,
            {'game_instance_id': 'uploaded-guid', 'properties': {'instanceId': 'other-guid'}}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket, expected_instance_id='uploaded-guid')
        self.assertEqual(self.service.state()['queued_count'], 0)
        self.assertEqual(self.session.sales, [])
        self.session.inventory = inventory(('1', 1, {}), ('2', 1, {'game_instance_id': 'uploaded-guid'}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(ticket, expected_instance_id='uploaded-guid')

    def test_upload_added_to_existing_stack_sells_only_delta(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 11, {}))
        self.service.complete_upload(ticket, count=10)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales[0][1]['amount'], 10)

    def test_later_verified_delta_on_same_asset_can_sell_only_new_amount(self):
        first = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 11, {}))
        self.service.complete_upload(first, count=10)
        self.service.sale_once(self.settings, STEAMID)
        # Steam accepted the first sale, leaving the original one unit.
        self.session.inventory = inventory(('1', 1, {}))
        second = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 4, {}))
        self.service.complete_upload(second, count=3)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual([sale[1]['amount'] for sale in self.session.sales], [10, 3])

    def test_pending_sale_blocks_same_asset_new_delta(self):
        ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 11, {}))
        self.service.complete_upload(ticket, count=10)
        self.session.result = {'success': 1, 'requires_confirmation': True}
        self.service.sale_once(self.settings, STEAMID)
        self.session.inventory = inventory(('1', 1, {}))
        next_ticket = self.service.prepare_upload(STEAMID, 'GoldBar')
        self.session.inventory = inventory(('1', 2, {}))
        with self.assertRaises(SteamSessionError):
            self.service.complete_upload(next_ticket, count=1)
        self.assertEqual(len(self.session.sales), 1)

    def test_game_instanceid_is_not_steam_numeric_instanceid(self):
        self.service.sale_once(self.settings, STEAMID, uploaded={'name_key': 'GoldBar', 'instance_id': '0'})
        self.assertEqual(self.session.sales, [])

    def test_named_game_dynamic_property_can_match_receipt(self):
        self.session.inventory = inventory(('1', 1, {'properties': {'instanceId': 'game-guid'}}))
        self.service.sale_once(self.settings, STEAMID,
                               uploaded={'name_key': 'GoldBar', 'instance_id': 'game-guid'})
        self.assertEqual(len(self.session.sales), 1)

    def test_http_429_backoff(self):
        self.session.failure = SteamSessionError('Steam 请求被限制', status=429, retry_after=600)
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['retry_after'], 1600)
        count = len(self.session.requests)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.session.requests), count)

    def test_upload_baseline_also_respects_rate_limit_backoff(self):
        self.session.failure = SteamSessionError('Steam 请求被限制', status=429, retry_after=600)
        with self.assertRaises(SteamSessionError):
            self.service.prepare_upload(STEAMID, 'GoldBar')
        requests = len(self.session.requests)
        with self.assertRaises(SteamSessionError):
            self.service.prepare_upload(STEAMID, 'GoldBar')
        self.assertEqual(len(self.session.requests), requests)
        self.assertEqual(self.service.state()['retry_after'], 1600)

    def test_stop_during_quote_does_not_start_sale(self):
        self.adopt()
        self.session.on_quote = self.service.close
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_submission_validation_rechecks_after_quote(self):
        self.adopt()
        running = [True]
        self.session.on_quote = lambda: running.__setitem__(0, False)
        self.service.sale_once(self.settings, STEAMID, validate=lambda: running[0])
        self.assertEqual(self.session.sales, [])
        self.assertEqual(self.service.state()['queued_count'], 1)

    def test_validation_after_durable_intent_can_still_cancel_before_post(self):
        self.adopt()
        calls = []
        def validate():
            calls.append(True)
            return len(calls) == 1
        self.service.sale_once(self.settings, STEAMID, validate=validate)
        self.assertEqual(self.session.sales, [])
        raw = json.loads((self.path / 'steam-sale-journal.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['records'][0]['status'], 'captured')

    def test_validation_failure_does_not_submit_or_leak_exception(self):
        self.adopt()
        def validate():
            raise ValueError('private provider data')
        state = self.service.sale_once(self.settings, STEAMID, validate=validate)
        self.assertEqual(self.session.sales, [])
        self.assertNotIn('private provider', json.dumps(state))

    def test_corrupt_journal_does_not_overwrite_or_sell(self):
        path = self.path / 'steam-sale-journal.json'
        path.write_text('broken', encoding='utf-8')
        restarted = SteamTradingService(self.path, catalog(), session=self.session)
        self.assertEqual(restarted.state()['status'], 'journal_error')
        restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(path.read_text(), 'broken')
        self.assertEqual(self.session.sales, [])

    def test_corrupt_journal_remains_blocked_after_logout_and_new_upload(self):
        path = self.path / 'steam-sale-journal.json'
        raw = json.dumps({'schema_version': 1, 'records': [None]})
        path.write_text(raw, encoding='utf-8')
        restarted = SteamTradingService(self.path, catalog(), session=self.session)
        restarted.logout()
        self.assertEqual(restarted.state()['status'], 'journal_error')
        with self.assertRaises(SteamSessionError):
            restarted.prepare_upload(STEAMID, 'GoldBar')
        self.assertEqual(path.read_text(), raw)

    def test_login_account_change_during_quote_never_submits_sale(self):
        self.adopt()
        self.session.on_quote = lambda: self.session.wallet.update(steamid=OTHER)
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.session.sales, [])

    def test_logout_request_during_quote_invalidates_before_waiting_for_operation(self):
        self.adopt()
        invalidated = threading.Event()
        finished = threading.Event()
        def logout():
            self.session.wallet['steamid'] = None
            invalidated.set()
            return {'bound': False}
        self.session.logout = logout
        def request_logout():
            self.service.logout()
            finished.set()
        thread = threading.Thread(target=request_logout)
        def during_quote():
            thread.start()
            self.assertTrue(invalidated.wait(1))
        self.session.on_quote = during_quote
        self.service.sale_once(self.settings, STEAMID)
        thread.join(timeout=1)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.session.sales, [])
