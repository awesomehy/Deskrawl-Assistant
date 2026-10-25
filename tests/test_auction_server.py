import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from tests import test_web_server as fixtures
from deskrawl_assistant.auction_service import AuctionServiceMixin


class AuctionRouteTests(unittest.TestCase):
    setUp = fixtures.HttpTests.setUp
    finish = fixtures.HttpTests.finish
    request = fixtures.HttpTests.request

    def test_state_requires_local_token_and_no_arbitrary_parameters(self):
        self.service.auction_state = Mock(return_value={'steam': {'authenticated': False}})
        headers = {'Origin': self.url, 'X-Assistant-Token': 'test-session-token'}
        self.assertEqual(self.request('/api/auction/state')[0], 403)
        self.assertEqual(self.request('/api/auction/state?url=bad', headers=headers)[0], 400)
        code, _, body = self.request('/api/auction/state', headers=headers)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)['steam']['authenticated'])
        self.service.auction_state.assert_called_once_with(desktop=False)

    def test_login_only_native_desktop_and_never_accepts_credentials(self):
        self.service.auction_login = Mock(return_value={})
        headers = {'Origin': self.url, 'X-Assistant-Token': 'test-session-token'}
        self.assertEqual(self.request('/api/auction/login', {}, headers)[0], 400)
        self.service.auction_login.assert_not_called()
        self.server.desktop = Mock()
        for data in ({'password': 'bad'}, {'cookies': []}, {'token': 'bad'}):
            self.assertEqual(self.request('/api/auction/login', data, headers)[0], 400)
        self.service.auction_login.assert_not_called()
        self.assertEqual(self.request('/api/auction/login', {}, headers)[0], 200)
        self.service.auction_login.assert_called_once_with()

    def test_logout_requires_token_and_empty_body(self):
        self.service.auction_logout = Mock()
        self.service.auction_state = Mock(return_value={})
        headers = {'Origin': self.url, 'X-Assistant-Token': 'test-session-token'}
        self.assertEqual(self.request('/api/auction/logout', {})[0], 403)
        self.assertEqual(self.request('/api/auction/logout', {'url':'bad'}, headers)[0], 400)
        self.assertEqual(self.request('/api/auction/logout', {}, headers)[0], 200)
        self.service.auction_logout.assert_called_once_with()


class SavedAuctionBindingTests(unittest.TestCase):
    def test_status_restores_saved_binding_without_opening_login_or_enabling_auction(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            coordinator = Mock()
            coordinator.state.return_value = {'steam': {'bound': True, 'authenticated': False}}
            service = SimpleNamespace(market_data_dir=directory, _auction_service=Mock(return_value=coordinator))
            value = AuctionServiceMixin.auction_state(service, desktop=True)
            self.assertEqual(value['steam'], {})
            service._auction_service.assert_not_called()
            (directory / 'steam-session.dpapi').write_bytes(b'encrypted-test-data')
            value = AuctionServiceMixin.auction_state(service, desktop=True)
            self.assertTrue(value['steam']['bound'])
            self.assertFalse(value['steam']['authenticated'])
            service._auction_service.assert_called_once_with()
            coordinator.trading.login_window.assert_not_called()


if __name__ == '__main__': unittest.main()
