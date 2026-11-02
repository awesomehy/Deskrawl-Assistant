"""Offline routing and transaction checks. All Steam I/O is replaced by fixtures."""
import http.client
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import parse_qs

from deskrawl_assistant.steam_acceleration import CDN_CANDIDATES, ProbeFailure
from deskrawl_assistant.steam_network import (
    AuthenticatedSteamTransport, LoginTunnel, LoginTunnelConnector, login_host_allowed,
)
from deskrawl_assistant.steam_session import SteamSession, SteamSessionError, normalise_cookies
from deskrawl_assistant.steam_trading import SteamTradingService
from tests.test_steam_session import STEAMID, cookies, page
from tests.test_steam_trading import catalog, inventory, listing


def response(status=200, body=b'ok', **headers):
    result = Mock()
    result.status, result.headers = status, headers
    result.read.return_value = body
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.proxy = patch('deskrawl_assistant.steam_network.getproxies',
                           return_value={'https': 'http://127.0.0.1:7890'})
        self.proxy.start();self.addCleanup(self.proxy.stop)
        self.transport = AuthenticatedSteamTransport(lambda: True)
        self.cookies = normalise_cookies(cookies())

    def request(self, method='GET', path='/market/'):
        return self.transport(method, path, {'price': 200} if method == 'POST' else None, self.cookies)

    def test_existing_proxy_first_and_read_only_fallback(self):
        with patch.object(self.transport, '_send', side_effect=[ProbeFailure('timeout'), 'cdn-ok']) as send:
            self.assertEqual(self.request(), 'cdn-ok')
        self.assertEqual([c.args[0] for c in send.call_args_list], [None, CDN_CANDIDATES[0]])
        self.assertEqual(send.call_args_list[0].args[-1]['https'], 'http://127.0.0.1:7890')

    def test_get_can_try_second_cdn(self):
        with patch.object(self.transport, '_send', side_effect=[ProbeFailure('drop'), ProbeFailure('timeout'), 'ok']) as send:
            self.request()
        self.assertEqual([c.args[0] for c in send.call_args_list], [None, *CDN_CANDIDATES])

    def test_switch_off_preserves_existing_proxy_without_cdn_fallback(self):
        self.transport.enabled = lambda: False
        with patch.object(self.transport, '_send', side_effect=ProbeFailure('drop')) as send:
            with self.assertRaises(SteamSessionError): self.request()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[-1], {'https': 'http://127.0.0.1:7890'})

    def test_certificate_content_http_failures_never_switch(self):
        for error in (ProbeFailure('cert', kind='security'),
                      SteamSessionError('limited', status=429), SteamSessionError('redirect', status=401),
                      SteamSessionError('invalid body')):
            with self.subTest(error=type(error).__name__), patch.object(self.transport, '_send', side_effect=error) as send:
                with self.assertRaises(SteamSessionError): self.request()
                self.assertEqual(send.call_count, 1)

    def test_post_does_not_replay_on_timeout_or_disconnect(self):
        for failure in (TimeoutError(), ConnectionResetError(), http.client.RemoteDisconnected()):
            with self.subTest(failure=type(failure).__name__), patch('deskrawl_assistant.steam_network.build_opener') as opener:
                opener.return_value.open.side_effect = URLError(failure)
                with patch('deskrawl_assistant.steam_network.SteamCdnConnection') as cdn:
                    with self.assertRaises(SteamSessionError): self.request('POST', '/market/sellitem/')
                    self.assertEqual(opener.return_value.open.call_count, 1)
                    cdn.assert_not_called()

    def test_post_uses_last_successful_get_route_and_does_not_switch(self):
        with patch.object(self.transport, '_send', side_effect=[ProbeFailure('drop'), 'ok', ProbeFailure('lost response')]) as send:
            self.request()
            with self.assertRaises(SteamSessionError): self.request('POST', '/market/sellitem/')
        self.assertEqual([c.args[0] for c in send.call_args_list], [None, CDN_CANDIDATES[0], CDN_CANDIDATES[0]])
        self.assertEqual(send.call_args.args[1], 'POST')

    def test_post_response_read_timeout_and_cdn_disconnect_are_not_replayed(self):
        with patch('deskrawl_assistant.steam_network.build_opener') as opener, \
             patch('deskrawl_assistant.steam_network.SteamCdnConnection') as cdn:
            accepted = response()
            accepted.read.side_effect = TimeoutError('response lost after request accepted')
            opener.return_value.open.return_value = accepted
            with self.assertRaises(SteamSessionError): self.request('POST', '/market/sellitem/')
            self.assertEqual(opener.return_value.open.call_count, 1)
            cdn.assert_not_called()
        with patch.object(self.transport, '_send', side_effect=[ProbeFailure('proxy unavailable'), 'ok']):
            self.request()
        with patch('deskrawl_assistant.steam_network.SteamCdnConnection') as cdn:
            cdn.return_value.getresponse.side_effect = http.client.RemoteDisconnected()
            with self.assertRaises(SteamSessionError): self.request('POST', '/market/sellitem/')
            self.assertEqual(cdn.call_count, 1)
            self.assertEqual(cdn.return_value.request.call_count, 1)
            self.assertTrue(cdn.return_value.close.called)

    def test_route_selection_is_per_thread(self):
        with patch.object(self.transport, '_send', side_effect=[ProbeFailure('drop'), 'ok', 'ok']) as send:
            self.request()
            worker = threading.Thread(target=lambda: self.request('POST', '/market/sellitem/'))
            worker.start();worker.join(2)
        self.assertIsNone(send.call_args.args[0])

    def test_changing_proxy_invalidates_selected_route(self):
        with patch.object(self.transport, '_send', side_effect=[ProbeFailure('drop'), 'ok', 'ok']) as send:
            self.request()
            with patch('deskrawl_assistant.steam_network.getproxies', return_value={'https': 'http://localhost:9000'}):
                self.request('POST', '/market/sellitem/')
        self.assertIsNone(send.call_args.args[0])

    def test_all_targets_fixed_and_post_endpoint_restricted(self):
        with patch.object(self.transport, '_send') as send:
            for path in ('https://other.test/', '//other.test/', '/bad\r\nCookie:', '/\\other.test/', '/market/#fragment'):
                with self.assertRaises(SteamSessionError): self.request(path=path)
            with self.assertRaises(SteamSessionError): self.request('POST', '/login/')
            send.assert_not_called()

    def test_http_backoff_survives_transport_restart_and_toggle(self):
        for status in (403, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'backoff.json'
                first = AuthenticatedSteamTransport(lambda: True, target)
                with self.assertRaises(SteamSessionError) as error:
                    first._read(response(status, **{'Retry-After': '600'}))
                self.assertGreaterEqual(error.exception.retry_after, 599)
                restarted = AuthenticatedSteamTransport(lambda: False, target)
                with patch.object(restarted, '_send') as send:
                    with self.assertRaises(SteamSessionError) as error:
                        restarted('GET', '/market/', None, self.cookies)
                    self.assertEqual(error.exception.status, 429)
                    send.assert_not_called()

    def test_tls_failure_does_not_expose_cookie_or_proxy_credentials(self):
        with patch('deskrawl_assistant.steam_network.build_opener') as opener:
            opener.return_value.open.side_effect = URLError(ssl.SSLCertVerificationError('secret-login-token user:pass'))
            with self.assertRaises(SteamSessionError) as error: self.request()
        self.assertIn('证书', str(error.exception))
        self.assertNotIn('secret-login-token', str(error.exception))

    def test_cooldown_write_failure_cannot_authorize_fallback(self):
        with patch('deskrawl_assistant.steam_network.build_opener') as opener, \
             patch('deskrawl_assistant.steam_network.SteamCdnConnection') as cdn, \
             patch.object(self.transport.budget, 'observe', side_effect=PermissionError()):
            opener.return_value.open.return_value = response(429)
            with self.assertRaises(SteamSessionError) as error: self.request()
            self.assertEqual(error.exception.status, 429)
            self.assertEqual(opener.return_value.open.call_count, 1)
            cdn.assert_not_called()

    def test_read_rejects_oversized_invalid_encoding_and_redirect(self):
        from deskrawl_assistant.steam_session import MAX_RESPONSE
        for value in (response(302), response(body=b'\xff'), response(body=b'a'*(MAX_RESPONSE+1)),
                      response(**{'Content-Encoding': 'gzip'})):
            with self.assertRaises(SteamSessionError): self.transport._read(value)

    def cdn_responses(self, *replies):
        """Exercise urllib's actual redirect/cookie handlers without network I/O."""
        requests = []
        remaining = iter(replies)
        class Connection:
            sock = None
            def __init__(self, *args): pass
            def set_debuglevel(self, value): pass
            def request(self, method, path, body=None, headers=None, **options):
                requests.append((method, path, headers))
            def getresponse(self):
                stream = Mock()
                stream.makefile.return_value = io.BytesIO(next(remaining))
                reply = http.client.HTTPResponse(stream)
                reply.begin()
                return reply
            def close(self): pass
        return requests, patch('deskrawl_assistant.steam_network.SteamCdnConnection', Connection)

    def test_cdn_preserves_official_qualification_redirects_and_temporary_cookies(self):
        requests, connection = self.cdn_responses(
            b'HTTP/1.1 302 Found\r\nLocation: /market/eligibilitycheck/\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 302 Found\r\nLocation: https://steamcommunity.com:443/market/\r\n'
            b'Set-Cookie: qualification=verified; Path=/; Secure\r\nContent-Length: 0\r\n\r\n',
            b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
        with connection:
            self.assertEqual(self.transport._send(CDN_CANDIDATES[0], 'GET', '/market/', None,
                {'Cookie': 'sessionid=fake; steamLoginSecure=fake'}, {}), 'ok')
        self.assertEqual([request[1] for request in requests],
                         ['/market/', '/market/eligibilitycheck/', '/market/'])
        self.assertIn('qualification=verified', requests[-1][2]['Cookie'])
        self.assertIn('steamLoginSecure=fake', requests[-1][2]['Cookie'])
        self.assertEqual(set(self.cookies), {'sessionid', 'steamLoginSecure'})

    def test_cdn_never_follows_sale_redirect_or_leaks_auth_to_foreign_origin(self):
        for method, path, target in [('POST', '/market/sellitem/', '/market/'),
                                     ('GET', '/market/', 'https://other.test/'),
                                     ('GET', '/market/', 'http://steamcommunity.com/market/')]:
            with self.subTest(method=method, target=target):
                requests, connection = self.cdn_responses(
                    ('HTTP/1.1 302 Found\r\nLocation: ' + target + '\r\nContent-Length: 0\r\n\r\n').encode())
                with connection, self.assertRaises(SteamSessionError):
                    self.transport._send(CDN_CANDIDATES[0], method, path, None,
                                         {'Cookie': 'sessionid=fake; steamLoginSecure=fake'}, {})
                self.assertEqual(len(requests), 1)

    def test_cdn_official_auth_refresh_requires_relogin_without_forwarding_cookies(self):
        destination = 'https://login.steampowered.com/jwt/refresh?redir=https%3A%2F%2Fsteamcommunity.com%2Fmarket%2F'
        requests, connection = self.cdn_responses(
            ('HTTP/1.1 302 Found\r\nLocation: ' + destination + '\r\nContent-Length: 0\r\n\r\n').encode())
        with connection, self.assertRaises(SteamSessionError) as caught:
            self.transport._send(CDN_CANDIDATES[0], 'GET', '/market/', None,
                                 {'Cookie': 'sessionid=fake; steamLoginSecure=fake'}, {})
        self.assertTrue(caught.exception.auth_expired)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1], '/market/')

    def test_cdn_qualification_loop_is_bounded(self):
        reply = b'HTTP/1.1 302 Found\r\nLocation: /market/\r\nContent-Length: 0\r\n\r\n'
        requests, connection = self.cdn_responses(*[reply]*5)
        with connection, self.assertRaises(SteamSessionError):
            self.transport._send(CDN_CANDIDATES[0], 'GET', '/market/', None,
                                 {'Cookie': 'sessionid=fake; steamLoginSecure=fake'}, {})
        self.assertLessEqual(len(requests), 4)


class TradingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.transport = AuthenticatedSteamTransport(lambda: True)
        self.now = 1000
        self.calls, self.posts = [], []
        self.get_failure = None
        self.post_failure = None
        self.post_result = {'success': 1, 'listingid': '345'}
        self.listings_html = ''
        self.validate = None
        self.patcher = patch.object(self.transport, '_send', side_effect=self.send)
        self.patcher.start();self.addCleanup(self.patcher.stop)
        self.proxies = patch('deskrawl_assistant.steam_network.getproxies', return_value={})
        self.proxies.start();self.addCleanup(self.proxies.stop)
        self.session = SteamSession(self.root/'session', transport=self.transport,
            protector=lambda raw, **kw: raw, clock=lambda: self.now)
        self.session.capture_session(cookies())
        self.service = SteamTradingService(self.root, catalog(), session=self.session, clock=lambda: self.now)
        self.settings = {'sell_enabled': True, 'categories': ['gold_bar'], 'price_rule': 'lowest_sell'}
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=STEAMID)
        self.calls.clear()

    def send(self, route, method, path, body, headers, proxies):
        self.calls.append((route, method, path))
        if method == 'POST':
            self.posts.append(parse_qs(body.decode()))
            # Simulates Steam receiving the body, then dropping its response.
            if self.post_failure: raise self.post_failure
            return json.dumps(self.post_result)
        if self.get_failure: raise self.get_failure
        if route is None: raise ProbeFailure('existing proxy disconnected')
        if path == '/market/': return page()
        if path.startswith('/inventory/'): return json.dumps(inventory(('1', 1, {})))
        if path.startswith('/market/listings/'): return listing()
        if path.startswith('/market/mylistings/'):
            return json.dumps({'success': 1, 'total_count': 1, 'results_html': self.listings_html})
        raise AssertionError(path)

    def test_unchanged_price_quantity_and_one_sale_after_read_fallback(self):
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['status'], 'listed')
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]['price'], ['200'])
        self.assertEqual(self.posts[0]['amount'], ['1'])
        self.assertEqual(self.posts[0]['assetid'], ['1'])
        self.assertEqual(self.posts[0]['sessionid'], ['abcdef1234567890abcdef12'])
        self.assertEqual(self.calls[-1][0], CDN_CANDIDATES[0])
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.posts), 1)

    def test_response_lost_persists_uncertain_and_is_not_retried_after_restart(self):
        self.post_failure = ProbeFailure('timeout after request accepted')
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['uncertain_count'], 1)
        self.now += 60
        self.post_failure = None
        self.service.sale_once(self.settings, STEAMID)
        restarted = SteamTradingService(self.root, catalog(), session=self.session, clock=lambda: self.now)
        restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(restarted.state()['uncertain_count'], 1)

    def test_get_disconnect_creates_no_intent_and_can_recover(self):
        self.get_failure = ProbeFailure('all routes disconnected')
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.service._journal[0]['status'], 'captured')
        self.get_failure = None;self.now += 60
        self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(len(self.posts), 1)

    def test_account_stop_and_configuration_rules_still_block_submit(self):
        for settings, identity, validate in (
            ({**self.settings, 'categories': []}, STEAMID, None),
            ({**self.settings, 'excluded_keys': ['GoldBar']}, STEAMID, None),
            (self.settings, '76561198000000002', None),
            (self.settings, STEAMID, lambda: False),
        ):
            with self.subTest(identity=identity, settings=settings):
                self.now += 60
                self.service.sale_once(settings, identity, validate=validate)
                self.assertEqual(self.posts, [])

    def test_pending_mobile_confirmation_and_confirmed_count_use_accelerated_readback(self):
        from deskrawl_assistant.steam_market import APP_ID
        self.post_result['requires_confirmation'] = True
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['pending_count'], 1)
        self.assertEqual(len(self.posts), 1)
        self.listings_html = ('<a onclick="CancelMarketListingConfirmation(\'mylisting_confirmation\','
            f'\'345\',\'{APP_ID}\',\'2\',\'1\')">pending</a>')
        self.now += 61
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['pending_count'], 1)
        self.assertTrue(any(route == CDN_CANDIDATES[0] and path.startswith('/market/mylistings/')
                            for route, method, path in self.calls))
        self.listings_html = (f'<a onclick="RemoveMarketListing(\'mylisting\',\'345\',\'{APP_ID}\','
                              '\'2\',\'1\')">listed</a>')
        self.now += 61
        result = self.service.sale_once(self.settings, STEAMID)
        self.assertEqual(result['pending_count'], 0)
        self.assertEqual(result['counts']['listed'], 1)
        self.assertEqual(len(self.posts), 1)

    def test_pending_journal_survives_but_stale_count_is_hidden_after_restart_and_outage(self):
        self.post_result['requires_confirmation'] = True
        self.service.sale_once(self.settings, STEAMID)
        restarted = SteamTradingService(self.root, catalog(), session=self.session, clock=lambda: self.now)
        self.assertEqual(restarted.state()['pending_count'], 0)
        self.assertEqual(restarted.state()['counts']['pending'], 1)
        self.get_failure = ProbeFailure('readback routes disconnected')
        self.now += 61
        result = restarted.sale_once(self.settings, STEAMID)
        self.assertEqual(result['pending_count'], 0)
        self.assertFalse(result['confirmation_known'])
        self.assertEqual(result['counts']['pending'], 1)
        self.assertEqual(len(self.posts), 1)


class LoginConnectorTests(unittest.TestCase):
    def test_proxy_is_first_and_only_network_errors_can_fall_back(self):
        connector = LoginTunnelConnector()
        stream, returned = Mock(), Mock()
        with patch('deskrawl_assistant.steam_network.getproxies', return_value={'https':'http://localhost:7890'}), \
             patch.object(connector, '_open', side_effect=[TimeoutError(), stream, returned]) as opening, \
             patch('deskrawl_assistant.steam_network.ssl.create_default_context') as context:
            self.assertIs(connector.connect('steamcommunity.com'), returned)
        self.assertEqual(opening.call_args_list[0].args, ('steamcommunity.com', 'http://localhost:7890', None))
        self.assertEqual(opening.call_args.args, ('steamcommunity.com', None, CDN_CANDIDATES[0]))
        context.return_value.wrap_socket.assert_called_once_with(stream, server_hostname='steamcommunity.com')
        stream.sendall.assert_not_called()  # Preflight sends no HTTP or login payload.

    def test_bad_certificate_aborts_before_browser_tunnel(self):
        connector = LoginTunnelConnector()
        with patch('deskrawl_assistant.steam_network.getproxies', return_value={}), \
             patch.object(connector, '_open', return_value=Mock()) as opening, \
             patch('deskrawl_assistant.steam_network.ssl.create_default_context') as context:
            context.return_value.wrap_socket.side_effect = ssl.SSLCertVerificationError()
            with self.assertRaises(ProbeFailure) as error: connector.connect('steamcommunity.com')
            self.assertEqual(error.exception.kind, 'security')
            self.assertEqual(opening.call_count, 1)

    def test_proxy_http_rejection_is_not_bypassed(self):
        connector = LoginTunnelConnector()
        with patch('deskrawl_assistant.steam_network.getproxies', return_value={'https':'http://localhost:7890'}), \
             patch.object(connector, '_open', side_effect=ProbeFailure('auth', kind='http', status=407)) as opening:
            with self.assertRaises(ProbeFailure): connector.connect('steamcommunity.com')
            self.assertEqual(opening.call_count, 1)

    def test_invalid_proxy_is_sanitized_and_not_bypassed(self):
        connector = LoginTunnelConnector()
        for proxy in ('socks5://secret@localhost:1080', 'http://secret@localhost:invalid'):
            with self.subTest(proxy=proxy), self.assertRaises(ProbeFailure) as error:
                connector._open('steamcommunity.com', proxy)
            self.assertEqual(error.exception.kind, 'config')
            self.assertNotIn('secret', str(error.exception))

    def test_login_targets_are_limited_by_domain_boundary(self):
        for host in ('steamcommunity.com', 'login.steampowered.com', 'community.akamai.steamstatic.com'):
            self.assertTrue(login_host_allowed(host))
        for host in ('steamcommunity.com.attacker.test', 'evಿಲ್steamstatic.com', '127.0.0.1', 'example.com'):
            self.assertFalse(login_host_allowed(host))

    def test_window_factory_uses_private_proxy_only_when_switch_is_on(self):
        import sys
        from types import SimpleNamespace
        from deskrawl_assistant.steam_login_window import create_login_window
        fake_webview = SimpleNamespace(create_window=Mock(), windows=[SimpleNamespace(native=object())])
        with patch.dict(sys.modules, webview=fake_webview), \
             patch('deskrawl_assistant.steam_login_window.SteamLoginWindow') as accelerated:
            create_login_window('login', 'https://steamcommunity.com/login/',
                                enabled=lambda: False, profile_directory='unused', js_api=None)
            fake_webview.create_window.assert_called_once()
            accelerated.assert_not_called()
            create_login_window('login', 'https://steamcommunity.com/login/',
                                enabled=lambda: True, profile_directory='unused', js_api=None)
            accelerated.assert_called_once()
            self.assertEqual(fake_webview.create_window.call_count, 1)

    def test_accelerated_login_asks_steam_for_official_assets_without_changing_origin(self):
        from urllib.parse import parse_qs, urlsplit
        from deskrawl_assistant.steam_login_window import accelerated_login_url
        original='https://steamcommunity.com/login/home/?goto=market%2F&l=schinese&_cdn=fastly&_cdn=other'
        parsed=urlsplit(accelerated_login_url(original))
        self.assertEqual((parsed.scheme,parsed.netloc,parsed.path),
                         ('https','steamcommunity.com','/login/home/'))
        self.assertEqual(parse_qs(parsed.query),
                         {'goto':['market/'],'l':['schinese'],'_cdn':['akamai']})
        self.assertEqual(accelerated_login_url(accelerated_login_url(original)),
                         accelerated_login_url(original))

    def test_asset_preference_does_not_change_auth_requests_other_origins_or_blank_test_page(self):
        from deskrawl_assistant.steam_login_window import accelerated_login_url
        for url in ('about:blank','https://steamcommunity.com/login/settoken',
                    'https://steamcommunity.com/jwt/finalizelogin',
                    'https://steamcommunity.com/market/',
                    'https://login.steampowered.com/jwt/refresh',
                    'https://steamcommunity.com.attacker.invalid/login/',
                    'https://steamcommunity.com:8443/login/'):
            self.assertEqual(accelerated_login_url(url),url)


class AuctionWiringTests(unittest.TestCase):
    def test_coordinator_uses_live_preference_for_session_and_login(self):
        from types import SimpleNamespace
        from deskrawl_assistant.auction_service import AuctionServiceMixin
        enabled = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            owner = SimpleNamespace(guard=threading.RLock(), auction_coordinator=None,
                market_data_dir=Path(directory), _market_catalog=catalog, _auction_policy=Mock(),
                _auction_auth_expired=Mock(),
                assistant_preferences=SimpleNamespace(acceleration_enabled=enabled))
            with patch('deskrawl_assistant.auction_service.AuctionCoordinator',
                       side_effect=lambda path, trading, provider, **kw: SimpleNamespace(trading=trading)):
                coordinator = AuctionServiceMixin._auction_service(owner)
                session = coordinator.trading.session
                self.assertIs(session.transport.enabled, enabled)
                self.assertFalse(session.transport.enabled())
                enabled.return_value = True
                self.assertTrue(session.transport.enabled())
                self.assertIs(session.login_window_factory.keywords['enabled'], enabled)
                self.assertIs(AuctionServiceMixin._auction_service(owner), coordinator)


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.peers = []
        self.connector = Mock()
        def connect(host):
            upstream, peer = socket.socketpair()
            peer.settimeout(2);self.peers.append(peer)
            return upstream
        self.connector.connect.side_effect = connect
        self.tunnel = LoginTunnel(self.connector).start()
        self.addCleanup(self.tunnel.close)
        self.addCleanup(lambda: [peer.close() for peer in self.peers])

    def connect(self, request=b'CONNECT steamcommunity.com:443 HTTP/1.1\r\nHost: steamcommunity.com:443\r\n\r\n'):
        sock = socket.create_connection(self.tunnel.server.server_address, timeout=2)
        self.addCleanup(sock.close)
        sock.sendall(request)
        header = b''
        while b'\r\n\r\n' not in header: header += sock.recv(1)
        return sock, header

    def test_opaque_bytes_and_disconnect_do_not_reconnect(self):
        sock, header = self.connect()
        self.assertIn(b'200 Connection Established', header)
        payload = b'\x16\x03\x03fake-TLS-login-data'
        sock.sendall(payload)
        self.assertEqual(self.peers[0].recv(1024), payload)
        self.peers[0].sendall(b'encrypted response')
        self.assertEqual(sock.recv(1024), b'encrypted response')
        self.peers[0].close()
        self.assertEqual(sock.recv(1024), b'')
        self.connector.connect.assert_called_once_with('steamcommunity.com')

    def test_bad_targets_ports_headers_and_plain_http_never_forward(self):
        for request in (b'CONNECT example.com:443 HTTP/1.1\r\n\r\n',
                        b'CONNECT steamcommunity.com:80 HTTP/1.1\r\n\r\n',
                        b'CONNECT steamcommunity.com:443 HTTP/1.1\r\nOrigin: https://example.com\r\n\r\n',
                        b'GET https://steamcommunity.com/ HTTP/1.1\r\n\r\n'):
            sock, header = self.connect(request)
            self.assertNotIn(b'200 Connection Established', header)
        self.connector.connect.assert_not_called()

    def test_connection_failure_before_establishment_is_generic(self):
        self.connector.connect.side_effect = ProbeFailure('secret proxy failure')
        sock, header = self.connect()
        self.assertIn(b'502', header)
        self.assertNotIn(b'secret', header)
        self.connector.connect.assert_called_once()

    def test_close_disconnects_existing_stream_and_releases_port(self):
        sock, _ = self.connect()
        address = self.tunnel.server.server_address
        self.tunnel.close()
        self.assertEqual(sock.recv(1024), b'')
        with self.assertRaises(OSError): socket.create_connection(address, timeout=.1)
        self.assertFalse(self.tunnel.thread.is_alive())


if __name__ == '__main__': unittest.main()
