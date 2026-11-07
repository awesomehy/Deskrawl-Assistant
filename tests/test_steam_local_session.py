"""Native session acceptance tests; no live Steam writes or credentials."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from deskrawl_assistant.steam_client_profile import ClientIdentity, STEAM_ID_BASE
from deskrawl_assistant.steam_local_session import (
    LocalSteamSessionProvider, LocalSessionSnapshot, LocalSessionUnavailable, _cookie_values, _aes_gcm,
)
from deskrawl_assistant.steam_session import SteamSession, SteamSessionError

SID = '76561198000000001'
VALUES = {'steamLoginSecure': SID + '%7C%7Ctest-only-token', 'sessionid': 'abcdef1234567890abcdef1234'}
IDENTITY = ClientIdentity('online', 42, 123, int(SID) - STEAM_ID_BASE, Path('test-steam'))


def page(sid=SID):
    return ('var g_steamID="' + sid + '";var g_rgWalletInfo=' + json.dumps({
        'wallet_currency': 23, 'wallet_market_minimum': 1, 'wallet_currency_increment': 1,
        'wallet_fee_percent': '.05', 'wallet_trade_max_balance': 10000000}) + ';')


def database(version=23, rows=None):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE meta (key TEXT, value TEXT)')
    db.execute('INSERT INTO meta VALUES (?,?)', ('version', str(version)))
    db.execute('CREATE TABLE cookies(host_key TEXT,name TEXT,path TEXT,is_secure INT,encrypted_value BLOB)')
    if rows is None:
        rows = [('steamcommunity.com', name, '/', 1, value) for name, value in VALUES.items()]
    for host, name, path, secure, value in rows:
        prefix = hashlib.sha256(host.encode()).digest() if version >= 24 else b''
        db.execute('INSERT INTO cookies VALUES (?,?,?,?,?)', (host, name, path, secure, b'v10' + bytes(28) + prefix + value.encode()))
    raw = db.serialize()
    db.close()
    return raw


class CacheTests(unittest.TestCase):
    def read(self, raw):
        return _cookie_values(raw, b'unused', lambda key, value: value[31:])

    def test_only_exact_community_auth_cookies_are_decrypted(self):
        raw = database(rows=[*(('steamcommunity.com', n, '/', 1, v) for n, v in VALUES.items()),
            ('attacker.steamcommunity.com', 'steamLoginSecure', '/', 1, 'foreign-cookie-not-decrypted'),
            ('store.steampowered.com', 'secret', '/', 1, 'foreign-cookie-not-decrypted')])
        decrypt = Mock(side_effect=lambda key, value: value[31:])
        self.assertEqual(_cookie_values(raw, b'unused', decrypt), VALUES)
        self.assertEqual(decrypt.call_count, 2)

    def test_both_host_variants_and_host_bound_schema(self):
        for version in (23, 24):
            self.assertEqual(self.read(database(version)), VALUES)
        self.assertEqual(self.read(database(rows=[('.steamcommunity.com', n, '/', 1, v) for n, v in VALUES.items()])), VALUES)

    def test_unknown_schema_and_corrupt_database_are_rejected(self):
        for raw in (b'not sqlite', database(25), database(0)):
            with self.assertRaises(LocalSessionUnavailable):
                self.read(raw)

    def test_unknown_locking_or_crypto_errors_expose_no_underlying_details(self):
        decrypt = Mock(side_effect=ValueError('SECRET-TOKEN filesystem-private-path'))
        with self.assertRaises(LocalSessionUnavailable) as caught:
            _cookie_values(database(), b'unused', decrypt)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('private-path', str(caught.exception))

    def test_duplicate_accounts_nonroot_cookie_and_insecure_auth_are_rejected(self):
        base = [('steamcommunity.com', n, '/', 1, v) for n, v in VALUES.items()]
        bad = [base + [('.steamcommunity.com', 'steamLoginSecure', '/', 1, '76561198000000002%7C%7Cother')],
               [(h, n, '/market/', s, v) for h, n, p, s, v in base],
               [(h, n, p, 0, v) for h, n, p, s, v in base]]
        for rows in bad:
            with self.assertRaises(LocalSessionUnavailable):
                self.read(database(rows=rows))

    def test_missing_and_control_character_values_are_rejected(self):
        for values in ({'steamLoginSecure': VALUES['steamLoginSecure']}, {**VALUES, 'sessionid': VALUES['sessionid'] + '\0'}):
            with self.assertRaises(LocalSessionUnavailable):
                self.read(database(rows=[('steamcommunity.com', n, '/', 1, v) for n, v in values.items()]))

    @unittest.skipUnless(os.name == 'nt', 'Windows CNG')
    def test_cng_authenticates_aes256_gcm_before_returning_plaintext(self):
        # NIST AES-256 GCM, all-zero key/nonce/plaintext, 16-byte plaintext.
        encrypted = b'v10' + bytes(12) + bytes.fromhex('cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919')
        self.assertEqual(_aes_gcm(bytes(32), encrypted), bytes(16))
        with self.assertRaises(ValueError):
            _aes_gcm(bytes(32), encrypted[:-1] + bytes([encrypted[-1] ^ 1]))


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.backend.identity.return_value = IDENTITY
        self.cache = Mock()
        self.cache.return_value.read.return_value = VALUES
        self.provider = LocalSteamSessionProvider(self.backend, self.cache)

    def test_reads_only_active_account_and_does_not_include_cookies_in_repr(self):
        snapshot = self.provider.snapshot()
        self.cache.assert_called_once_with(IDENTITY)
        self.assertEqual(snapshot.cookies, VALUES)
        self.assertNotIn('test-only-token', repr(snapshot))

    def test_not_running_or_logged_out_never_reads_cache(self):
        for state in ('unknown', 'not_running', 'logged_out'):
            self.backend.identity.return_value = ClientIdentity(state)
            with self.assertRaises(LocalSessionUnavailable):
                self.provider.snapshot()
        self.cache.assert_not_called()

    def test_other_account_cookie_and_switch_during_read_are_rejected(self):
        self.cache.return_value.read.return_value = {**VALUES, 'steamLoginSecure': '76561198000000002%7C%7Cother'}
        with self.assertRaises(LocalSessionUnavailable):
            self.provider.snapshot()
        self.cache.return_value.read.return_value = VALUES
        self.backend.identity.side_effect = [IDENTITY, ClientIdentity('logged_out')]
        with self.assertRaises(LocalSessionUnavailable):
            self.provider.snapshot()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'session.dpapi'
        self.provider = Mock()
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, dict(VALUES))
        self.provider.current_identity.return_value = IDENTITY
        self.transport = Mock(return_value=page())
        self.now = 1000
        self.session = SteamSession(self.path, transport=self.transport, local_provider=self.provider,
                                    protector=lambda raw, **kw: raw, clock=lambda: self.now)

    def test_verify_native_in_memory_without_persisting_it(self):
        self.session.refresh_local()
        state = self.session.state()
        self.assertTrue(state['verified'])
        self.assertEqual(state['source'], 'local_steam')
        self.assertEqual(state['steamid'], SID)
        self.assertFalse(self.path.exists())
        self.assertNotIn('test-only-token', json.dumps(state))
        self.assertEqual(self.transport.call_args.args[:3], ('GET', '/market/', None))

    def test_native_snapshot_is_not_treated_as_verified_on_rejection(self):
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        self.session.refresh_local()
        self.assertFalse(self.session.state()['verified'])
        self.assertEqual(self.session.state()['local_status'], 'rejected')
        self.assertFalse(self.path.exists())

    def test_native_failure_preserves_existing_saved_login_and_file(self):
        raw = json.dumps({'cookies': VALUES}).encode()
        self.path.write_bytes(raw)
        s = SteamSession(self.path, transport=self.transport, local_provider=self.provider,
                         protector=lambda raw, **kw: raw)
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        s.refresh_local()
        self.assertEqual(s.state()['source'], 'saved_login')
        self.assertTrue(s.state()['bound'])
        self.assertFalse(s.state()['expired'])
        self.assertEqual(self.path.read_bytes(), raw)

    def test_native_expiry_never_deletes_manual_file_even_if_cookie_same(self):
        self.path.write_bytes(json.dumps({'cookies': VALUES}).encode())
        s = SteamSession(self.path, transport=self.transport, local_provider=self.provider,
                         protector=lambda raw, **kw: raw)
        s.refresh_local()
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        with self.assertRaises(SteamSessionError):
            s.verify(force=True)
        self.assertTrue(self.path.exists())

    def test_switch_or_logout_cancels_post_before_any_economic_request(self):
        self.session.refresh_local()
        generation = self.session.state()['binding_generation']
        self.transport.reset_mock()
        self.provider.current_identity.return_value = ClientIdentity('logged_out')
        with self.assertRaises(SteamSessionError):
            self.session.post('/market/sellitem/', {'price': 1}, expected_steamid=SID, expected_generation=generation)
        self.transport.assert_not_called()
        self.assertFalse(self.session.state()['verified'])

    def test_switch_during_network_verification_does_not_adopt_candidate(self):
        self.provider.current_identity.return_value = ClientIdentity('logged_out')
        self.session.refresh_local()
        self.assertFalse(self.session.state()['bound'])

    def test_logout_or_close_while_verifying_prevents_late_adoption(self):
        for action in (self.session.logout, self.session.close):
            self.transport.side_effect = lambda *args: (action(), page())[1]
            self.session.refresh_local(force=True)
            self.assertFalse(self.session.state()['verified'])

    def test_native_refresh_is_throttled_and_not_rebound_when_unchanged(self):
        self.session.refresh_local()
        generation = self.session.state()['binding_generation']
        self.session.refresh_local()
        self.assertEqual(self.provider.snapshot.call_count, 1)
        self.now += 61
        self.session.refresh_local()
        self.assertEqual(self.provider.snapshot.call_count, 2)
        self.assertEqual(self.session.state()['binding_generation'], generation)

    def test_new_cookie_invalidates_prepared_posts(self):
        self.session.refresh_local()
        generation = self.session.state()['binding_generation']
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, {**VALUES, 'sessionid': 'b' * 24})
        self.session.refresh_local(force=True)
        self.transport.reset_mock()
        with self.assertRaises(SteamSessionError):
            self.session.post('/market/sellitem/', {}, expected_generation=generation)
        self.transport.assert_not_called()

    def test_ambiguous_native_post_is_submitted_only_once_and_not_refreshed(self):
        self.session.refresh_local()
        self.transport.reset_mock()
        self.transport.side_effect = SteamSessionError('network result uncertain')
        with self.assertRaises(SteamSessionError):
            self.session.post('/market/sellitem/', {}, expected_steamid=SID)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.provider.snapshot.call_count, 1)

    def test_logout_suppresses_auto_rebinding_until_next_assistant_session(self):
        self.session.refresh_local()
        self.session.logout()
        self.session.refresh_local(force=True)
        self.assertEqual(self.provider.snapshot.call_count, 1)
        self.assertFalse(self.session.state()['bound'])

    def test_rejected_cookie_is_backed_off_but_new_native_cookie_can_verify(self):
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        self.session.refresh_local()
        self.now += 61
        self.session.refresh_local()
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.provider.snapshot.call_count, 2)
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, {**VALUES, 'sessionid': 'b' * 24})
        self.now += 61
        self.transport.side_effect = None
        self.session.refresh_local()
        self.assertEqual(self.transport.call_count, 2)
        self.assertTrue(self.session.state()['verified'])

    def test_failed_manual_login_does_not_disable_native_renewal(self):
        from tests.test_steam_session import cookies
        self.session.refresh_local()
        self.transport.side_effect = SteamSessionError('temporary failure')
        with self.assertRaises(SteamSessionError):
            self.session.capture_session(cookies())
        self.assertEqual(self.session.state()['source'], 'local_steam')
        self.transport.side_effect = None
        self.session.refresh_local(force=True)
        self.assertTrue(self.session.state()['verified'])
        self.assertEqual(self.provider.snapshot.call_count, 2)

    def test_slow_native_read_is_bounded_and_late_success_cannot_replace_saved_login(self):
        release, finished = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.path.write_bytes(json.dumps({'cookies': VALUES}).encode())
        session = SteamSession(self.path, transport=self.transport, local_provider=self.provider,
            protector=lambda raw, **kw: raw, clock=lambda: self.now, local_verify_timeout=.02)
        def slow(*args):
            release.wait(2)
            finished.set()
            return page()
        self.transport.side_effect = slow
        start = time.monotonic()
        session.refresh_local()
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(session.state()['source'], 'saved_login')
        self.assertFalse(session.state()['verified'])
        session.refresh_local(force=True)
        self.assertEqual(self.transport.call_count, 1)
        release.set()
        self.assertTrue(finished.wait(1))
        session._local_request[0].join(1)
        self.assertEqual(session.state()['source'], 'saved_login')
        self.assertFalse(session.state()['verified'])
        self.assertTrue(self.path.exists())
        session.close()


class NativeTradingIntegrationTests(unittest.TestCase):
    """Production session/trading modules; all HTTP and economic writes are fakes."""
    def setUp(self):
        from tests.test_sale_tracking import TrackingSession
        from tests.test_steam_trading import catalog
        from deskrawl_assistant.steam_trading import SteamTradingService
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = 1000
        self.server = TrackingSession()
        self.provider = Mock()
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, dict(VALUES))
        self.provider.current_identity.return_value = IDENTITY
        self.methods = []
        def transport(method, path, data, cookies):
            self.assertEqual(cookies, VALUES)
            self.methods.append((method, path))
            if path == '/market/':
                return page()
            if method == 'POST':
                self.assertEqual(data['sessionid'], VALUES['sessionid'])
                return self.server.post(path, data, expected_steamid=SID)
            return self.server.request(method, path, data)
        self.session = SteamSession(self.root / 'steam-session.dpapi', transport=transport,
            local_provider=self.provider, clock=lambda: self.now)
        self.service = SteamTradingService(self.root, catalog(), session=self.session, clock=lambda: self.now)
        self.service.refresh_auth()
        self.assertEqual(self.service.state()['source'], 'local_steam')
        self.settings = {'sell_enabled': True, 'categories': ['gold_bar'], 'price_rule': 'lowest_sell'}

    def test_price_inventory_and_sale_use_verified_native_account(self):
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=SID)
        result = self.service.sale_once(self.settings, SID)
        self.assertTrue(result['sale_submitted'])
        self.assertEqual(self.server.sales[0][1]['price'], 200)
        self.assertTrue(any(p.startswith('/inventory/') for m, p in self.methods))
        self.assertTrue(any(p.startswith('/market/listings/') for m, p in self.methods))
        self.assertFalse(self.session.path.exists())
        self.service.sale_once(self.settings, SID)
        self.assertEqual(len(self.server.sales), 1)

    def test_reprice_cancels_exact_listing_then_lists_once_with_native_session(self):
        from tests.test_steam_trading import inventory
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=SID)
        row = self.service._journal[0]
        row.update(status='listed', proof='a' * 32, listingid='345', game_instance_id='game-guid',
            buyer_total=230, seller_receive=200, sale_amount=1, currency_id=23,
            submitted_at=999, publisher_fee='.10')
        self.service._save()
        self.service.refresh_confirmation()
        preview = self.service.reprice_preview(row['id'])
        self.server.inventory = inventory()
        self.server.result = {'success': 1, 'requires_confirmation': True, 'listingid': '678'}
        self.service.reprice(preview['token'], '2.30')
        self.assertEqual([p for p, _ in self.server.sales], ['/market/removelisting/345', '/market/sellitem/'])
        self.assertEqual(self.server.sales[-1][1]['assetid'], '2')
        self.assertEqual(row['status'], 'pending')
        with self.assertRaises(SteamSessionError):
            self.service.reprice(preview['token'], '2.30')
        self.assertEqual(len(self.server.sales), 2)

    def test_native_switch_after_quote_prevents_sale(self):
        self.service.register_uploaded('1', 'GoldBar', expected_steamid=SID)
        self.server.on_quote = lambda: setattr(self.provider.current_identity, 'return_value', ClientIdentity('logged_out'))
        self.service.sale_once(self.settings, SID)
        self.assertEqual(self.server.sales, [])
        self.assertTrue(self.service.state()['expired'])


if __name__ == '__main__':
    unittest.main()
