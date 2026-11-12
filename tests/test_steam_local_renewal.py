"""Renewal/recovery acceptance tests, with no live auth or economic requests."""
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.steam_client_profile import ClientIdentity
from deskrawl_assistant.steam_local_renewal import (
    web_cookies, cookie_expiry, NativeRenewalUnavailable, renew_native_web_session, _renew_child,
)
from deskrawl_assistant.steam_local_session import LocalSteamSessionProvider, LocalSessionSnapshot, LocalSessionUnavailable
from deskrawl_assistant.steam_session import SteamSession, SteamSessionError
from tests.test_steam_local_session import SID, VALUES, IDENTITY, page


def token(sid=SID, exp=10000, aud=None):
    payload = base64.urlsafe_b64encode(json.dumps({'sub': sid, 'exp': exp,
        'aud': ['client', 'web'] if aud is None else aud}).encode()).rstrip(b'=').decode()
    return sid + '||e30.' + payload + '.fake-test-signature'


class NativeCookieTests(unittest.TestCase):
    def test_native_value_already_includes_account_and_stays_memory_only(self):
        values = web_cookies(token(), SID, now=1000)
        self.assertEqual(values['steamLoginSecure'].count(SID), 1)
        self.assertEqual(cookie_expiry(values), 10000)
        self.assertEqual(len(values['sessionid']), 24)

    def test_other_account_refresh_secret_and_expired_tokens_are_rejected(self):
        for value in (token('76561198000000002'), token(aud=['web', 'derive']),
                      token(exp=1001), token(aud=['client']), 'not-a-cookie'):
            with self.assertRaises(NativeRenewalUnavailable): web_cookies(value, SID, now=1000)

    def test_malformed_expiry_is_not_an_authentication_signal(self):
        for value in (VALUES, {}, {'steamLoginSecure': 'invalid'},
                      {'steamLoginSecure': SID + '||e30.W10.fake'},
                      {'steamLoginSecure': token(exp=float('inf'))}):
            self.assertIsNone(cookie_expiry(value))

    def test_provider_rechecks_account_before_and_after_renewal(self):
        backend, renewer = Mock(), Mock(return_value=VALUES)
        backend.identity.return_value = IDENTITY
        provider = LocalSteamSessionProvider(backend, renewer=renewer)
        self.assertEqual(provider.renew(IDENTITY).cookies, VALUES)
        backend.identity.side_effect = [IDENTITY, ClientIdentity('logged_out')]
        with self.assertRaises(LocalSessionUnavailable): provider.renew(IDENTITY)
        self.assertEqual(renewer.call_count, 2)
        backend.identity.side_effect = None
        backend.identity.return_value = ClientIdentity('logged_out')
        with self.assertRaises(LocalSessionUnavailable): provider.renew(IDENTITY)
        self.assertEqual(renewer.call_count, 2)


class IsolatedHelperTests(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.receive, self.send, self.process = Mock(), Mock(), Mock()
        self.context.Pipe.return_value = self.receive, self.send
        self.context.Process.return_value = self.process
        self.process.pid = 42
        self.process.is_alive.return_value = False
        self.receive.poll.return_value = True
        self.receive.recv.return_value = {'cookies': VALUES}
        patcher = patch('deskrawl_assistant.steam_local_renewal.multiprocessing.get_context',
                        return_value=self.context)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_uses_only_own_spawned_helper_and_closes_ipc(self):
        self.assertEqual(renew_native_web_session(IDENTITY), VALUES)
        self.assertEqual(self.context.Process.call_args.kwargs['args'], (self.send, IDENTITY))
        self.process.start.assert_called_once()
        self.process.close.assert_called_once()
        self.process.terminate.assert_not_called()
        self.receive.close.assert_called_once()

    def test_timeout_terminates_only_the_owned_helper(self):
        self.receive.poll.return_value = False
        self.process.is_alive.side_effect = [True, False]
        with self.assertRaises(NativeRenewalUnavailable) as caught:
            renew_native_web_session(IDENTITY, timeout=.01)
        self.assertIn('超时', str(caught.exception))
        self.process.terminate.assert_called_once()
        self.process.close.assert_called_once()
        self.receive.poll.assert_called_once_with(.01)
        self.receive.close.assert_called_once()

    def test_crash_and_account_mismatch_fail_closed_without_raw_error_details(self):
        for reply in (EOFError('SECRET-TOKEN private-path'),
                      {'cookies': {**VALUES, 'steamLoginSecure': '76561198000000002%7C%7Cother'}},
                      'invalid'):
            with self.subTest(reply_type=type(reply).__name__):
                self.receive.recv.side_effect = reply if isinstance(reply, Exception) else None
                self.receive.recv.return_value = reply
                with self.assertRaises(NativeRenewalUnavailable) as caught:
                    renew_native_web_session(IDENTITY)
                self.assertNotIn('SECRET', str(caught.exception))
                self.assertNotIn('private-path', str(caught.exception))
        self.process.terminate.assert_not_called()

    def test_unknown_abi_is_not_retried_as_a_network_failure(self):
        self.receive.recv.return_value = {'message': '当前 Steam 版本尚未适配。', 'retryable': False}
        with self.assertRaises(NativeRenewalUnavailable) as caught:
            renew_native_web_session(IDENTITY)
        self.assertFalse(caught.exception.retryable)

    def test_child_sanitizes_unknown_native_failure(self):
        connection = Mock()
        with patch('deskrawl_assistant.steam_local_renewal._native_refresh',
                   side_effect=RuntimeError('SECRET-TOKEN private-path')):
            _renew_child(connection, IDENTITY)
        sent = connection.send.call_args.args[0]
        self.assertEqual(set(sent), {'message'})
        self.assertNotIn('SECRET', sent['message'])
        self.assertNotIn('private-path', sent['message'])
        connection.close.assert_called_once()


class AutomaticRenewalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 1000
        self.provider = Mock()
        self.provider.current_identity.return_value = IDENTITY
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, dict(VALUES))
        self.fresh = {**VALUES, 'steamLoginSecure': SID + '%7C%7Cfresh-test-token'}
        self.provider.renew.return_value = LocalSessionSnapshot(IDENTITY, self.fresh)
        self.transport = Mock(return_value=page())
        self.session = SteamSession(Path(folder.name) / 'session.dpapi', transport=self.transport,
            local_provider=self.provider, clock=lambda: self.now)
        self.addCleanup(self.session.close)

    def test_rejected_native_cookie_is_renewed_and_live_verified_without_login(self):
        def transport(method, path, data, cookies):
            if cookies == VALUES: raise SteamSessionError('expired', status=401, auth_expired=True)
            return page()
        self.transport.side_effect = transport
        self.session.refresh_local()
        self.assertTrue(self.session.state()['verified'])
        self.assertFalse(self.session.path.exists())
        self.provider.renew.assert_called_once_with(IDENTITY)
        self.assertEqual([c.args[0] for c in self.transport.call_args_list], ['GET', 'GET'])

    def test_rejected_post_never_replays_and_expiry_does_not_cancel_native_auto_sale(self):
        self.session.refresh_local()
        old_generation = self.session.state()['binding_generation']
        self.transport.reset_mock()
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        with self.assertRaises(SteamSessionError):
            self.session.post('/market/sellitem/', {'price': 100}, expected_generation=old_generation)
        self.assertEqual(self.transport.call_count, 1)
        self.assertTrue(self.session.state()['renewing'])
        self.assertFalse(self.session.state()['expired'])
        self.assertFalse(self.session.state()['verified'])
        self.assertTrue(self.session.state()['bound'])
        with self.assertRaises(SteamSessionError): self.session.post('/market/sellitem/', {'price': 100})
        self.assertEqual(self.transport.call_count, 1)
        def transport(method, path, data, cookies):
            if cookies == VALUES: raise SteamSessionError('expired', status=401, auth_expired=True)
            return page()
        self.transport.side_effect = transport
        self.session.refresh_local()
        self.assertTrue(self.session.state()['verified'])
        with self.assertRaises(SteamSessionError):
            self.session.post('/market/sellitem/', {}, expected_generation=old_generation)
        self.assertEqual([c.args[0] for c in self.transport.call_args_list], ['POST', 'GET', 'GET'])

    def test_renewal_keeps_confirmed_upload_fence_and_does_not_cancel_auto_sale_setting(self):
        from deskrawl_assistant.auction_service import AuctionCoordinator
        self.session.refresh_local()
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        with self.assertRaises(SteamSessionError): self.session.verify(force=True)
        revoked = []
        trading = SimpleNamespace(state=self.session.state, close=lambda: None)
        coordinator = AuctionCoordinator(self.session.path.parent, trading,
            lambda: (False, {}, SID), start_worker=False, on_auth_expired=revoked.append)
        self.addCleanup(coordinator.close)
        row = {'item_uid': 'local', 'instance_id': 'native', 'internal_name': 'GoldBar'}
        identity = coordinator.submitted(row, SID, ticket='a' * 32)
        coordinator.confirmed(identity, {'uploaded_uid': 'local', 'uploaded_instance_id': 'native',
                                        'uploaded_internal_name': 'GoldBar'})
        coordinator.process_once()
        self.assertEqual(coordinator.intents[identity]['phase'], 'confirmed')
        self.assertIn('GoldBar', coordinator.observe({'steamid': SID})['blocked_keys'])
        self.assertEqual(revoked, [])
        self.assertTrue(coordinator.state()['steam']['renewing'])

    def test_failed_renewal_is_throttled_but_recovers_on_later_worker_pass(self):
        self.session.refresh_local()
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        with self.assertRaises(SteamSessionError): self.session.verify(force=True)
        self.provider.renew.side_effect = LocalSessionUnavailable('temporary offline')
        self.session.refresh_local()
        self.now += 61
        self.session.refresh_local(force=True)
        self.assertEqual(self.provider.renew.call_count, 1)
        self.assertTrue(self.session.state()['renewing'])
        self.now += 301
        self.provider.renew.side_effect = None
        self.transport.side_effect = lambda m, p, d, c: page() if c == self.fresh else (_ for _ in ()).throw(
            SteamSessionError('expired', status=401, auth_expired=True))
        self.session.refresh_local()
        self.assertEqual(self.provider.renew.call_count, 2)
        self.assertTrue(self.session.state()['verified'])

    def test_proactive_renewal_before_expiry_and_cold_cache_fallback(self):
        near = web_cookies(token(exp=1300), SID, now=1000)
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, near)
        self.session.refresh_local()
        self.provider.renew.assert_called_once()
        self.assertEqual(self.transport.call_args.args[3], self.fresh)
        self.now += 301
        self.provider.snapshot.side_effect = LocalSessionUnavailable('cache locked')
        self.session.refresh_local()
        self.assertEqual(self.provider.renew.call_count, 2)
        self.assertTrue(self.session.state()['verified'])

    def test_proactive_failure_keeps_still_valid_session_usable(self):
        near = web_cookies(token(exp=1300), SID, now=1000)
        self.provider.snapshot.return_value = LocalSessionSnapshot(IDENTITY, near)
        self.provider.renew.side_effect = LocalSessionUnavailable('temporarily offline')
        self.session.refresh_local()
        self.assertTrue(self.session.state()['verified'])
        self.assertFalse(self.session.state()['renewing'])
        self.assertEqual(self.transport.call_args.args[3], near)

    def test_unsupported_native_build_has_explicit_manual_login_fallback(self):
        self.session.refresh_local()
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        self.provider.renew.side_effect = LocalSessionUnavailable('当前 Steam 版本尚未适配。', retryable=False)
        self.now += 61
        self.session.refresh_local()
        self.assertTrue(self.session.state()['expired'])
        self.assertFalse(self.session.state()['renewing'])
        self.assertEqual(self.session.state()['local_status'], 'unsupported')
        self.assertIn('尚未适配', self.session.state()['message'])

    def test_native_logout_while_waiting_for_renewal_is_not_shown_as_still_renewing(self):
        self.session.refresh_local()
        self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
        with self.assertRaises(SteamSessionError): self.session.verify(force=True)
        self.provider.snapshot.side_effect = LocalSessionUnavailable('offline')
        self.provider.current_identity.return_value = ClientIdentity('logged_out')
        self.session.refresh_local()
        self.assertTrue(self.session.state()['expired'])
        self.assertFalse(self.session.state()['renewing'])
        self.provider.renew.assert_not_called()

    def test_user_logout_close_or_account_switch_during_renewal_cannot_adopt_result(self):
        for action in ('logout', 'close', 'switch'):
            with self.subTest(action=action):
                self.setUp()
                self.transport.side_effect = SteamSessionError('expired', status=401, auth_expired=True)
                def renew(identity):
                    if action == 'switch': self.provider.current_identity.return_value = ClientIdentity('logged_out')
                    else: getattr(self.session, action)()
                    return LocalSessionSnapshot(IDENTITY, self.fresh)
                self.provider.renew.side_effect = renew
                self.session.refresh_local()
                self.assertFalse(self.session.state()['verified'])
                self.assertFalse(self.session.path.exists())


if __name__ == '__main__': unittest.main()
