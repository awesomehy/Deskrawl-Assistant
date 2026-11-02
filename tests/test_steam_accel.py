"""Offline-only checks: no Steam requests and no running-assistant access."""
from contextlib import contextmanager
from io import BytesIO
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from deskrawl_assistant.steam_acceleration import (
    CdnRoute, ExistingRoute, LocalRelay, MarketProbe, NoRedirect, ProbeFailure,
    RequestBudget, SteamCdnConnection, read_response, upstream_tls_context,
    verify_steam_hostname,
)
from deskrawl_assistant.steam_market import APP_ID, MAX_DOCUMENT_BYTES, MarketCatalog


SAMPLE = Path(__file__).resolve().parents[1] / 'tests/fixtures/steam-acceleration-market.json'


def sample_item():
    return MarketCatalog.load(SAMPLE).resolve({
        'internal_name': 'LegendaryNeck8', 'rarity': 3,
        'bound': False, 'is_black_mist': True,
    })[0]


def document(name=None):
    name = name or sample_item().market_hash_name
    def query(kind, value):
        return {'queryKey': ['market', kind, APP_ID, name],
                'state': {'status': 'success', 'data': value}}
    queries = [query('description', {
        'appid': APP_ID, 'market_hash_name': name, 'marketable': True,
        'type': '[Black Mist] Necklace',
    }), query('orderbook', {
        'eCurrency': 29, 'amtMinSellOrder': 1234, 'amtMaxBuyOrder': 1200,
        'cSellOrders': 7, 'cBuyOrders': 9,
    })]
    value = {'renderContext': {'queryData': json.dumps({'queries': queries})}}
    return '<script id="valve-ssr-data" type="application/json">' + json.dumps(value) + '</script>'


def route(*, fail=None, name='existing_proxy'):
    obj = Mock(name=name)
    obj.name = name
    obj.get.side_effect = fail
    obj.get.return_value = document()
    return obj


class FakeTime:
    def __init__(self):
        self.value = 1000.0
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds

    def budget(self, path=None):
        return RequestBudget(path, clock=self.now, wall=self.now, sleep=self.sleep)


class Response(BytesIO):
    def __init__(self, body=b'', status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class RouteTests(unittest.TestCase):
    def test_real_local_item_mapping_is_exact(self):
        item = sample_item()
        self.assertEqual(item.definition_id, 3062)
        self.assertEqual(item.market_hash_name, 'Prismatic Heart (Black Mist)')

    def test_working_existing_proxy_does_not_start_relay(self):
        preferred, upstream, factory = route(), route(), Mock()
        probe = MarketProbe(preferred, upstream, relay_factory=factory)
        quote = probe.run(sample_item())
        self.assertEqual(quote['lowest_sell'], '12.34')  # synthetic, never live data
        self.assertEqual(quote['currency'], 'HKD')
        self.assertEqual(probe.route, 'existing_proxy')
        factory.assert_not_called()
        upstream.get.assert_not_called()

    def test_failed_proxy_uses_actual_loopback_server_and_closes_it(self):
        preferred = route(fail=ProbeFailure('existing_connection_failed'))
        upstream = route()
        relays = []

        @contextmanager
        def factory(item, target):
            with LocalRelay(item, target) as relay:
                relays.append(relay)
                self.assertEqual(relay.server.server_address[0], '127.0.0.1')
                yield relay

        before = dict(os.environ)
        probe = MarketProbe(preferred, upstream, relay_factory=factory)
        self.assertEqual(probe.run(sample_item())['highest_buy'], '12.00')
        self.assertEqual(probe.route, 'local_reverse_proxy')
        upstream.get.assert_called_once_with(sample_item())
        self.assertEqual(dict(os.environ), before)
        self.assertFalse(relays[0].thread.is_alive())
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', relays[0].port), timeout=0.1)

    def test_only_network_failure_allows_fallback(self):
        for kind, status in [('http', 403), ('http', 429), ('http', 500),
                             ('security', None), ('content', None), ('cooldown', None)]:
            with self.subTest(kind=kind, status=status):
                preferred = route(fail=ProbeFailure('test', kind=kind, status=status))
                factory = Mock()
                with self.assertRaises(ProbeFailure):
                    MarketProbe(preferred, route(), relay_factory=factory).run(sample_item())
                factory.assert_not_called()

    def test_wrong_item_never_produces_quote_or_retries(self):
        preferred, factory = route(), Mock()
        preferred.get.return_value = document('Some Other Item (Black Mist)')
        with self.assertRaises(ProbeFailure) as caught:
            MarketProbe(preferred, route(), relay_factory=factory).run(sample_item())
        self.assertEqual(caught.exception.kind, 'content')
        factory.assert_not_called()

    def test_fallback_http_error_is_preserved_without_more_routes(self):
        upstream = route(fail=ProbeFailure('steam_http_error', kind='http', status=429, retry_after=300))
        probe = MarketProbe(route(fail=ProbeFailure('offline')), upstream)
        with self.assertRaises(ProbeFailure) as caught:
            probe.run(sample_item())
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 300))
        upstream.get.assert_called_once()

    def test_existing_proxy_snapshot_does_not_replace_global_opener(self):
        import urllib.request
        before = urllib.request._opener
        proxies = {'https': 'http://127.0.0.1:12345'}
        preferred = ExistingRoute(FakeTime().budget(), proxies=proxies)
        proxies['https'] = 'http://example.invalid'
        self.assertEqual(preferred.proxies['https'], 'http://127.0.0.1:12345')
        self.assertIs(urllib.request._opener, before)

    def test_existing_http_error_records_cooldown(self):
        budget = FakeTime().budget()
        preferred = ExistingRoute(budget, proxies={})
        preferred.opener = Mock()
        preferred.opener.open.side_effect = HTTPError(
            sample_item().market_url, 429, 'limited', {'Retry-After': '600'}, BytesIO())
        with self.assertRaises(ProbeFailure) as caught:
            preferred.get(sample_item())
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 600))

    def test_real_http_connect_proxy_failure_falls_back_without_proxy_loop(self):
        requests = []

        class FailingProxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                requests.append((self.path, dict(self.headers)))
                self.send_error(502)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), FailingProxy)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            proxy = {'https': f'http://127.0.0.1:{server.server_port}'}
            preferred = ExistingRoute(FakeTime().budget(), proxies=proxy)
            with patch('urllib.request.proxy_bypass', return_value=False):
                probe = MarketProbe(preferred, route())
                self.assertEqual(probe.run(sample_item())['status'], 'ready')
            self.assertEqual(probe.route, 'local_reverse_proxy')
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], 'steamcommunity.com:443')
            self.assertFalse(any(key.lower() == 'cookie' for key in requests[0][1]))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_all_redirects_are_blocked(self):
        with self.assertRaises(ProbeFailure) as caught:
            NoRedirect().redirect_request(None, None, 302, None, {}, 'https://example.invalid/')
        self.assertEqual(caught.exception.code, 'market_redirect_blocked')


class RelayBoundaryTests(unittest.TestCase):
    def request(self, relay, path='/listing', headers=None, method='GET'):
        connection = http.client.HTTPConnection('127.0.0.1', relay.port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            with connection.getresponse() as response:
                response.read()
                return response.status
        finally:
            connection.close()

    def test_rejects_unauthorized_arbitrary_urls_cookies_and_browser_origins(self):
        upstream = route()
        with LocalRelay(sample_item(), upstream) as relay:
            auth = {'X-Deskrawl-Probe': relay.token}
            cases = [('/listing', {}), ('/http://example.invalid/', auth),
                     ('/listing', {**auth, 'Host': 'example.invalid'}),
                     ('/listing', {**auth, 'Cookie': 'sessionid=not-a-real-session'}),
                     ('/listing', {**auth, 'Authorization': 'Bearer fake'}),
                     ('/listing', {**auth, 'Origin': 'https://example.invalid'})]
            for path, headers in cases:
                with self.subTest(path=path, header_names=list(headers)):
                    self.assertEqual(self.request(relay, path, headers), 403)
            self.assertEqual(self.request(relay, headers=auth, method='CONNECT'), 501)
            upstream.get.assert_not_called()

    def test_one_authorized_fetch_only_and_fixed_target(self):
        upstream = route()
        with LocalRelay(sample_item(), upstream) as relay:
            relay.get(sample_item())
            self.assertEqual(self.request(relay, headers={'X-Deskrawl-Probe': relay.token}), 409)
            upstream.get.assert_called_once()

    def test_loopback_client_ignores_broken_system_proxy(self):
        # Test-local environment restoration; never writes user settings.
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1',
                                    'HTTPS_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
            with LocalRelay(sample_item(), route()) as relay:
                self.assertEqual(relay.get(sample_item()), document())


class TlsAndCdnTests(unittest.TestCase):
    def test_chain_verification_stays_required_with_sni_suppressed(self):
        context = upstream_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()), 0)

    def test_exact_origin_san_required_not_alias_or_cn(self):
        verify_steam_hostname({'subjectAltName': [('DNS', 'steamcommunity.com')]})
        for certificate in [{}, {'subjectAltName': [('DNS', '*.steamcommunity.com')]},
                            {'subjectAltName': [('DNS', 'steamcommunity-a.akamaihd.net')]},
                            {'subject': ((('commonName', 'steamcommunity.com'),),)}]:
            with self.subTest(certificate=certificate), self.assertRaises(ProbeFailure):
                verify_steam_hostname(certificate)

    def test_invalid_origin_certificate_closes_socket_before_http_send(self):
        plain, secured, context = Mock(), Mock(), Mock()
        secured.getpeercert.return_value = {'subjectAltName': [('DNS', 'example.invalid')]}
        context.wrap_socket.return_value = secured
        with patch('deskrawl_assistant.steam_acceleration.upstream_tls_context', return_value=context), \
             patch('deskrawl_assistant.steam_acceleration.socket.create_connection', return_value=plain):
            connection = SteamCdnConnection('104.91.87.202', timeout=2)
            with self.assertRaises(ProbeFailure):
                connection.request('GET', '/market/listings/4623570/test')
        context.wrap_socket.assert_called_once_with(plain, server_hostname=None)
        secured.sendall.assert_not_called()
        secured.close.assert_called()

    def test_cdn_429_does_not_try_next_address(self):
        connection = Mock()
        connection.getresponse.return_value = Response(status=429)
        with patch('deskrawl_assistant.steam_acceleration.SteamCdnConnection', return_value=connection) as factory:
            with self.assertRaises(ProbeFailure) as caught:
                CdnRoute(FakeTime().budget()).get(sample_item())
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(factory.call_count, 1)

    def test_cdn_connection_failure_can_use_next_candidate_after_interval(self):
        first, second = Mock(), Mock()
        first.request.side_effect = TimeoutError()
        second.getresponse.return_value = Response(document().encode())
        fake = FakeTime()
        with patch('deskrawl_assistant.steam_acceleration.SteamCdnConnection', side_effect=[first, second]):
            self.assertEqual(CdnRoute(fake.budget()).get(sample_item()), document())
        self.assertEqual(fake.delays, [15])

    def test_oversize_and_compressed_documents_are_rejected(self):
        for response in [Response(b'x' * (MAX_DOCUMENT_BYTES + 1)),
                         Response(b'compressed', headers={'Content-Encoding': 'gzip'})]:
            with self.subTest(headers=response.headers), self.assertRaises(ProbeFailure) as caught:
                read_response(response, FakeTime().budget())
            self.assertEqual(caught.exception.kind, 'content')


class BackoffTests(unittest.TestCase):
    def test_remote_request_interval_is_shared(self):
        fake = FakeTime()
        budget = fake.budget()
        budget.before_request()
        budget.before_request()
        self.assertEqual(fake.delays, [15])

    def test_429_persists_across_cli_restarts(self):
        fake = FakeTime()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cooldown.json'
            budget = fake.budget(path)
            budget.before_request()
            budget.observe(429, '600')
            with self.assertRaises(ProbeFailure) as caught:
                fake.budget(path).before_request()
            self.assertEqual(caught.exception.kind, 'cooldown')
            fake.value += 601
            fake.budget(path).before_request()

    def test_403_and_bad_retry_after_still_back_off_five_minutes(self):
        fake = FakeTime()
        budget = fake.budget()
        budget.observe(403, 'invalid')
        self.assertEqual(budget.blocked_until - fake.now(), 300)

    def test_restart_also_preserves_spacing(self):
        fake = FakeTime()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cooldown.json'
            fake.budget(path).before_request()
            fake.budget(path).before_request()
            self.assertEqual(fake.delays, [15])

    def test_retry_after_is_capped_at_one_hour(self):
        fake = FakeTime()
        budget = fake.budget()
        budget.observe(429, '999999')
        self.assertEqual(budget.blocked_until - fake.now(), 3600)

    def test_invalid_persisted_timestamps_fail_closed(self):
        fake = FakeTime()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cooldown.json'
            path.write_text('{"blocked_until": NaN, "last_request": 0}', encoding='utf-8')
            with self.assertRaises(ProbeFailure) as caught:
                fake.budget(path).before_request()
            self.assertEqual(caught.exception.kind, 'config')


if __name__ == '__main__':
    unittest.main()
