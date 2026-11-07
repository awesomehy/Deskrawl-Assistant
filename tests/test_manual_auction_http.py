"""Manual batch HTTP boundary tests; no game or Steam requests."""
import json
import unittest
from unittest.mock import Mock

from tests import test_web_server as fixtures


class ManualAuctionHttpTests(unittest.TestCase):
    setUp = fixtures.HttpTests.setUp
    finish = fixtures.HttpTests.finish
    request = fixtures.HttpTests.request

    def headers(self):
        return {'Origin': self.url, 'X-Assistant-Token': 'test-session-token'}

    def test_manual_mutations_require_session_and_same_origin(self):
        bodies = {
            'preview': {'items': ['local-selection'], 'mode': 'upload',
                        'price_rule': 'lowest_sell', 'allow_locked': False},
            'start': {'token': 'preview-only-token'},
            'cancel': {},
        }
        for action, body in bodies.items():
            method = Mock(return_value={})
            setattr(self.service, 'manual_auction_' + action, method)
            for headers in ({'Origin': self.url},
                            {'Origin': 'https://outside.example',
                             'X-Assistant-Token': 'test-session-token'}):
                with self.subTest(action=action, headers=headers):
                    self.assertEqual(self.request('/api/auction/manual/' + action,
                                                  body, headers)[0], 403)
            method.assert_not_called()

    def test_managed_tracking_is_local_authenticated_and_query_free(self):
        self.service.managed_auction_tracking = Mock(return_value={'items': [], 'total': 0})
        self.assertEqual(self.request('/api/auction/managed/state')[0], 403)
        self.assertEqual(self.request('/api/auction/managed/state?run=other', headers=self.headers())[0], 400)
        self.assertEqual(self.request('/api/auction/managed/state', headers=self.headers())[0], 200)
        self.service.managed_auction_tracking.assert_called_once_with()

    def test_native_list_refresh_requires_origin_session_and_no_query_controls(self):
        self.service.auction_tracking_refresh = Mock(return_value={'message': '刷新中'})
        path = '/api/auction/tracking/refresh'
        self.assertEqual(self.request(path, {}, {'Origin': self.url})[0], 403)
        self.assertEqual(self.request(path, {}, {'Origin': 'https://outside.example',
                         'X-Assistant-Token': 'test-session-token'})[0], 403)
        self.assertEqual(self.request(path + '?steamid=other', {}, self.headers())[0], 400)
        self.service.auction_tracking_refresh.assert_not_called()
        self.assertEqual(self.request(path, {}, self.headers())[0], 200)
        self.service.auction_tracking_refresh.assert_called_once_with({})

    def test_reprice_mutations_require_origin_and_session_and_pass_only_explicit_payload(self):
        for action, payload in [('preview', {'tracking_id': 'a' * 32}),
                                ('start', {'token': 'b' * 43, 'price': '2.30'})]:
            method = Mock(return_value={'running': True})
            setattr(self.service, 'auction_reprice_' + action, method)
            path = '/api/auction/reprice/' + action
            self.assertEqual(self.request(path, payload, {'Origin': self.url})[0], 403)
            self.assertEqual(self.request(path, payload, {'Origin': 'https://outside.example',
                             'X-Assistant-Token': 'test-session-token'})[0], 403)
            method.assert_not_called()
            self.assertEqual(self.request(path, payload, self.headers())[0], 200)
            method.assert_called_once_with(payload)

    def test_manual_state_is_authenticated_and_has_no_query_controls(self):
        self.service.manual_auction_state = Mock(return_value={
            'running': False, 'phase': 'completed', 'items': [
                {'name': '测试符文', 'status': 'pending_confirmation'}]})
        self.assertEqual(self.request('/api/auction/manual/state')[0], 403)
        self.assertEqual(self.request('/api/auction/manual/state?start=true',
                                      headers=self.headers())[0], 400)
        code, _, body = self.request('/api/auction/manual/state', headers=self.headers())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['items'][0]['status'], 'pending_confirmation')
        self.service.manual_auction_state.assert_called_once_with()

    def test_preview_and_start_return_service_result_without_inventing_success(self):
        preview = {'items': ['selected'], 'mode': 'sell',
                   'price_rule': 'highest_buy', 'allow_locked': False}
        self.service.manual_auction_preview = Mock(return_value={
            'token': 'short-lived-preview', 'ready_count': 1, 'skipped_count': 0})
        self.service.manual_auction_start = Mock(return_value={
            'running': True, 'phase': 'processing', 'completed': 0, 'total': 1})
        code, _, body = self.request('/api/auction/manual/preview', preview, self.headers())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['ready_count'], 1)
        self.service.manual_auction_preview.assert_called_once_with(preview)
        request = {'token': 'short-lived-preview'}
        code, _, body = self.request('/api/auction/manual/start', request, self.headers())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['completed'], 0)
        self.service.manual_auction_start.assert_called_once_with(request)

    def test_cancel_only_requests_cancellation(self):
        self.service.manual_auction_cancel = Mock(return_value={
            'running': True, 'phase': 'cancelling', 'can_cancel': False})
        code, _, body = self.request('/api/auction/manual/cancel', {}, self.headers())
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['running'])

    def test_manual_assets_are_served_with_correct_mime(self):
        for file, mime in (('inventory-auction.js', 'text/javascript'),
                           ('auction-tracking.js', 'text/javascript'),
                           ('inventory-auction.css', 'text/css')):
            with self.subTest(file=file):
                code, headers, body = self.request('/' + file)
                self.assertEqual(code, 200)
                self.assertTrue(headers['Content-Type'].startswith(mime))
                self.assertTrue(body)


if __name__ == '__main__':
    unittest.main()
