"""Real loopback TLS, fake trust/hosts: never alter Windows or a running Steam."""
import http.client
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import socket
import ssl
import threading
import time
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from deskrawl_assistant.steam_native_acceleration import (
    NativeSteamAcceleration, NativeCommunityBridge, CommunityRelayTransport, temporary_certificate,
    WindowsLeafTrust, cleanup_owned_leaf,
)
from deskrawl_assistant.steam_native_hosts import (
    hosts_edit, hosts_worker, recover_orphaned_hosts, NativeAccelerationError, BEGIN, END,
)
from deskrawl_assistant.steam_login_transport import CommunityLoginTransport

NONCE = 'a' * 32


class HostsTests(unittest.TestCase):
    def test_only_fixed_community_mapping_is_added_and_removed(self):
        raw = b'# custom\r\n192.0.2.2 other.example\r\n'
        applied = hosts_edit(raw, NONCE, True)
        self.assertTrue(applied.startswith(raw))
        self.assertEqual(applied.count(b'127.0.0.1 steamcommunity.com'), 1)
        self.assertEqual(hosts_edit(applied, NONCE, False), raw)
        self.assertEqual(hosts_edit(applied, NONCE, True), applied)

    def test_third_party_changes_outside_owned_block_are_preserved(self):
        raw = b'# keep\n'
        applied = hosts_edit(raw, NONCE, True) + b'192.0.2.3 another.example\n'
        self.assertEqual(hosts_edit(applied, NONCE, False), raw + b'192.0.2.3 another.example\n')

    def test_existing_accelerator_and_modified_or_foreign_block_are_never_overwritten(self):
        cases = (b'1.2.3.4 steamcommunity.com\n', b'::1 STEAMCOMMUNITY.COM\n',
                 BEGIN + b'b' * 32 + b'\n127.0.0.1 steamcommunity.com\n' + END + b'\n',
                 BEGIN + NONCE.encode() + b'\n1.2.3.4 steamcommunity.com\n' + END + b'\n',
                 END + b'\n')
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError): hosts_edit(raw, NONCE, True)

    def test_no_caller_chosen_address_path_or_marker_can_be_injected(self):
        for nonce in ('', 'a' * 31, '\r\n127.0.0.1 example.com', '../../hosts'):
            with self.assertRaises(ValueError): hosts_edit(b'', nonce, True)

    def test_orphan_recovery_preserves_unrelated_entries_and_clean_shutdown_baseline(self):
        original = b'# custom\r\n192.0.2.2 other.example\r\n'
        abandoned = hosts_edit(original, 'b' * 32, True) + b'# later edit\r\n'
        probe = Mock(return_value=False)
        clean = recover_orphaned_hosts(abandoned, NONCE, probe)
        probe.assert_called_once_with('b' * 32)
        self.assertEqual(clean, original + b'# later edit\r\n')
        self.assertEqual(hosts_edit(hosts_edit(clean, NONCE, True), NONCE, False), clean)

    def test_live_or_ambiguous_lease_cannot_be_taken_over(self):
        raw = hosts_edit(b'# keep\n', 'b' * 32, True)
        with self.assertRaises(NativeAccelerationError) as raised:
            recover_orphaned_hosts(raw, NONCE, lambda _: True)
        self.assertEqual(raised.exception.code, 'hosts_conflict')
        for changed in (raw.replace(b'127.0.0.1', b'192.0.2.3'),
                        raw + raw, raw.replace(b'b' * 32, b'invalid')):
            probe = Mock(return_value=False)
            try:
                clean = recover_orphaned_hosts(changed, NONCE, probe)
                with self.assertRaises(ValueError): hosts_edit(clean, NONCE, True)
            except ValueError:
                pass
            probe.assert_not_called()

    def test_foreign_mapping_survives_orphan_recovery_and_blocks_activation(self):
        raw = hosts_edit(b'# keep\n', 'b' * 32, True) + b'192.0.2.3 steamcommunity.com\n'
        clean = recover_orphaned_hosts(raw, NONCE, lambda _: False)
        self.assertIn(b'192.0.2.3 steamcommunity.com', clean)
        with self.assertRaises(ValueError): hosts_edit(clean, NONCE, True)


@unittest.skipUnless(os.name == 'nt', 'Windows watcher orchestration with fake system APIs')
class WatcherTests(unittest.TestCase):
    def exercise(self, raw, *, changed=False, cancelled=False, wrong_owner=False, restore_busy=False, live_lease=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture-hosts'
            path.write_bytes(raw)
            kernel = Mock()
            kernel.OpenEventW.side_effect = lambda _a, _b, name: {'ready': 1, 'stop': 2, 'done': 3}[name.rsplit('.', 1)[1]]
            kernel.OpenProcess.return_value = 4
            kernel.CreateMutexW.return_value = 5
            active = []
            path_reads = []
            def hosts_path():
                path_reads.append(True)
                if restore_busy and len(path_reads) == 2: raise PermissionError('fixture lock')
                return path
            def event(handle):
                if handle == 1:
                    active.append(path.read_bytes())
                    if changed: path.write_bytes(path.read_bytes() + b'# external change\n')
            kernel.SetEvent.side_effect = event
            kernel.WaitForSingleObject.side_effect = lambda handle, delay: (
                0 if handle == 5 or handle == 4 and active or handle == 2 and cancelled else 258)
            with patch('deskrawl_assistant.steam_native_hosts._kernel', return_value=kernel), \
                 patch('deskrawl_assistant.steam_native_hosts._created', return_value=43 if wrong_owner else 42), \
                 patch('deskrawl_assistant.steam_native_hosts._owner_sid', return_value='S-1-5-21-1234'), \
                 patch('deskrawl_assistant.steam_native_hosts._lease_exists', return_value=live_lease), \
                 patch('deskrawl_assistant.steam_native_hosts._hosts_path', side_effect=hosts_path), \
                 patch('deskrawl_assistant.steam_native_hosts.time.sleep'), \
                 patch('deskrawl_assistant.steam_native_hosts._flush'), \
                 patch('deskrawl_assistant.steam_native_acceleration.cleanup_owned_leaf') as cleanup:
                result = hosts_worker(123, 42, NONCE, 'b' * 64)
            return result, path.read_bytes(), active, kernel, cleanup

    def test_owner_exit_restores_exact_original_and_cleans_original_user_certificate(self):
        original = b'# no trailing newline'
        result, restored, active, kernel, cleanup = self.exercise(original)
        self.assertEqual((result, restored), (0, original))
        self.assertIn(b'127.0.0.1 steamcommunity.com', active[0])
        cleanup.assert_called_once_with('b' * 64, 'S-1-5-21-1234')
        kernel.SetEvent.assert_any_call(3)

    def test_external_hosts_changes_survive_cleanup(self):
        result, restored, _, _, _ = self.exercise(b'# original\n', changed=True)
        self.assertEqual((result, restored), (0, b'# original\n# external change\n'))

    def test_abandoned_mapping_is_replaced_but_not_restored_on_exit(self):
        original = b'# original\n'
        abandoned = hosts_edit(original, 'b' * 32, True)
        result, restored, active, _, _ = self.exercise(abandoned)
        self.assertEqual((result, restored), (0, original))
        self.assertIn(BEGIN + NONCE.encode(), active[0])

    def test_still_owned_old_mapping_is_preserved_without_writing(self):
        raw = hosts_edit(b'# original\n', 'b' * 32, True)
        result, restored, active, kernel, _ = self.exercise(raw, live_lease=True)
        self.assertEqual((result, restored, active), (1, raw, []))
        kernel.SetEvent.assert_any_call(3)

    def test_temporary_restore_lock_retries_before_removing_certificate(self):
        result, restored, _, kernel, cleanup = self.exercise(b'# original\n', restore_busy=True)
        self.assertEqual((result, restored), (0, b'# original\n'))
        cleanup.assert_called_once()
        kernel.SetEvent.assert_any_call(3)

    def test_conflicting_mapping_cancelled_setup_and_reused_pid_never_write(self):
        for raw, options in [(b'192.0.2.1 steamcommunity.com\n', {}),
                             (b'# keep\n', {'cancelled': True}),
                             (b'# keep\n', {'wrong_owner': True})]:
            result, restored, active, _, _ = self.exercise(raw, **options)
            self.assertEqual((result, restored, active), (1, raw, []))

    def test_invalid_helper_arguments_never_open_windows_or_system_files(self):
        with patch('deskrawl_assistant.steam_native_hosts._kernel') as kernel:
            self.assertEqual(hosts_worker(0, 42, NONCE, 'b' * 64), 1)
            self.assertEqual(hosts_worker(123, 42, '../path', 'b' * 64), 1)
            self.assertEqual(hosts_worker(123, 42, NONCE, 'not-a-key'), 1)
            kernel.assert_not_called()


class TrustCleanupTests(unittest.TestCase):
    def test_watcher_removes_all_renewed_owned_leaves_in_original_user_store_only(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import hashlib
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        owned = [temporary_certificate(key=key)[1] for _ in range(2)]
        foreign = temporary_certificate()[1]
        fingerprint = hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
        class Context(ctypes.Structure):
            _fields_ = [('encoding', w.DWORD), ('raw', ctypes.c_void_p), ('size', w.DWORD),
                        ('info', ctypes.c_void_p), ('store', ctypes.c_void_p)]
        buffers = [ctypes.create_string_buffer(der) for der in [b'legacy-unparseable-fixture', foreign, *owned]]
        contexts = [Context(1, ctypes.cast(raw, ctypes.c_void_p), len(raw.raw) - 1, None, None) for raw in buffers]
        addresses = [ctypes.addressof(item) for item in contexts]
        remaining = addresses.copy()
        api = Mock()
        api.CertOpenStore.return_value = 7
        def enum(_store, previous):
            index = remaining.index(previous) + 1 if previous else 0
            return remaining[index] if index < len(remaining) else None
        api.CertEnumCertificatesInStore.side_effect = enum
        def remove(address): remaining.remove(address); return True
        api.CertDeleteCertificateFromStore.side_effect = remove
        with patch.object(WindowsLeafTrust, '_api', return_value=api):
            cleanup_owned_leaf(fingerprint, 'S-1-5-21-1234')
        self.assertEqual(remaining, addresses[:2])
        self.assertEqual(api.CertDeleteCertificateFromStore.call_count, 2)
        self.assertEqual(api.CertOpenStore.call_args.args[3:], (0x64000, 'S-1-5-21-1234\\Root'))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.trust, self.lease, self.proxy = Mock(), Mock(), Mock()
        self.transport = Mock(spec=CommunityRelayTransport)
        self.transport.method_allowed.side_effect = CommunityRelayTransport().method_allowed
        self.transport.request.return_value = (200, [('Content-Length', '2')], b'ok')
        self.context, self.der = temporary_certificate()
        self.bridge = NativeCommunityBridge(transport=self.transport, lease=self.lease, trust=self.trust, proxy_lease=self.proxy,
            certificate_factory=lambda: (self.context, self.der), port=0)
        self.bridge.start(threading.Event())
        self.addCleanup(self.bridge.close)

    def connection(self, host='steamcommunity.com', trusted=True):
        context = ssl.create_default_context()
        if trusted: context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(self.bridge.der))
        connection = http.client.HTTPSConnection('127.0.0.1', self.bridge.server.server_port, timeout=2)
        raw = socket.create_connection(('127.0.0.1', self.bridge.server.server_port), timeout=2)
        try: connection.sock = context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close(); raise
        self.addCleanup(connection.close)
        return connection

    def test_certificate_is_short_lived_exact_host_leaf_not_a_ca(self):
        cert = x509.load_der_x509_certificate(self.der)
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), ['steamcommunity.com'])
        self.assertEqual(list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value), [ExtendedKeyUsageOID.SERVER_AUTH])
        self.assertLess((cert.not_valid_after_utc - cert.not_valid_before_utc).total_seconds(), 90000)

    def test_real_tls_forwards_only_original_community_request_once(self):
        connection = self.connection()
        connection.request('POST', '/market/sellitem/', body=b'quantity=1', headers={'Host': 'steamcommunity.com', 'Cookie': 'test-only-fixture=abc'})
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b'ok'))
        args = self.transport.request.call_args.args
        self.assertEqual(args[:2], ('https://steamcommunity.com/market/sellitem/', 'POST'))
        self.assertEqual(args[3], b'quantity=1')
        self.assertIn(('Cookie', 'test-only-fixture=abc'), args[2])
        self.assertEqual(self.transport.request.call_count, 1)
        self.trust.add.assert_called_once_with(self.der)

    def test_wrong_sni_untrusted_leaf_and_foreign_http_host_fail_closed(self):
        for hostname, trusted in [('other.example', True), ('steamcommunity.com', False)]:
            with self.assertRaises((ssl.SSLError, OSError)):
                self.connection(hostname, trusted)
        connection = self.connection()
        connection.request('GET', '/market/', headers={'Host': 'other.example'})
        self.assertEqual(connection.getresponse().status, 403)
        self.transport.request.assert_not_called()

    def test_ambiguous_post_response_is_not_replayed(self):
        self.transport.request.side_effect = ConnectionResetError()
        connection = self.connection()
        connection.request('POST', '/market/removelisting/123', body=b'sessionid=fixture', headers={'Host': 'steamcommunity.com'})
        self.assertEqual(connection.getresponse().status, 502)
        self.assertEqual(self.transport.request.call_count, 1)

    def test_proxy_forms_chunked_requests_and_ambiguous_lengths_are_rejected(self):
        for path, extra in [('https://other.example/market/', {}), ('//other.example/', {}),
                            ('/market/', {'Transfer-Encoding': 'chunked'})]:
            connection = self.connection()
            connection.request('GET', path, headers={'Host': 'steamcommunity.com', **extra})
            self.assertEqual(connection.getresponse().status, 403)
        self.transport.request.assert_not_called()

    def test_cleanup_restores_hosts_before_removing_trust(self):
        calls = Mock()
        calls.attach_mock(self.proxy, 'proxy')
        calls.attach_mock(self.lease, 'lease'); calls.attach_mock(self.trust, 'trust')
        self.bridge.close()
        self.assertLess(calls.mock_calls.index(unittest.mock.call.proxy.close()), calls.mock_calls.index(unittest.mock.call.lease.close()))
        self.assertLess(calls.mock_calls.index(unittest.mock.call.lease.close()), calls.mock_calls.index(unittest.mock.call.trust.close()))

    def test_proxy_bypass_starts_after_hosts_and_uses_the_same_lease(self):
        self.proxy.start.assert_called_once()
        self.assertEqual(self.proxy.start.call_args.kwargs, {'nonce': self.lease.nonce})

    def test_proxy_cleanup_failure_keeps_hosts_and_tls_available(self):
        self.proxy.close.side_effect = OSError('fixture')
        with self.assertRaises(OSError): self.bridge.close()
        self.lease.close.assert_not_called()
        self.trust.close.assert_not_called()
        self.proxy.close.side_effect = None

    def test_login_interceptor_does_not_gain_native_browser_write_permissions(self):
        self.assertFalse(CommunityLoginTransport().method_allowed('POST', '/market/sellitem/'))
        self.assertTrue(CommunityRelayTransport().method_allowed('POST', '/market/sellitem/'))

    def test_certificate_renewal_retains_owned_key_without_hosts_or_steam_changes(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.bridge.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.bridge.certificate_factory = temporary_certificate
        self.bridge.renew()
        first = self.bridge.der
        first_key = x509.load_der_x509_certificate(first).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.bridge.renew()
        second_key = x509.load_der_x509_certificate(self.bridge.der).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(first_key, second_key)
        self.assertNotEqual(first, self.bridge.der)
        self.assertIs(self.bridge.retired_der, first)
        self.lease.start.assert_called_once()
        connection = self.connection()
        connection.request('GET', '/market/', headers={'Host': 'steamcommunity.com'})
        self.assertEqual(connection.getresponse().status, 200)


class ManagerTests(unittest.TestCase):
    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline: time.sleep(.005)
        self.assertTrue(predicate())

    def test_default_off_enable_disable_and_shutdown_are_serialized(self):
        factory = Mock()
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        factory.assert_not_called()
        manager.configure(True)
        self.wait(lambda: manager.snapshot()['status'] == 'active')
        manager.configure(True)
        self.assertEqual(factory.call_count, 1)
        manager.configure(False)
        self.wait(lambda: manager.snapshot()['status'] == 'disabled')
        factory.return_value.close.assert_called_once()
        manager.configure(True)
        self.wait(lambda: factory.call_count == 2)
        manager.close()
        self.assertFalse(manager.thread.is_alive())
        self.assertGreaterEqual(factory.return_value.close.call_count, 2)

    def test_failure_does_not_report_active_or_retry_uac_in_a_loop(self):
        factory = Mock()
        factory.return_value.start.side_effect = OSError('fixture')
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        manager.configure(True)
        self.wait(lambda: manager.snapshot()['status'] == 'error')
        self.assertEqual(factory.call_count, 1)

    def test_fixed_diagnostic_identifies_failure_without_exposing_exception_details(self):
        factory = Mock()
        factory.return_value.start.side_effect = NativeAccelerationError(
            'listener_unavailable', '本机 Steam 加速监听端口 443 无法启用。')
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        manager.configure(True)
        self.wait(lambda: manager.snapshot()['status'] == 'error')
        self.assertEqual(manager.snapshot()['error_code'], 'listener_unavailable')
        self.assertIn('443', manager.snapshot()['message'])
        self.assertEqual(factory.call_count, 1)

    def test_disabling_during_setup_cancels_and_cleans_before_disabled(self):
        started = threading.Event()
        factory = Mock()
        def start(cancel):
            started.set(); cancel.wait(2); raise OSError('cancelled')
        factory.return_value.start.side_effect = start
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        manager.configure(True); self.assertTrue(started.wait(1)); manager.configure(False)
        self.wait(lambda: manager.snapshot()['status'] == 'disabled')
        factory.return_value.close.assert_called_once()

    def test_cleanup_failure_retains_bridge_for_retry_and_never_starts_another(self):
        factory = Mock()
        factory.return_value.close.side_effect = [OSError('fixture'), None, None]
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        manager.configure(True)
        self.wait(lambda: manager.snapshot()['status'] == 'active')
        manager.configure(False)
        self.wait(lambda: manager.snapshot()['status'] == 'error')
        self.assertEqual(factory.call_count, 1)
        manager.configure(True)
        self.wait(lambda: factory.call_count == 2)
        self.wait(lambda: manager.snapshot()['status'] == 'active')

    def test_long_running_bridge_renews_without_another_factory_or_system_authorization(self):
        factory = Mock()
        bridge = factory.return_value
        bridge.renew_after = time.monotonic() + .04
        def renew(): bridge.renew_after = time.monotonic() + 3600
        bridge.renew.side_effect = renew
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        manager.configure(True)
        self.wait(lambda: bridge.renew.call_count == 1)
        self.assertEqual(factory.call_count, 1)
        bridge.close.assert_not_called()
        self.assertEqual(manager.snapshot()['status'], 'active')

    def test_failed_renewal_cleans_hosts_and_trust_instead_of_leaving_expired_tls(self):
        factory = Mock()
        factory.return_value.renew_after = time.monotonic() + .04
        factory.return_value.renew.side_effect = OSError('fixture')
        manager = NativeSteamAcceleration(bridge_factory=factory, supported=True)
        self.addCleanup(manager.close)
        manager.configure(True)
        self.wait(lambda: manager.snapshot()['status'] == 'expired')
        factory.return_value.close.assert_called_once()
        self.assertEqual(factory.call_count, 1)


if __name__ == '__main__': unittest.main()
