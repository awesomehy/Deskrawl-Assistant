import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from deskrawl_assistant.steam_market import (
    APP_ID, MarketCatalog, MarketDataError, MarketItem, MarketUnavailable,
    SteamMarketPrices, parse_listing,
)


def catalog_data():
    return {'schema_version': 1, 'appid': APP_ID, 'items': {
        'GoldBar': {'kind': 'material', 'rarity': 3,
                    'normal': {'definition_id': 5000, 'market_hash_name': 'Gold Bar'}},
        'DivineRing1': {'kind': 'equipment', 'rarity': 4,
                        'normal': {'definition_id': 1000, 'market_hash_name': "Sage's Ring"}},
        'LegendaryBoots1': {'kind': 'equipment', 'rarity': 3,
                           'black_mist': {'definition_id': 3004,
                                          'market_hash_name': 'Ancient Stride (Black Mist)'}},
        'LegendaryRune_Test': {'kind': 'rune', 'rarity': 3,
                               'normal': {'definition_id': 8000, 'market_hash_name': 'Test Rune (Grand)'}},
    }}


def row(key='GoldBar', **kwargs):
    return {'internal_name': key, 'rarity': 3, 'container': 'inventory', **kwargs}


def document(name='Gold Bar', *, appid=APP_ID, marketable=True, currency=23,
             lowest=230, highest=227, sell_count=20, buy_count=25, item_type='[Legendary] Commodity',
             description=True, orderbook=True, extra_queries=()):
    queries = []
    def query(key, value):
        return {'queryKey': key, 'state': {'status': 'success', 'data': value}}
    if description:
        queries.append(query(['market', 'description', APP_ID, name],
                             {'appid': appid, 'market_hash_name': name,
                              'marketable': marketable, 'type': item_type}))
    if orderbook:
        queries.append(query(['market', 'orderbook', APP_ID, name],
                             {'eCurrency': currency, 'amtMinSellOrder': lowest,
                              'amtMaxBuyOrder': highest, 'cSellOrders': sell_count,
                              'cBuyOrders': buy_count}))
    queries.extend(extra_queries)
    value = {'route': '/market/listings/:appid/:name/:bucket?',
             'renderContext': {'queryData': json.dumps({'queries': queries})}}
    return '<html><script type="application/json" id="valve-ssr-data">' + json.dumps(value) + '</script></html>'


class MarketCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = MarketCatalog(catalog_data())

    def test_known_commodity_uses_exact_shipped_candidate(self):
        item, status = self.catalog.resolve(row())
        self.assertEqual(status, 'candidate')
        self.assertEqual(item.market_hash_name, 'Gold Bar')
        self.assertEqual(item.market_url, 'https://steamcommunity.com/market/listings/4623570/Gold%20Bar')

    def test_normal_equipment_is_not_mist_quote(self):
        self.assertIsNone(self.catalog.resolve(row('LegendaryBoots1', bound=False, is_black_mist=False))[0])
        item, _ = self.catalog.resolve(row('LegendaryBoots1', bound=False, is_black_mist=True))
        self.assertEqual(item.variant, 'black_mist')

    def test_unknown_binding_is_not_treated_as_marketable(self):
        for key, rarity in (('DivineRing1', 4), ('LegendaryRune_Test', 3)):
            with self.subTest(key=key):
                self.assertEqual(self.catalog.resolve(row(key, rarity=rarity))[1], 'unverified')
                self.assertEqual(self.catalog.resolve(row(key, rarity=rarity, bound=True))[1], 'bound')

    def test_unbound_high_quality_instances_are_candidates(self):
        for key, rarity in (('DivineRing1', 4), ('LegendaryRune_Test', 3)):
            self.assertIsNotNone(self.catalog.resolve(row(key, rarity=rarity, bound=False, is_black_mist=False))[0])

    def test_equipped_and_issues_are_excluded(self):
        self.assertEqual(self.catalog.resolve(row('DivineRing1', rarity=4, bound=False, container='equipped'))[1], 'bound')
        self.assertEqual(self.catalog.resolve(row(issues=['bad']))[1], 'unverified')

    def test_unknown_names_never_become_search_queries(self):
        self.assertEqual(self.catalog.resolve(row('https://example.invalid/'))[1], 'unsupported')

    def test_native_base_rarity_is_accepted(self):
        value = row()
        value['base_rarity'] = value.pop('rarity')
        self.assertIsNotNone(self.catalog.resolve(value)[0])

    def test_malformed_catalog_is_rejected(self):
        for change in ({'appid': 730}, {'schema_version': 2}, {'items': []}):
            with self.subTest(change=change), self.assertRaises(MarketDataError):
                MarketCatalog({**catalog_data(), **change})
        value = catalog_data()
        value['items']['GoldBar']['normal']['market_hash_name'] = 'Gold\nBar'
        with self.assertRaises(MarketDataError):
            MarketCatalog(value)

    def test_real_extracted_definition_mapping(self):
        data = json.loads((Path(__file__).resolve().parents[1] / 'data/steam-market-catalog.json').read_text(encoding='utf-8'))
        catalog = MarketCatalog(data)
        self.assertEqual(catalog.items['GoldBar']['normal']['definition_id'], 5000)
        self.assertEqual(catalog.items['UpgradeCommodity1']['normal']['market_hash_name'], 'Consecration Stone')
        self.assertEqual(catalog.items['LegendaryBoots1']['black_mist']['definition_id'], 3004)
        self.assertNotIn('MovementPotion1', catalog.items)
        self.assertNotIn('GemRuby1', catalog.items)


class ParseMarketListingTests(unittest.TestCase):
    def setUp(self):
        self.item = MarketCatalog(catalog_data()).resolve(row())[0]

    def test_live_orderbook_uses_both_sides_not_median(self):
        history = {'queryKey': ['market', 'pricehistory', APP_ID, 'Gold Bar'],
                   'state': {'status': 'success', 'data': {'price_median': 9999}}}
        result = parse_listing(document(extra_queries=[history]), self.item)
        self.assertEqual((result['lowest_sell'], result['highest_buy']), ('2.30', '2.27'))
        self.assertEqual((result['currency'], result['currency_id']), ('CNY', 23))

    def test_currency_is_what_steam_returned(self):
        result = parse_listing(document(currency=29), self.item)
        self.assertEqual(result['currency'], 'HKD')
        self.assertEqual(result['highest_buy'], '2.27')

    def test_empty_buy_or_sell_orders_are_not_zero_price(self):
        result = parse_listing(document(highest=0, buy_count=0), self.item)
        self.assertIsNone(result['highest_buy'])
        self.assertEqual(result['lowest_sell'], '2.30')
        result = parse_listing(document(lowest=None, sell_count=0, highest=None, buy_count=0), self.item)
        self.assertIsNone(result['lowest_sell'])

    def test_names_app_id_and_marketable_must_match(self):
        for values in ({'name': 'Gold Bar FAKE'}, {'appid': 730}, {'marketable': False},
                       {'description': False}, {'orderbook': False}, {'currency': 999}):
            with self.subTest(values=values), self.assertRaises(MarketDataError):
                parse_listing(document(**values), self.item)

    def test_bad_numeric_and_inconsistent_orders_are_not_displayed(self):
        for values in ({'lowest': '230'}, {'highest': True}, {'sell_count': -1},
                       {'lowest': 230, 'sell_count': 0}, {'highest': 0, 'buy_count': 2}):
            with self.subTest(values=values), self.assertRaises(MarketDataError):
                parse_listing(document(**values), self.item)

    def test_equipment_rarity_is_also_validated(self):
        item = MarketCatalog(catalog_data()).resolve(row('DivineRing1', rarity=4, bound=False, is_black_mist=False))[0]
        with self.assertRaises(MarketDataError):
            parse_listing(document(name=item.market_hash_name, item_type='[Rare] Ring'), item)
        self.assertEqual(parse_listing(document(name=item.market_hash_name, item_type='[Divine] Ring'), item)['status'], 'ready')

    def test_missing_ssr_and_median_only_are_unmatched(self):
        for value in ('<html>Median 99.90; starting at 1.00</html>', document(orderbook=False), '<script id="valve-ssr-data" type="application/json">{bad}</script>'):
            with self.subTest(value=value[:40]), self.assertRaises(MarketDataError):
                parse_listing(value, self.item)

    def test_duplicate_exact_orderbooks_are_rejected(self):
        extra = {'queryKey': ['market', 'orderbook', APP_ID, 'Gold Bar'],
                 'state': {'status': 'success', 'data': {}}}
        with self.assertRaises(MarketDataError):
            parse_listing(document(extra_queries=[extra]), self.item)


class MarketCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.catalog = MarketCatalog(catalog_data())
        self.calls = []
        self.service = SteamMarketPrices(self.catalog, transport=self.transport, clock=lambda: self.now,
                                         start_worker=False, cache_ttl=300, error_ttl=60)

    def tearDown(self):
        self.service.close()

    def transport(self, item):
        self.calls.append(item.market_hash_name)
        return document(item.market_hash_name)

    def consume_one(self):
        self.service._fetch(self.service.pending.popleft())

    def test_queue_deduplicates_and_never_blocks_on_http(self):
        for _ in range(10):
            self.assertEqual(self.service.for_item(row())['status'], 'queued')
        self.assertEqual(len(self.service.pending), 1)
        self.assertEqual(self.calls, [])
        self.consume_one()
        result = self.service.for_item(row())
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['stale'])
        self.assertFalse(result['refreshing'])
        self.assertEqual(len(self.calls), 1)

    def test_return_is_detached_from_cache(self):
        self.service.for_item(row())
        self.consume_one()
        result = self.service.for_item(row())
        result['lowest_sell'] = '99999.00'
        self.assertEqual(self.service.for_item(row())['lowest_sell'], '2.30')

    def test_stale_quote_retained_but_marked_during_refresh(self):
        self.service.for_item(row())
        self.consume_one()
        self.now += 301
        result = self.service.for_item(row())
        self.assertTrue(result['stale'])
        self.assertTrue(result['refreshing'])
        self.assertEqual(result['lowest_sell'], '2.30')

    def test_negative_cache_avoids_repeated_bad_mapping_requests(self):
        self.service.transport = lambda item: document('Another Item')
        self.service.for_item(row())
        self.consume_one()
        self.assertEqual(self.service.for_item(row())['status'], 'unmatched')
        self.now += 61
        self.service.for_item(row())
        self.assertEqual(len(self.service.pending), 0)
        self.now += 1800
        self.service.for_item(row())
        self.assertEqual(len(self.service.pending), 1)

    def test_429_sets_global_backoff_and_error_ttl(self):
        def limited(item):
            raise MarketUnavailable('rate limited', status=429, retry_after=600)
        self.service.transport = limited
        self.service.for_item(row())
        self.consume_one()
        result = self.service.for_item(row())
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['retry_at'], self.now + 600)
        self.assertEqual(len(self.service.pending), 0)

    def test_last_success_survives_failed_refresh_with_retry_delay(self):
        self.service.for_item(row())
        self.consume_one()
        self.now += 301
        self.service.for_item(row())
        def offline(item):
            raise OSError('do not publish the remote URL or account details')
        self.service.transport = offline
        self.consume_one()
        result = self.service.for_item(row())
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['stale'])
        self.assertEqual(result['fetched_at'], 1000)
        self.assertIn('refresh_error', result)
        self.assertEqual(len(self.service.pending), 0)
        self.now += 61
        self.service.for_item(row())
        self.assertEqual(len(self.service.pending), 1)

    def test_unexpected_transport_error_does_not_leave_permanent_pending(self):
        self.service.transport = lambda item: 1 / 0
        self.service.for_item(row())
        self.consume_one()
        self.assertEqual(self.service.for_item(row())['status'], 'unavailable')
        self.assertFalse(self.service.pending_names)

    def test_unsupported_or_bound_items_do_not_queue(self):
        self.assertFalse(self.service.for_item(row('MovementPotion1'))['eligible'])
        self.assertFalse(self.service.for_item(row('DivineRing1', rarity=4, bound=True))['eligible'])
        self.assertEqual(len(self.service.pending), 0)

    def test_close_prevents_new_requests(self):
        self.service.close()
        self.service.for_item(row())
        self.assertEqual(len(self.service.pending), 0)

    def test_public_quote_cache_round_trip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'quotes.json'
            self.service.cache_path = path
            self.service.for_item(row())
            self.consume_one()
            cached = SteamMarketPrices(self.catalog, cache_path=path, clock=lambda: self.now, start_worker=False)
            try:
                self.assertEqual(cached.for_item(row())['highest_buy'], '2.27')
                self.assertEqual(cached.for_item(row())['status'], 'ready')
            finally:
                cached.close()
            for contents in ('{broken}', '[]', 'null', '{"schema_version":1,"appid":4623570,"quotes":[]}'):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding='utf-8')
                    cached = SteamMarketPrices(self.catalog, cache_path=path, start_worker=False)
                    try:
                        self.assertEqual(cached.for_item(row())['status'], 'queued')
                    finally:
                        cached.close()

    def test_worker_is_serial_and_close_wakes_wait(self):
        entered, release = threading.Event(), threading.Event()
        seen = []
        def blocked(item):
            seen.append(item.market_hash_name)
            entered.set()
            release.wait(2)
            return document(item.market_hash_name)
        service = SteamMarketPrices(self.catalog, transport=blocked, request_gap=0)
        try:
            result = service.for_item(row())
            self.assertEqual(result['status'], 'queued')
            self.assertTrue(entered.wait(1))
            # Cached state remains immediately readable while HTTP is in flight.
            self.assertTrue(service.for_item(row())['refreshing'])
            self.assertEqual(seen, ['Gold Bar'])
            release.set()
            deadline = time.monotonic() + 2
            while service.for_item(row())['status'] != 'ready' and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(service.for_item(row())['status'], 'ready')
        finally:
            release.set()
            service.close()
            service.worker.join(1)
        self.assertFalse(service.worker.is_alive())


if __name__ == '__main__':
    unittest.main()
