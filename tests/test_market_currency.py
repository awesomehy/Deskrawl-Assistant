"""Reference conversions are synthetic; no Steam, FX or game requests."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from deskrawl_assistant.market_currency import MarketCurrency, currency_options, parse_rates, valid_currency_id

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp()


def rates(day='2026-10-16'):
    return [{'date':day, 'base':'EUR', 'quote':key, 'rate':rate}
            for key, rate in {'EUR':1, 'HKD':'8', 'CNY':'7.2', 'USD':'1.2'}.items()]


def native(**extra):
    return {'status':'ready', 'eligible':True, 'currency':'HKD', 'currency_id':29,
            'lowest_sell':'10.00', 'highest_buy':'2.27', 'source':'steam_orderbook',
            'fetched_at':NOW, 'stale':False, **extra}


class CurrencyValidationTests(unittest.TestCase):
    def test_options_include_common_current_currencies_not_historical_only(self):
        options = currency_options()
        self.assertEqual([row['code'] for row in options[:4]], ['CNY','USD','HKD','EUR'])
        self.assertNotIn('TWD', [row['code'] for row in options])
        self.assertTrue(all(valid_currency_id(row['id']) for row in options))
        for value in (True, False, '23', 23.0, -1, 999, 30):
            with self.subTest(value=value): self.assertFalse(valid_currency_id(value))
        self.assertTrue(valid_currency_id(0))

    def test_one_consistent_dated_eur_batch_is_required(self):
        self.assertEqual(parse_rates(rates(), NOW)['rates']['CNY'], '7.2')
        for data in ([], {}, rates()+rates()[:1], rates('2026-10-18'),
                     [dict(rates()[0], base='USD')],
                     [dict(rates()[0], rate=True)], [dict(rates()[0], rate=0)],
                     [dict(rates()[0], rate='NaN')], [dict(rates()[0], rate='Infinity')],
                     rates()[:-1]+[dict(rates()[-1], date='2026-10-15')]):
            with self.subTest(data=data), self.assertRaises(ValueError): parse_rates(data, NOW)


class ReferenceCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.transport = Mock(return_value=rates())
        self.service = MarketCurrency(transport=self.transport, clock=lambda:self.now, start_worker=False)
        self.addCleanup(self.service.close)

    def load(self):
        self.assertEqual(self.service.for_quote(native(),23)['status'], 'queued')
        self.service._fetch()

    def test_auto_and_same_currency_do_not_request_rates(self):
        quote = native()
        for currency in (0, 29):
            result = self.service.for_quote(quote, currency)
            self.assertEqual(result, quote)
            result['lowest_sell'] = '99.00'
        self.assertEqual(quote['lowest_sell'], '10.00')
        self.assertFalse(self.service.refreshing)
        self.transport.assert_not_called()

    def test_switching_targets_always_derives_from_immutable_native_amount(self):
        self.load()
        quote = native()
        original = deepcopy(quote)
        cny = self.service.for_quote(quote, 23)
        usd = self.service.for_quote(quote, 1)
        self.assertEqual((cny['lowest_sell'], cny['highest_buy']), ('9.00','2.04'))
        self.assertEqual((usd['lowest_sell'], usd['highest_buy']), ('1.50','0.34'))
        self.assertEqual((cny['currency'], usd['currency']), ('CNY','USD'))
        self.assertEqual(cny['original_lowest_sell'], '10.00')
        self.assertEqual(cny['original_currency'], 'HKD')
        self.assertEqual(cny['exchange_rate_date'], '2026-10-16')
        self.assertTrue(cny['converted'])
        self.assertEqual(quote, original)
        self.transport.assert_called_once_with()

    def test_no_orders_remain_absent_not_zero_price(self):
        self.load()
        result = self.service.for_quote(native(highest_buy=None), 23)
        self.assertIsNone(result['highest_buy'])
        self.assertIsNone(result['original_highest_buy'])
        self.assertEqual(result['lowest_sell'], '9.00')

    def test_missing_rates_do_not_relabel_original_price_and_requests_deduplicate(self):
        for _ in range(10):
            result = self.service.for_quote(native(), 23)
            self.assertEqual(result['status'], 'queued')
            self.assertIsNone(result['lowest_sell'])
            self.assertEqual(result['original_lowest_sell'], '10.00')
        self.transport.assert_not_called()
        self.transport.side_effect = OSError('offline')
        self.service._fetch()
        result = self.service.for_quote(native(), 23)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['highest_buy'])
        self.assertFalse(result['converted'])
        self.assertFalse(self.service.refreshing)
        self.assertEqual(self.service.retry_at, self.now + 900)

    def test_weekend_reference_valid_but_expired_cache_marked_and_bounded(self):
        self.load()
        self.now += 3601
        self.assertTrue(self.service.for_quote(native(),23)['exchange_rate_stale'])
        self.transport.side_effect = OSError('offline')
        self.service._fetch()
        self.assertEqual(self.service.for_quote(native(),23)['lowest_sell'], '9.00')
        self.now += 8*86400
        result = self.service.for_quote(native(),23)
        self.assertFalse(result['converted'])
        self.assertIsNone(result['lowest_sell'])

    def test_unsupported_source_currency_never_uses_wrong_pivot(self):
        self.load()
        result = self.service.for_quote(native(currency='RUB', currency_id=5), 23)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['lowest_sell'])
        self.assertEqual(result['original_currency'], 'RUB')

    def test_queued_or_ineligible_native_quotes_do_not_load_rates(self):
        for changes in ({'status':'queued'}, {'eligible':False}):
            quote = native(**changes)
            self.assertEqual(self.service.for_quote(quote, 23), quote)
        self.assertFalse(self.service.refreshing)

    def test_cache_roundtrip_bad_files_and_wrong_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.service.cache_path = Path(directory)/'rates.json'
            self.load()
            restored = MarketCurrency(cache_path=path, transport=Mock(), clock=lambda:self.now, start_worker=False)
            self.addCleanup(restored.close)
            self.assertEqual(restored.for_quote(native(), 23)['lowest_sell'], '9.00')
            for value in ('broken', '{}', '[]', json.dumps({'schema_version':1,'provider':'FAKE'})):
                path.write_text(value, encoding='utf-8')
                bad = MarketCurrency(cache_path=path, clock=lambda:self.now, start_worker=False)
                self.addCleanup(bad.close)
                self.assertEqual(bad.for_quote(native(),23)['status'], 'queued')

    def test_worker_is_nonblocking_and_shutdown_does_not_publish_pending_result(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(2)
            return rates()
        service = MarketCurrency(transport=blocked, clock=lambda:NOW)
        self.addCleanup(service.close)
        try:
            self.assertEqual(service.for_quote(native(),23)['status'], 'queued')
            self.assertTrue(entered.wait(1))
            self.assertEqual(service.for_quote(native(),23)['status'], 'queued')
            service.close()
            release.set()
            service.worker.join(1)
            self.assertFalse(service.worker.is_alive())
            self.assertIsNone(service.cache)
        finally:
            release.set()


if __name__ == '__main__': unittest.main()
