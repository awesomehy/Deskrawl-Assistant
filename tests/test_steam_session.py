from http.cookies import SimpleCookie
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from deskrawl_assistant.steam_session import (
    SteamSession, SteamSessionError, normalise_cookies, parse_wallet, _dpapi, _NoRedirect,
)
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import HTTPSHandler, Request, build_opener as real_build_opener
from urllib.response import addinfourl

STEAMID = '76561198000000001'


def cookies(domain='steamcommunity.com', *, steamid=STEAMID):
    values = []
    for name, value in [('steamLoginSecure', steamid + '%7C%7Csecret-login-token'),
                        ('sessionid', 'abcdef1234567890abcdef12')]:
        cookie = SimpleCookie()
        cookie[name] = value
        cookie[name]['domain'] = domain
        cookie[name]['path'] = '/'
        cookie[name]['secure'] = True
        values.append(cookie)
    return values


def page(steamid=STEAMID, *, app_fee='.1', wallet_default=None):
    wallet = {'wallet_currency': 23, 'wallet_market_minimum': 1,
              'wallet_currency_increment': 1, 'wallet_fee_percent': '.05',
              'wallet_trade_max_balance': 10000000}
    if wallet_default is not None:
        wallet['wallet_publisher_fee_percent_default'] = wallet_default
    app = {} if app_fee is None else {'market_pubfee_rate': app_fee}
    return ('var g_steamID = "' + steamid + '";var g_rgWalletInfo = ' + json.dumps(wallet)
            + ';var g_rgAppContextData = ' + json.dumps({'4623570': app}) + ';')


class _ScriptedHTTPS(HTTPSHandler):
    """Entirely in-memory HTTP responses; this handler never opens a socket."""
    def __init__(self, steps, requests):
        super().__init__()
        self.steps = list(steps)
        self.requests = requests

    def https_open(self, request):
        self.requests.append(request)
        code, location, set_cookies, body = self.steps.pop(0)
        headers = Message()
        if location:
            headers['Location'] = location
        for value in set_cookies:
            headers['Set-Cookie'] = value
        response = addinfourl(BytesIO(body.encode()), headers, request.full_url, code)
        response.msg = 'OK' if code == 200 else 'Redirect'
        return response


class ReadRedirectTests(unittest.TestCase):
    def test_bodyless_get_can_follow_only_same_origin_market_paths(self):
        handler = _NoRedirect()
        for code in (301, 302, 303, 307, 308):
            for source, destination in (
                ('/market/', '/market/eligibilitycheck/?goto=market'),
                ('/market/eligibilitycheck/?goto=market', '/market/'),
            ):
                with self.subTest(code=code, source=source):
                    request = Request('https://steamcommunity.com' + source, method='GET')
                    follow = handler.redirect_request(request, None, code, 'Redirect', Message(),
                                                      'https://steamcommunity.com' + destination)
                    self.assertEqual(follow.get_method(), 'GET')
                    self.assertIsNone(follow.data)
                    self.assertEqual(follow.full_url, 'https://steamcommunity.com' + destination)

    def test_post_head_and_body_cannot_follow_any_redirect(self):
        for code in (301, 302, 303, 307, 308):
            for method, data in (('POST', b'price=1'), ('POST', None), ('HEAD', None), ('GET', b'body')):
                with self.subTest(code=code, method=method, body=data is not None):
                    request = Request('https://steamcommunity.com/market/', data=data, method=method)
                    with self.assertRaises(SteamSessionError):
                        _NoRedirect().redirect_request(request, None, code, 'Redirect', Message(),
                            'https://steamcommunity.com/market/eligibilitycheck/')

    def test_redirect_rejects_other_origins_login_and_unrelated_paths(self):
        destinations = (
            'http://steamcommunity.com/market/',
            'https://steamcommunity.com.attacker.test/market/',
            'https://store.steampowered.com/market/',
            'https://steamcommunity.com:444/market/',
            'https://user@steamcommunity.com/market/',
            'https://steamcommunity.com/login/',
            'https://steamcommunity.com/login/home/?goto=market',
            'https://steamcommunity.com/inventory/',
            'https://steamcommunity.com/market/sellitem/',
            'https://steamcommunity.com/market/eligibilitycheck/extra',
            'https://steamcommunity.com/market/\\anything',
            'https://steamcommunity.com/market/\r\ninjected',
        )
        for destination in destinations:
            with self.subTest(destination=destination):
                request = Request('https://steamcommunity.com/market/', method='GET')
                with self.assertRaises(SteamSessionError):
                    _NoRedirect().redirect_request(request, None, 302, 'Redirect', Message(), destination)

    def test_redirect_limits_bound_hops_without_breaking_one_return_to_market(self):
        handler = _NoRedirect()
        self.assertEqual(handler.max_redirections, 3)
        self.assertEqual(handler.max_repeats, 2)


class SessionTests(unittest.TestCase):
    def test_official_cookie_domains_only(self):
        for domain in ('steamcommunity.com.attacker.test', 'store.steampowered.com', ''):
            with self.assertRaises(SteamSessionError):
                normalise_cookies(cookies(domain))

    def test_plain_frontend_cookie_dict_is_rejected(self):
        with self.assertRaises(SteamSessionError):
            normalise_cookies({'steamLoginSecure': 'secret'})

    def test_only_two_needed_cookies_are_captured(self):
        extra = SimpleCookie()
        extra['tracking'] = 'not-needed'
        extra['tracking']['domain'] = 'steamcommunity.com'
        self.assertEqual(set(normalise_cookies(cookies() + [extra])), {'steamLoginSecure', 'sessionid'})

    def test_logged_in_wallet_identity_must_match_cookie(self):
        with self.assertRaises(SteamSessionError):
            parse_wallet(page('76561198000000002'), STEAMID)

    def test_wallet_unknown_does_not_default_to_reference_currency(self):
        with self.assertRaises(SteamSessionError):
            parse_wallet(page().replace('"wallet_currency": 23', '"wallet_currency": 999'), STEAMID)

    def test_wallet_requires_native_limits(self):
        with self.assertRaises(SteamSessionError):
            parse_wallet(page().replace('"wallet_currency_increment": 1,', ''), STEAMID)

    def test_publisher_fee_falls_back_to_official_wallet_default(self):
        result = parse_wallet(page(app_fee=None, wallet_default='0.10'), STEAMID)
        self.assertEqual(result['publisher_fee'], '0.10')
        self.assertEqual(result['publisher_fee_source'], 'wallet_default')
        self.assertEqual(result['wallet']['wallet_publisher_fee_percent_default'], '0.10')

    def test_explicit_app_publisher_fee_takes_priority(self):
        result = parse_wallet(page(app_fee='.2', wallet_default='.1'), STEAMID)
        self.assertEqual(result['publisher_fee'], '.2')
        self.assertEqual(result['publisher_fee_source'], 'app_context')

    def test_null_app_fee_uses_official_wallet_default(self):
        document = page(app_fee=None, wallet_default='.1').replace(
            '"4623570": {}', '"4623570": {"market_pubfee_rate": null}')
        self.assertEqual(parse_wallet(document, STEAMID)['publisher_fee'], '.1')

    def test_wallet_default_does_not_require_legacy_app_context_assignment(self):
        document = page(app_fee=None, wallet_default='.1').split(';var g_rgAppContextData')[0]
        self.assertEqual(parse_wallet(document, STEAMID)['publisher_fee'], '.1')

    def test_zero_publisher_fees_are_preserved(self):
        for app_fee, default, source in ((0, '.1', 'app_context'), (None, 0, 'wallet_default')):
            with self.subTest(source=source):
                result = parse_wallet(page(app_fee=app_fee, wallet_default=default), STEAMID)
                self.assertEqual(result['publisher_fee'], 0)
                self.assertEqual(result['publisher_fee_source'], source)

    def test_missing_publisher_fee_stays_unknown(self):
        result = parse_wallet(page(app_fee=None), STEAMID)
        self.assertIsNone(result['publisher_fee'])
        self.assertIsNone(result['publisher_fee_source'])

    def test_invalid_app_fee_cannot_fall_back_to_wallet_default(self):
        for fee in ('bad', '', 'NaN', 'Infinity', '-.1', '1.01', True, {}):
            with self.subTest(fee=fee):
                with self.assertRaises(SteamSessionError):
                    parse_wallet(page(app_fee=fee, wallet_default='.1'), STEAMID)

    def test_invalid_selected_wallet_default_is_rejected(self):
        for fee in ('bad', '', 'NaN', 'Infinity', '-.1', '1.01', True, {}):
            with self.subTest(fee=fee):
                with self.assertRaises(SteamSessionError):
                    parse_wallet(page(app_fee=None, wallet_default=fee), STEAMID)

    def test_malformed_app_context_cannot_use_wallet_default(self):
        document = page(app_fee=None, wallet_default='.1').replace('"4623570": {}', '"4623570": []')
        with self.assertRaises(SteamSessionError):
            parse_wallet(document, STEAMID)

    def test_capture_is_encrypted_and_state_has_no_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            def protector(value, *, decrypt=False):
                return value[9:][::-1] if decrypt else b'protected' + value[::-1]
            service = SteamSession(path, transport=lambda *args: page(), protector=protector)
            result = service.capture_session(cookies())
            self.assertEqual(result['steamid'], STEAMID)
            self.assertNotIn('secret-login-token', json.dumps(result))
            self.assertNotIn(b'secret-login-token', path.read_bytes())
            restarted = SteamSession(path, transport=lambda *args: page(), protector=protector)
            self.assertFalse(restarted.state()['verified'])
            self.assertEqual(restarted.verify()['steamid'], STEAMID)
            restarted.logout()
            self.assertFalse(path.exists())

    def test_failed_capture_does_not_persist_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            service = SteamSession(path, transport=lambda *args: '<html>sign in</html>')
            with self.assertRaises(SteamSessionError):
                service.capture_session(cookies())
            self.assertFalse(path.exists())
            self.assertFalse(service.state()['bound'])

    def test_deferred_capture_is_encrypted_without_contacting_steam(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            requests = []
            def transport(*args):
                requests.append(args[0])
                return page()
            def protector(value, *, decrypt=False):
                return value[9:][::-1] if decrypt else b'protected' + value[::-1]
            service = SteamSession(path, transport=transport, protector=protector)
            result = service.capture_session(cookies(), verify_now=False)
            self.assertEqual(requests, [])
            self.assertTrue(result['bound'])
            self.assertFalse(result['verified'])
            self.assertIsNone(result['wallet_currency'])
            self.assertNotIn('secret-login-token', json.dumps(result))
            self.assertNotIn(b'secret-login-token', path.read_bytes())

            restarted = SteamSession(path, transport=transport, protector=protector)
            self.assertTrue(restarted.state()['bound'])
            self.assertFalse(restarted.state()['verified'])
            self.assertEqual(requests, [])
            restarted.verify()
            self.assertEqual(requests, ['GET'])
            self.assertTrue(restarted.state()['verified'])

    def test_deferred_capture_save_failure_leaves_no_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            service = SteamSession(path, protector=lambda raw, **kwargs: b'protected')
            with patch('deskrawl_assistant.steam_session._atomic_write', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    service.capture_session(cookies(), verify_now=False)
            self.assertFalse(service.state()['bound'])
            self.assertFalse(service.state()['verified'])
            self.assertFalse(path.exists())

    def test_logout_during_deferred_capture_does_not_restore_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            service = None
            def protector(raw, **kwargs):
                service.logout()
                return b'protected'
            service = SteamSession(path, protector=protector)
            with self.assertRaises(SteamSessionError):
                service.capture_session(cookies(), verify_now=False)
            self.assertFalse(service.state()['bound'])
            self.assertFalse(service.state()['verified'])
            self.assertFalse(path.exists())

    def test_authenticated_requests_cannot_target_arbitrary_origins(self):
        with tempfile.TemporaryDirectory() as temp:
            service = SteamSession(Path(temp) / 'session.dpapi')
            for path in ('https://attacker.test/', '//attacker.test/', '/\\attacker.test/', '/bad\r\nheader'):
                with self.assertRaises(SteamSessionError):
                    service.request('GET', path)

    def test_authenticated_https_uses_system_proxy_without_changing_origin(self):
        from urllib.request import ProxyHandler
        with tempfile.TemporaryDirectory() as temp:
            service = SteamSession(Path(temp) / 'session.dpapi', transport=lambda *a: page(),
                                   protector=lambda raw, **kwargs: b'protected')
            service.capture_session(cookies())
            service.transport = None
            with patch('urllib.request.getproxies', return_value={'https': 'http://127.0.0.1:7897'}), \
                    patch('deskrawl_assistant.steam_session.build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = b'official response'
                self.assertEqual(service.request('GET', '/market/'), 'official response')
            handlers = build.call_args.args
            proxy = next(handler for handler in handlers if isinstance(handler, ProxyHandler))
            self.assertEqual(proxy.proxies, {'https': 'http://127.0.0.1:7897'})
            self.assertTrue(any(type(handler).__name__ == '_NoRedirect' for handler in handlers))
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://steamcommunity.com/market/')
            self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 15)

    def test_market_eligibility_cookies_are_temporary_and_wallet_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            service = SteamSession(path, protector=lambda raw, **kwargs: b'protected' + raw[::-1])
            service.capture_session(cookies(), verify_now=False)
            saved = path.read_bytes()
            steps = [
                (302, '/market/eligibilitycheck/?goto=market',
                 ['steamCountry=CN; Path=/; Secure', 'Steam_Language=english; Path=/; Secure',
                  'browserid=temporary-browser; Path=/; Secure'], ''),
                (302, '/market/', ['webTradeEligibility=temporary-eligibility; Path=/; Secure'], ''),
                (200, None, [], page()),
            ]
            requests = []
            fake_https = _ScriptedHTTPS(steps + steps, requests)
            def build(*handlers):
                return real_build_opener(*handlers, fake_https)
            with patch('urllib.request.getproxies', return_value={}), \
                    patch('deskrawl_assistant.steam_session.build_opener', side_effect=build):
                wallet = service.verify(force=True)
                service.verify(force=True)
            self.assertEqual(wallet['currency'], 'CNY')
            self.assertTrue(service.state()['verified'])
            self.assertEqual(len(requests), 6)
            for offset in (0, 3):
                start = SimpleCookie(requests[offset].get_header('Cookie', ''))
                eligibility = SimpleCookie(requests[offset + 1].get_header('Cookie', ''))
                returned = SimpleCookie(requests[offset + 2].get_header('Cookie', ''))
                self.assertEqual(set(start), {'steamLoginSecure', 'sessionid'})
                self.assertTrue({'steamCountry', 'Steam_Language', 'browserid'} <= set(eligibility))
                self.assertEqual(returned['webTradeEligibility'].value, 'temporary-eligibility')
                self.assertEqual(returned['steamLoginSecure'].value, start['steamLoginSecure'].value)
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(set(service._cookies), {'steamLoginSecure', 'sessionid'})

    def test_post_and_cross_origin_redirect_never_issue_a_second_request(self):
        cases = [('POST', '/market/sellitem/', 'https://steamcommunity.com/market/')]
        cases.extend(('GET', '/market/', url) for url in (
            'https://attacker.test/market/', 'https://steamcommunity.com/login/home/'))
        for method, path, destination in cases:
            with self.subTest(method=method, destination=destination), tempfile.TemporaryDirectory() as temp:
                service = SteamSession(Path(temp) / 'session.dpapi',
                                       protector=lambda raw, **kwargs: b'protected')
                service.capture_session(cookies(), verify_now=False)
                requests = []
                fake_https = _ScriptedHTTPS([(302, destination, [], ''), (200, None, [], page())], requests)
                with patch('urllib.request.getproxies', return_value={}), \
                        patch('deskrawl_assistant.steam_session.build_opener',
                              side_effect=lambda *handlers: real_build_opener(*handlers, fake_https)):
                    with self.assertRaises(SteamSessionError):
                        service.request(method, path, {'price': 1} if method == 'POST' else None)
                self.assertEqual(len(requests), 1)

    def test_eligibility_loop_is_limited_to_three_followed_hops(self):
        with tempfile.TemporaryDirectory() as temp:
            service = SteamSession(Path(temp) / 'session.dpapi', protector=lambda raw, **kwargs: b'protected')
            service.capture_session(cookies(), verify_now=False)
            requests = []
            steps = [(302, '/market/eligibilitycheck/?goto=market', [], ''),
                     (302, '/market/', [], '')] * 5
            fake_https = _ScriptedHTTPS(steps, requests)
            with patch('urllib.request.getproxies', return_value={}), \
                    patch('deskrawl_assistant.steam_session.build_opener',
                          side_effect=lambda *handlers: real_build_opener(*handlers, fake_https)):
                with self.assertRaises(SteamSessionError):
                    service.request('GET', '/market/')
            self.assertLessEqual(len(requests), 4)

    def test_saved_corrupt_cookie_cannot_inject_request_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            path.write_bytes(json.dumps({'cookies': {'sessionid': 'abcdef1234567890',
                             'steamLoginSecure': STEAMID + '\r\ninjected'}}).encode())
            service = SteamSession(path, protector=lambda raw, **kwargs: raw)
            self.assertFalse(service.state()['bound'])
            with self.assertRaises(SteamSessionError):
                service.request('GET', '/market/')

    def test_logout_during_login_verification_does_not_restore_session(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            service = None
            def transport(*args):
                service.logout()
                return page()
            service = SteamSession(path, transport=transport)
            with self.assertRaises(SteamSessionError):
                service.capture_session(cookies())
            self.assertFalse(service.state()['bound'])
            self.assertFalse(service.state()['verified'])
            self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows current-user DPAPI')
    def test_windows_dpapi_random_payload_roundtrip(self):
        raw = os.urandom(48)
        encrypted = _dpapi(raw)
        self.assertNotEqual(raw, encrypted)
        self.assertEqual(_dpapi(encrypted, decrypt=True), raw)

    def test_post_checks_identity_at_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            submitted = []
            def transport(method, path, data, saved):
                submitted.append(method)
                return page()
            service = SteamSession(Path(temp) / 'session.dpapi', transport=transport,
                                   protector=lambda raw, **kwargs: b'protected')
            service.capture_session(cookies())
            with self.assertRaises(SteamSessionError):
                service.post('/market/sellitem/', {'price': 1}, expected_steamid='76561198000000002')
            self.assertNotIn('POST', submitted)

    def test_stale_verification_failure_cannot_clear_a_newly_bound_account(self):
        with tempfile.TemporaryDirectory() as temp:
            other = '76561198000000002'
            service = None
            def transport(method, path, data, saved):
                if saved['steamLoginSecure'].startswith(STEAMID):
                    service.capture_session(cookies(steamid=other))
                    raise SteamSessionError('旧登录失败')
                return page(other)
            service = SteamSession(Path(temp) / 'session.dpapi', transport=transport,
                                   protector=lambda raw, **kwargs: b'protected')
            with self.assertRaises(SteamSessionError):
                service.capture_session(cookies())
            self.assertTrue(service.state()['verified'])
            self.assertEqual(service.state()['steamid'], other)

    def test_rebinding_same_account_invalidates_old_sale_session(self):
        with tempfile.TemporaryDirectory() as temp:
            submitted = []
            def transport(method, *args):
                submitted.append(method)
                return page()
            service = SteamSession(Path(temp) / 'session.dpapi', transport=transport,
                                   protector=lambda raw, **kwargs: b'protected')
            service.capture_session(cookies())
            generation = service.verify()['binding_generation']
            service.capture_session(cookies())
            with self.assertRaises(SteamSessionError):
                service.post('/market/sellitem/', {'price': 1}, expected_steamid=STEAMID,
                             expected_generation=generation)
            self.assertNotIn('POST', submitted)

    def test_shutdown_during_verification_does_not_persist_new_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            service = None
            def transport(*args):
                service.close()
                return page()
            path = Path(temp) / 'session.dpapi'
            service = SteamSession(path, transport=transport, protector=lambda raw, **kwargs: b'protected')
            with self.assertRaises(SteamSessionError):
                service.capture_session(cookies())
            self.assertFalse(path.exists())


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self):
        for handler in self.handlers:
            handler()


class _Window:
    def __init__(self):
        self.events = SimpleNamespace(closed=_Event())
        self.destroyed = 0

    def destroy(self):
        self.destroyed += 1
        self.events.closed.emit()

    def show(self):
        pass


@unittest.skipUnless(os.name == 'nt', 'native Windows login host')
class LoginWindowTests(unittest.TestCase):
    def test_native_observer_saves_before_verification_and_keeps_binding_after_close(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            window = _Window()
            window.get_current_url = lambda: 'https://steamcommunity.com/market/'
            window.get_cookies = cookies
            requests = []
            def transport(*args):
                requests.append(args[0])
                return page()
            service = SteamSession(path, transport=transport,
                                   protector=lambda raw, **kwargs: b'protected')
            with patch.dict(sys.modules, {'webview': SimpleNamespace(create_window=lambda *a, **k: window)}), \
                    patch('deskrawl_assistant.steam_session.threading.Thread') as thread, \
                    patch('deskrawl_assistant.steam_session.threading.Event') as ended, \
                    patch.object(service, 'capture_session', wraps=service.capture_session) as capture:
                ended.return_value.wait.side_effect = [False]
                service.login_window()
                epoch = service._window_epoch
                thread.call_args.kwargs['target']()
                self.assertEqual(capture.call_count, 1)
                self.assertEqual(capture.call_args.kwargs, {'window_epoch': epoch, 'verify_now': False})
            self.assertEqual(requests, [])
            self.assertEqual(window.destroyed, 1)
            self.assertTrue(path.exists())
            self.assertTrue(service.state()['bound'])
            self.assertFalse(service.state()['verified'])
            self.assertFalse(service.state()['login_open'])
            service.verify()
            self.assertEqual(requests, ['GET'])
            self.assertTrue(service.state()['verified'])

    def test_closing_login_during_deferred_capture_prevents_save(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session.dpapi'
            window = _Window()
            def protector(raw, **kwargs):
                window.events.closed.emit()
                return b'protected'
            service = SteamSession(path, protector=protector)
            with patch.dict(sys.modules, {'webview': SimpleNamespace(create_window=lambda *a, **k: window)}), \
                    patch('deskrawl_assistant.steam_session.threading.Thread'):
                service.login_window()
                with self.assertRaises(SteamSessionError):
                    service.capture_session(cookies(), window_epoch=service._window_epoch, verify_now=False)
            self.assertFalse(path.exists())
            self.assertFalse(service.state()['verified'])
            self.assertFalse(service.state()['bound'])

    def test_successful_login_window_close_reverifies_binding_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            window = _Window()
            submitted = []
            def transport(method, *args):
                submitted.append(method)
                return page()
            service = SteamSession(Path(temp) / 'session.dpapi', transport=transport,
                                   protector=lambda raw, **kwargs: b'protected')
            with patch.dict(sys.modules, {'webview': SimpleNamespace(create_window=lambda *a, **k: window)}), \
                    patch('deskrawl_assistant.steam_session.threading.Thread'):
                service.login_window()
                service.capture_session(cookies(), window_epoch=service._window_epoch)
                old_generation = service.verify()['binding_generation']
                window.destroy()
            self.assertTrue(service.state()['bound'])
            self.assertFalse(service.state()['verified'])
            wallet = service.verify()
            self.assertGreater(wallet['binding_generation'], old_generation)
            service.post('/market/sellitem/', {'price': 1}, expected_steamid=STEAMID,
                         expected_generation=wallet['binding_generation'])
            self.assertIn('POST', submitted)

    def test_logout_while_window_is_being_created_cancels_that_window(self):
        with tempfile.TemporaryDirectory() as temp:
            service = SteamSession(Path(temp) / 'session.dpapi')
            window = _Window()
            def create(*args, **kwargs):
                self.assertIsNone(kwargs['js_api'])
                service.logout()
                return window
            with patch.dict(sys.modules, {'webview': SimpleNamespace(create_window=create)}):
                service.login_window()
            self.assertEqual(window.destroyed, 1)
            self.assertFalse(service.state()['login_open'])

    def test_old_closed_event_does_not_reset_a_new_login_window(self):
        with tempfile.TemporaryDirectory() as temp:
            service = SteamSession(Path(temp) / 'session.dpapi')
            first, second = _Window(), _Window()
            windows = iter([first, second])
            with patch.dict(sys.modules, {'webview': SimpleNamespace(create_window=lambda *a, **k: next(windows))}), \
                    patch('deskrawl_assistant.steam_session.threading.Thread'):
                service.login_window()
                service.logout()
                service.login_window()
                first.events.closed.emit()
            self.assertTrue(service.state()['login_open'])
            self.assertIs(service._window, second)
            service.close()

    def test_closing_login_during_cookie_verification_cancels_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            window = _Window()
            path = Path(temp) / 'session.dpapi'
            def transport(*args):
                window.events.closed.emit()
                return page()
            service = SteamSession(path, transport=transport, protector=lambda raw, **kwargs: b'protected')
            with patch.dict(sys.modules, {'webview': SimpleNamespace(create_window=lambda *a, **k: window)}), \
                    patch('deskrawl_assistant.steam_session.threading.Thread'):
                service.login_window()
                with self.assertRaises(SteamSessionError):
                    service.capture_session(cookies(), window_epoch=service._window_epoch)
            self.assertFalse(path.exists())
            self.assertFalse(service.state()['verified'])
