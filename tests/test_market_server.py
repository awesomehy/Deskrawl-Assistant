"""Loopback-only settings/quote API validation, without outbound requests."""
import json
import unittest
from unittest.mock import Mock

from tests import test_web_server as fixtures


class MarketRouteTests(unittest.TestCase):
    setUp = fixtures.HttpTests.setUp
    finish = fixtures.HttpTests.finish
    request = fixtures.HttpTests.request

    def test_settings_get_and_post_require_local_session_and_narrow_payload(self):
        self.service.market_settings = Mock(return_value={'currency_id':0,'currencies':[]})
        self.service.set_market_currency = Mock(return_value={'currency_id':23,'currencies':[]})
        headers = {'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        self.assertEqual(self.request('/api/market/settings')[0],403)
        self.assertEqual(self.request('/api/market/settings',{'currency_id':23})[0],403)
        self.assertEqual(self.request('/api/market/settings?url=bad',headers=headers)[0],400)
        self.assertEqual(self.request('/api/market/settings',{'currency_id':23,'url':'bad'},headers)[0],400)
        self.service.set_market_currency.assert_not_called()
        code, _, body = self.request('/api/market/settings',headers=headers)
        self.assertEqual(code,200)
        self.assertEqual(json.loads(body)['currency_id'],0)
        self.assertEqual(self.request('/api/market/settings',{'currency_id':23},headers)[0],200)
        self.service.set_market_currency.assert_called_once_with(23)

    def test_quotes_do_not_accept_client_supplied_urls_or_currency(self):
        self.service.market_quotes = Mock(return_value={'items':[]})
        headers = {'Origin':self.url,'X-Assistant-Token':'test-session-token'}
        for data in ({'items':[],'currency':23}, {'items':[],'url':'bad'}):
            self.assertEqual(self.request('/api/market/quotes',data,headers)[0],400)
        self.service.market_quotes.assert_not_called()
        self.assertEqual(self.request('/api/market/quotes',{'items':['selection']},headers)[0],200)
        self.service.market_quotes.assert_called_once_with(['selection'])


if __name__ == '__main__': unittest.main()
