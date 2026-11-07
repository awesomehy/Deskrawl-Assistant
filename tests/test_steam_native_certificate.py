"""Reusable TLS identity tests; no real trust, hosts or proxy modifications."""
import base64
import ctypes
from ctypes import wintypes as w
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from deskrawl_assistant.steam_native_certificate import (
    PersistentCertificateStore, PREFIX, validate_leaf, _new_leaf,
)
from deskrawl_assistant.steam_native_acceleration import (
    WindowsLeafTrust, NativeCommunityBridge, CommunityRelayTransport, cleanup_owned_leaf,
)


class MemoryTrust:
    def __init__(self):
        self.trusted = set()
        self.installs = []
        self.retired = []

    def ensure(self, der):
        validate_leaf(der)
        if der not in self.trusted:
            self.installs.append(der)
            self.trusted.add(der)

    def retire(self, der):
        self.retired.append(der)
        self.trusted.discard(der)

    def close(self): pass


class CertificateFixture:
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'certificate.dpapi'
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.cipher = Fernet(Fernet.generate_key())
        self.trust = MemoryTrust()
        self.store = self.make_store()

    def protect(self, raw, *, decrypt=False):
        return self.cipher.decrypt(raw) if decrypt else self.cipher.encrypt(raw)

    def make_store(self, **kwargs):
        return PersistentCertificateStore(self.path, protector=kwargs.pop('protector', self.protect),
            clock=lambda: self.now, **kwargs)

    def read_bundle(self):
        return json.loads(self.protect(self.path.read_bytes(), decrypt=True))

    def replace_bundle(self, data):
        self.path.write_bytes(self.protect(json.dumps(data).encode()))


class PersistentCertificateTests(CertificateFixture, unittest.TestCase):
    def test_restart_reuses_exact_leaf_and_only_installs_once(self):
        first = self.store.prepare(self.trust)
        encrypted = self.path.read_bytes()
        self.trust.close()
        second = self.make_store().prepare(self.trust)
        self.assertEqual(first.der, second.der)
        self.assertEqual(self.trust.installs, [first.der])
        self.assertEqual(self.path.read_bytes(), encrypted)
        self.assertEqual(second.check_after, 24 * 3600)

    def test_disk_contains_only_protected_bundle_and_no_scratch_files(self):
        material = self.store.prepare(self.trust)
        encrypted = self.path.read_bytes()
        for marker in (b'PRIVATE KEY', b'certificate', b'key', material.der):
            self.assertNotIn(marker, encrypted)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(validate_leaf(material.der).not_valid_after_utc, self.now + timedelta(days=365))

    def test_no_certificate_or_file_created_by_construction(self):
        self.assertFalse(self.path.exists())
        self.assertFalse(self.trust.installs)

    def test_regular_daily_check_does_not_rotate_or_request_new_trust(self):
        first = self.store.prepare(self.trust)
        for delta in (1, 30, 300):
            self.now += timedelta(days=delta)
            self.assertEqual(self.store.prepare(self.trust).der, first.der)
        self.assertEqual(self.trust.installs, [first.der])

    def test_near_expiry_rotates_once_and_retires_only_saved_old_leaf(self):
        first = self.store.prepare(self.trust)
        foreign = b'foreign-fixture-not-a-certificate'
        self.trust.trusted.add(foreign)
        self.now += timedelta(days=358)
        second = self.make_store().prepare(self.trust)
        self.assertNotEqual(first.der, second.der)
        self.assertEqual(self.trust.retired, [first.der])
        self.assertEqual(self.trust.trusted, {second.der, foreign})
        self.assertEqual(self.read_bundle()['retired'], [])
        self.assertEqual(self.make_store().prepare(self.trust).der, second.der)
        self.assertEqual(len(self.trust.installs), 2)

    def test_renewal_publishes_new_context_before_removing_old_trust(self):
        first = self.store.prepare(self.trust)
        self.now += timedelta(days=360)
        activated = []
        original_retire = self.trust.retire
        def retire(der):
            self.assertEqual(der, first.der)
            self.assertEqual(len(activated), 1)
            self.assertIn(activated[0], self.trust.trusted)
            original_retire(der)
        with patch.object(self.trust, 'retire', side_effect=retire):
            material = self.store.prepare(self.trust, activate=lambda context, der: activated.append(der))
        self.assertEqual(activated, [material.der])

    def test_expired_and_future_dated_leaves_are_never_reused(self):
        first = self.store.prepare(self.trust)
        self.now -= timedelta(days=1)
        second = self.store.prepare(self.trust)
        self.assertNotEqual(first.der, second.der)
        self.now += timedelta(days=367)
        third = self.store.prepare(self.trust)
        self.assertNotEqual(second.der, third.der)

    def test_missing_system_trust_reinstalls_same_leaf_without_new_key(self):
        first = self.store.prepare(self.trust)
        self.trust.trusted.clear()
        second = self.make_store().prepare(self.trust)
        self.assertEqual(first.der, second.der)
        self.assertEqual(self.trust.installs, [first.der, first.der])

    def test_denied_trust_preserves_candidate_and_retry_does_not_rotate(self):
        denied = Mock()
        denied.ensure.side_effect = OSError('rejected fixture')
        with self.assertRaisesRegex(OSError, '证书'):
            self.store.prepare(denied)
        der = base64.b64decode(self.read_bundle()['certificate'])
        self.assertEqual(self.make_store().prepare(self.trust).der, der)
        denied.retire.assert_not_called()

    def test_rotation_rejection_keeps_old_trust_and_cleanup_survives_restart(self):
        first = self.store.prepare(self.trust)
        self.now += timedelta(days=360)
        with patch.object(self.trust, 'ensure', side_effect=OSError('denied')):
            with self.assertRaises(OSError): self.store.prepare(self.trust)
        self.assertIn(first.der, self.trust.trusted)
        candidate = base64.b64decode(self.read_bundle()['certificate'])
        self.assertEqual(self.make_store().prepare(self.trust).der, candidate)
        self.assertEqual(self.trust.retired, [first.der])

    def test_corrupt_or_different_user_file_is_replaced_without_plaintext_fallback(self):
        self.path.write_bytes(b'corrupt fixture')
        first = self.store.prepare(self.trust)
        other_cipher = Fernet(Fernet.generate_key())
        def other_user(raw, *, decrypt=False):
            return other_cipher.decrypt(raw) if decrypt else other_cipher.encrypt(raw)
        second = self.make_store(protector=other_user).prepare(self.trust)
        self.assertNotEqual(first.der, second.der)
        self.assertNotIn(b'PRIVATE KEY', self.path.read_bytes())

    def test_key_mismatch_wrong_host_ca_and_invalid_signature_are_rejected(self):
        first = self.store.prepare(self.trust)
        original = self.read_bundle()
        wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bundle = dict(original, key=base64.b64encode(wrong_key.private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())).decode())
        self.replace_bundle(bundle)
        self.assertNotEqual(self.store.prepare(self.trust).der, first.der)
        for ca, host in ((True, 'steamcommunity.com'), (False, 'other.example')):
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, PREFIX + 'a' * 32)])
            builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(wrong_key.public_key()).serial_number(1)
                .not_valid_before(self.now - timedelta(minutes=1))
                .not_valid_after(self.now + timedelta(days=10)))
            for extension in validate_leaf(first.der).extensions:
                value = extension.value
                if isinstance(value, x509.BasicConstraints): value = x509.BasicConstraints(ca=ca, path_length=None)
                if isinstance(value, x509.SubjectAlternativeName): value = x509.SubjectAlternativeName([x509.DNSName(host)])
                builder = builder.add_extension(value, critical=extension.critical)
            forged = builder.sign(wrong_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)
            with self.assertRaises(ValueError): validate_leaf(forged)
        damaged = first.der[:-1] + bytes([first.der[-1] ^ 1])
        with self.assertRaises(Exception): validate_leaf(damaged)

    def test_failed_protection_or_atomic_replace_never_changes_old_bundle_or_trust(self):
        self.store.prepare(self.trust)
        original = self.path.read_bytes()
        self.now += timedelta(days=360)
        with patch.object(self.store, 'protector', side_effect=OSError('fixture secret')):
            with self.assertRaises(OSError) as error: self.store.prepare(self.trust)
        self.assertNotIn('fixture secret', str(error.exception))
        self.assertEqual(self.path.read_bytes(), original)
        with patch.object(Path, 'replace', side_effect=PermissionError('fixture lock')):
            with self.assertRaises(OSError): self.store.prepare(self.trust)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(len(self.trust.installs), 1)

    @unittest.skipUnless(os.name == 'nt', 'Current-user Windows DPAPI')
    def test_real_windows_dpapi_round_trip_across_instances_without_trust_writes(self):
        first = PersistentCertificateStore(self.path).prepare(self.trust)
        second = PersistentCertificateStore(self.path).prepare(self.trust)
        self.assertEqual(first.der, second.der)
        self.assertEqual(len(self.trust.installs), 1)
        self.assertNotIn(b'PRIVATE KEY', self.path.read_bytes())


class WindowsTrustTests(unittest.TestCase):
    def setUp(self):
        self.der, _ = _new_leaf(datetime.now(timezone.utc))
        self.trust = WindowsLeafTrust()

    def test_exact_existing_trust_is_not_added_or_removed_on_exit(self):
        with patch.object(self.trust, 'contains', return_value=True), \
             patch.object(self.trust, 'add') as add, patch.object(self.trust, 'remove') as remove:
            self.trust.ensure(self.der)
            self.trust.close()
        add.assert_not_called(); remove.assert_not_called()

    def test_first_authorized_install_is_kept_and_retired_leaf_is_removed_exactly(self):
        def install(der): self.trust.certificates.add(der)
        with patch.object(self.trust, 'contains', return_value=False), \
             patch.object(self.trust, 'add', side_effect=install) as add, \
             patch.object(self.trust, 'remove') as remove:
            self.trust.ensure(self.der)
            self.trust.close()
            remove.assert_not_called()
            self.trust.retire(self.der)
            remove.assert_called_once_with(self.der)
        add.assert_called_once_with(self.der)

    def test_exact_der_lookup_frees_context_and_never_matches_only_a_common_name(self):
        class Context(ctypes.Structure):
            _fields_ = [('encoding', w.DWORD), ('raw', ctypes.c_void_p), ('size', w.DWORD),
                        ('info', ctypes.c_void_p), ('store', ctypes.c_void_p)]
        raw = ctypes.create_string_buffer(self.der)
        context = Context(1, ctypes.cast(raw, ctypes.c_void_p), len(self.der), None, None)
        address = ctypes.addressof(context)
        api = Mock()
        api.CertOpenSystemStoreW.return_value = 7
        api.CertEnumCertificatesInStore.side_effect = [address]
        with patch.object(self.trust, '_api', return_value=api):
            self.assertTrue(self.trust.contains(self.der))
        api.CertFreeCertificateContext.assert_called_once_with(address)
        api.CertAddEncodedCertificateToStore.assert_not_called()

    def test_crash_watcher_keeps_reusable_leaf_even_when_key_fingerprint_matches(self):
        class Context(ctypes.Structure):
            _fields_ = [('encoding', w.DWORD), ('raw', ctypes.c_void_p), ('size', w.DWORD),
                        ('info', ctypes.c_void_p), ('store', ctypes.c_void_p)]
        raw = ctypes.create_string_buffer(self.der)
        context = Context(1, ctypes.cast(raw, ctypes.c_void_p), len(self.der), None, None)
        fingerprint = hashlib.sha256(validate_leaf(self.der).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
        api = Mock()
        api.CertOpenStore.return_value = 7
        api.CertEnumCertificatesInStore.side_effect = [ctypes.addressof(context), None]
        with patch.object(WindowsLeafTrust, '_api', return_value=api):
            cleanup_owned_leaf(fingerprint, 'S-1-5-21-1234')
        api.CertDeleteCertificateFromStore.assert_not_called()


class PersistentBridgeTests(CertificateFixture, unittest.TestCase):
    def bridge(self):
        transport = Mock(spec=CommunityRelayTransport)
        transport.method_allowed.side_effect = CommunityRelayTransport().method_allowed
        transport.request.return_value = (200, [('Content-Length', '2')], b'ok')
        return NativeCommunityBridge(transport=transport, lease=Mock(), proxy_lease=Mock(),
            trust=self.trust, certificate_store=self.make_store(), port=0)

    def test_bridge_restart_and_daily_refresh_keep_trust_and_real_tls_works(self):
        first = self.bridge()
        first.start(threading.Event())
        der = first.der
        first.close()
        second = self.bridge()
        self.addCleanup(second.close)
        second.start(threading.Event())
        second.renew()
        self.assertEqual(second.der, der)
        self.assertEqual(self.trust.installs, [der])
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))
        connection = http.client.HTTPSConnection('127.0.0.1', second.server.server_port, timeout=2)
        raw = socket.create_connection(('127.0.0.1', second.server.server_port), timeout=2)
        connection.sock = context.wrap_socket(raw, server_hostname='steamcommunity.com')
        self.addCleanup(connection.close)
        connection.request('GET', '/market/', headers={'Host': 'steamcommunity.com'})
        self.assertEqual(connection.getresponse().read(), b'ok')
        second.lease.start.assert_called_once()

    def test_trust_denial_prevents_hosts_and_proxy_installation(self):
        bridge = self.bridge()
        with patch.object(self.trust, 'ensure', side_effect=OSError('denied')):
            with self.assertRaises(OSError): bridge.start(threading.Event())
        bridge.lease.start.assert_not_called()
        bridge.proxy_lease.start.assert_not_called()

    def test_occupied_listener_prevents_certificate_changes(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen(1)
            bridge = self.bridge()
            bridge.port = occupied.getsockname()[1]
            with self.assertRaises(OSError): bridge.start(threading.Event())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.trust.installs)
        bridge.lease.start.assert_not_called()

    def test_cancelled_setup_never_creates_or_trusts_a_certificate(self):
        cancel = threading.Event(); cancel.set()
        bridge = self.bridge()
        with self.assertRaises(OSError): bridge.start(cancel)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.trust.installs)
        bridge.lease.start.assert_not_called()

    def test_failed_hosts_setup_keeps_authorized_certificate_for_next_attempt(self):
        bridge = self.bridge()
        bridge.lease.start.side_effect = OSError('cancelled UAC fixture')
        with self.assertRaises(OSError): bridge.start(threading.Event())
        der = self.trust.installs[0]
        retry = self.bridge()
        self.addCleanup(retry.close)
        retry.start(threading.Event())
        self.assertEqual(retry.der, der)
        self.assertEqual(self.trust.installs, [der])

    def test_persistent_tls_rejects_other_host_and_untrusted_clients(self):
        bridge = self.bridge()
        self.addCleanup(bridge.close)
        bridge.start(threading.Event())
        for host, trusted in (('other.example', True), ('steamcommunity.com', False)):
            context = ssl.create_default_context()
            if trusted: context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(bridge.der))
            with socket.create_connection(('127.0.0.1', bridge.server.server_port), timeout=2) as raw:
                with self.assertRaises((OSError, ssl.SSLError)):
                    context.wrap_socket(raw, server_hostname=host)


if __name__ == '__main__': unittest.main()
