"""Offline login routing: certificate failures, no POST replay and browser semantics."""
import http.client
import ssl
import threading
import unittest
from unittest.mock import Mock

from deskrawl_assistant.steam_acceleration import ProbeFailure
from deskrawl_assistant.steam_login_transport import CommunityLoginTransport, community_request


class CommunityLoginTests(unittest.TestCase):
    def setUp(self):
        self.first, self.second = Mock(), Mock()
        for connection in (self.first, self.second):
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b'login-page'
            response.getheaders.return_value = [('Content-Type', 'text/html'),
                ('Set-Cookie', 'sessionid=fake; Secure; HttpOnly; SameSite=None'),
                ('Set-Cookie', 'pref=fake; Secure'), ('Transfer-Encoding', 'chunked')]
        self.factory = Mock(side_effect=[self.first, self.second])
        self.transport = CommunityLoginTransport(addresses=('1.1.1.1', '2.2.2.2'), connection_factory=self.factory)
        self.addCleanup(self.transport.close)

    def request(self, method='GET', path='/login/home/', **kwargs):
        return self.transport.request('https://steamcommunity.com' + path, method,
            kwargs.get('headers', [('Cookie', 'sessionid=fake'), ('Origin', 'https://login.steampowered.com'),
                                   ('Host', 'attacker.invalid'), ('Proxy-Authorization', 'must-not-leak')]),
            kwargs.get('body', b''))

    def test_browser_cookies_set_cookies_and_cors_headers_are_preserved(self):
        status, headers, body = self.request('POST', '/login/settoken', body=b'opaque-token')
        self.assertEqual((status, body), (200, b'login-page'))
        self.assertEqual(len([v for k, v in headers if k == 'Set-Cookie']), 2)
        self.assertNotIn('Transfer-Encoding', dict(headers))
        sent = self.first.request.call_args
        self.assertEqual(sent.args, ('POST', '/login/settoken'))
        self.assertEqual(sent.kwargs['body'], b'opaque-token')
        self.assertEqual(sent.kwargs['headers']['Cookie'], 'sessionid=fake')
        self.assertEqual(sent.kwargs['headers']['Host'], 'steamcommunity.com')
        self.assertEqual(sent.kwargs['headers']['Origin'], 'https://login.steampowered.com')
        self.assertNotIn('Proxy-Authorization', sent.kwargs['headers'])
        self.assertLess(self.first.mock_calls.index(unittest.mock.call.connect()),
                        self.first.mock_calls.index(unittest.mock.call.request(*sent.args, **sent.kwargs)))

    def test_only_pre_http_connection_failure_may_change_route(self):
        self.first.connect.side_effect = TimeoutError()
        self.request('POST', '/login/settoken', body=b'opaque-token')
        self.first.request.assert_not_called()
        self.second.request.assert_called_once()
        self.first.close.assert_called_once()

    def test_prepare_selects_first_responsive_http_edge_without_auth_data_or_waiting_for_slow_edge(self):
        slow_finished = threading.Event()
        release_slow = threading.Event()
        def stalled_response():
            try:
                release_slow.wait(2)
                raise TimeoutError('opaque private data must not be retained')
            finally: slow_finished.set()
        self.first.getresponse.side_effect = stalled_response
        self.factory.side_effect = lambda address, timeout: (
            self.first if address == '1.1.1.1' else self.second)
        try:
            self.assertTrue(self.transport.prepare())
            self.assertEqual(self.transport.preferred, '2.2.2.2')
            self.assertFalse(release_slow.is_set())
            for connection in (self.first, self.second):
                args = connection.request.call_args
                if args is not None:
                    self.assertEqual(args.args, ('GET', '/login/home/?_cdn=akamai'))
                    self.assertNotIn('Cookie', args.kwargs['headers'])
                    self.assertNotIn('Authorization', args.kwargs['headers'])
                    self.assertNotIn('body', args.kwargs)
            self.factory.reset_mock()
            self.second.request.reset_mock()
            self.assertEqual(self.request('POST', '/login/settoken', body=b'opaque-token')[0], 200)
            self.assertEqual(self.factory.call_args.args[0], '2.2.2.2')
            self.second.request.assert_called_once()
        finally:
            release_slow.set()
            slow_finished.wait(2)

    def test_prepare_failure_preserves_normal_connection_checks(self):
        self.factory.side_effect = lambda address, timeout: (
            self.first if address == '1.1.1.1' else self.second)
        self.first.connect.side_effect = TimeoutError()
        self.second.getresponse.return_value.status = 503
        self.assertFalse(self.transport.prepare())
        self.assertIsNone(self.transport.preferred)
        self.assertEqual(self.factory.call_count, 2)
        self.first.request.assert_not_called()

    def test_failed_http_request_does_not_pin_a_route_just_because_tls_worked(self):
        self.first.getresponse.side_effect = TimeoutError()
        with self.assertRaises(ProbeFailure): self.request('POST', '/login/settoken')
        self.assertIsNone(self.transport.preferred)
        self.assertEqual(self.factory.call_count, 1)

    def test_closed_transport_does_not_start_public_probes(self):
        self.transport.close()
        self.assertFalse(self.transport.prepare())
        self.factory.assert_not_called()

    def test_official_same_origin_qr_and_password_authentication_posts_are_allowed_once(self):
        from deskrawl_assistant.steam_login_transport import LOGIN_AUTH_PATHS
        for path in sorted(LOGIN_AUTH_PATHS):
            with self.subTest(path=path):
                self.factory.side_effect = None
                self.factory.return_value = self.first
                self.first.request.reset_mock()
                self.assertEqual(self.request('POST', path, body=b'opaque-auth-protobuf')[0], 200)
                self.first.request.assert_called_once()
                self.assertEqual(self.first.request.call_args.kwargs['body'], b'opaque-auth-protobuf')

    def test_um_allowlist_rejects_market_and_other_account_mutations(self):
        for path in ('/um/Authentication/RevokeToken/', '/um/EconService/Trade/',
                     '/um/Authentication/BeginAuthSessionViaQR/extra',
                     '/um/Authentication/BeginAuthSessionViaQR/../RevokeToken/',
                     '/um/ClientMetrics/ReportClientError/'):
            with self.subTest(path=path):
                with self.assertRaises(ProbeFailure): self.request('POST', path)
        self.factory.assert_not_called()

    def test_invalid_certificate_never_sends_or_changes_route(self):
        for error in (ssl.SSLCertVerificationError('fake'), ProbeFailure('hostname', kind='security')):
            with self.subTest(error=type(error).__name__):
                self.first.connect.side_effect = error
                with self.assertRaises(ProbeFailure): self.request()
                self.assertEqual(self.factory.call_count, 1)
                self.first.request.assert_not_called()
                self.factory.reset_mock(side_effect=True)
                self.factory.side_effect = [self.first, self.second]

    def test_post_and_get_never_replay_after_request_starts(self):
        for method in ('POST', 'GET'):
            with self.subTest(method=method):
                self.factory.reset_mock(side_effect=True)
                self.factory.side_effect = [self.first, self.second]
                self.first.getresponse.side_effect = http.client.RemoteDisconnected()
                with self.assertRaises(ProbeFailure): self.request(method, '/login/settoken')
                self.assertEqual(self.factory.call_count, 1)

    def test_http_rejection_is_returned_once_to_browser(self):
        self.first.getresponse.return_value.status = 429
        status, _, _ = self.request()
        self.assertEqual(status, 429)
        self.assertEqual(self.factory.call_count, 1)

    def test_browser_follows_only_official_https_redirects(self):
        response = self.first.getresponse.return_value
        response.status = 302
        response.getheaders.return_value = [('Location', 'https://login.steampowered.com/jwt/refresh')]
        self.assertEqual(self.request()[0], 302)
        for url in ('https://steamcommunity.com.evil.invalid/', 'http://steamcommunity.com/',
                    'https://user@steamcommunity.com/', 'https://steamcommunity.com:8443/'):
            with self.subTest(url=url):
                self.factory.side_effect = None
                self.factory.return_value = self.first
                response.getheaders.return_value = [('Location', url)]
                with self.assertRaises(ProbeFailure): self.request()

    def test_other_hosts_credentials_ports_and_economic_posts_are_rejected(self):
        for url in ('https://evil.invalid/', 'http://steamcommunity.com/',
                    'https://user@steamcommunity.com/', 'https://steamcommunity.com:8443/'):
            self.assertFalse(community_request(url))
            with self.assertRaises(ProbeFailure): self.transport.request(url, 'GET', [])
        with self.assertRaises(ProbeFailure): self.request('POST', '/market/sellitem/')
        self.factory.assert_not_called()

    def test_oversized_bodies_and_header_injection_do_not_connect(self):
        with self.assertRaises(ProbeFailure): self.request(body=b'x' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ProbeFailure): self.request(headers=[('Cookie', 'fake\r\nInjected: yes')])
        self.factory.assert_not_called()

    def test_close_rejects_new_requests_and_does_not_expose_prior_cookies(self):
        self.transport.close()
        with self.assertRaises(ProbeFailure) as raised: self.request()
        self.assertNotIn('sessionid', str(raised.exception))
        self.factory.assert_not_called()


if __name__ == '__main__': unittest.main()
